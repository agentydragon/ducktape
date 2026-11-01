"""Offline system test driver startup and terminal-result checks, not deployed harness evidence."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests import agent as agent_module
from agentplane.system_tests.agent import RUNNER_ANSWERS_SECONDS, Agent, scripted_turn_with_admission_and_history
from agentplane.testing.scripted_model import ScriptedModel

# gazelle:include_dep @pypi//protobuf


async def test_agent_open_retries_unavailable_without_creating_another_session() -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    unavailable = httpx.HTTPStatusError(
        "runner is not listening", request=request, response=httpx.Response(503, request=request)
    )
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.create_session.side_effect = [unavailable, None]

    await Agent.open(
        client,
        dispatcher,
        sandbox="test-sandbox",
        sandbox_uid=UUID("00000000-0000-4000-8000-000000000001"),
        harness=runner_pb2.HARNESS_CODEX,
        model="test-model",
    )

    first, retry = client.create_session.call_args_list
    assert first == retry
    assert first.args[0] == "test-sandbox"
    assert first.kwargs["sandbox_uid"] == UUID("00000000-0000-4000-8000-000000000001")
    assert isinstance(first.args[1], UUID)
    assert first.kwargs["overrides"]["model"] == "test-model"
    client.wait_for_thread_projection.assert_awaited_once_with(first.args[1], timeout_s=RUNNER_ANSWERS_SECONDS)


@pytest.mark.parametrize("status", [409, 500, 502])
async def test_startup_refusals_and_other_server_failures_are_not_retried(status: int) -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    response = httpx.Response(status, request=request)
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.create_session.side_effect = httpx.HTTPStatusError("open refused", request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await Agent.open(
            client,
            dispatcher,
            sandbox="test-sandbox",
            sandbox_uid=UUID("00000000-0000-4000-8000-000000000001"),
            harness=runner_pb2.HARNESS_CODEX,
            model="test-model",
        )
    assert caught.value.response is response
    client.create_session.assert_awaited_once()
    client.thread.assert_not_awaited()


async def test_run_deadline_closes_a_stream_kept_open_without_terminal_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "TURN_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    closed = False

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        nonlocal closed
        try:
            yield event_log_pb2.EventEntry(cursor=1, event=event_pb2.Event(setup_started=event_pb2.SetupStarted()))
            # SSE keepalives can keep the HTTP read alive without yielding another EventEntry.
            await asyncio.Event().wait()
        finally:
            closed = True

    client.events.return_value = events()
    with pytest.raises(TimeoutError):
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
            "test prompt"
        )
    assert closed
    dispatcher.submit_input.assert_awaited_once()


@pytest.mark.parametrize("failure_kind", ["command_failed", "harness_launch_failed"])
async def test_run_reports_terminal_runner_failure_and_closes_the_stream(failure_kind: str) -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "test startup failed\n\x1b[31mtest detail"
    closed = False

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        nonlocal closed
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="test prompt"))
        try:
            yield event_log_pb2.EventEntry(
                cursor=1, event=event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))
            )
            failure = (
                event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command_id, reason=diagnostic))
                if failure_kind == "command_failed"
                else event_pb2.Event(harness_launch_failed=event_pb2.HarnessLaunchFailed(reason=diagnostic))
            )
            yield event_log_pb2.EventEntry(cursor=2, event=failure)
            await asyncio.Event().wait()
        finally:
            closed = True

    client.events.return_value = events()
    with pytest.raises(AssertionError) as caught:
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
            "test prompt"
        )

    assert repr(diagnostic) in str(caught.value)
    assert "\x1b" not in str(caught.value)
    assert closed


async def test_run_ignores_retained_failures_until_its_command_is_admitted() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="test prompt"))
        for entry in (
            event_log_pb2.EventEntry(
                cursor=1,
                event=event_pb2.Event(
                    command_failed=event_pb2.CommandFailed(command_id="retained-command", reason="old failure")
                ),
            ),
            event_log_pb2.EventEntry(
                cursor=2,
                event=event_pb2.Event(harness_launch_failed=event_pb2.HarnessLaunchFailed(reason="old launch failure")),
            ),
            event_log_pb2.EventEntry(
                cursor=3, event=event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))
            ),
            event_log_pb2.EventEntry(
                cursor=4,
                event=event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ),
        ):
            yield entry
        pytest.fail("Agent.run read past the terminal event")

    client.events.return_value = events()
    turn = await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
        "test prompt"
    )

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert client.events.call_args.kwargs["awaiting_command_id"] == dispatcher.submit_input.call_args.args[1]


async def test_next_turn_reports_a_new_command_failure_without_an_awaited_id() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "test notification command failed"
    closed = False

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        nonlocal closed
        try:
            yield event_log_pb2.EventEntry(
                cursor=1,
                event=event_pb2.Event(
                    command_failed=event_pb2.CommandFailed(command_id="incoming-command", reason=diagnostic)
                ),
            )
            await asyncio.Event().wait()
        finally:
            closed = True

    client.events.return_value = events()
    with pytest.raises(AssertionError) as caught:
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).next_turn()

    assert repr(diagnostic) in str(caught.value)
    assert closed


async def test_credentials_turn_cancels_model_wait_when_collection_fails() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    command_id = "test-command"
    diagnostic = "test runner launch failed"
    dispatcher.submit_input.return_value = session_dispatcher_pb2.Dispatch(
        command_id=command_id, operation=session_dispatcher_pb2.OPERATION_SUBMIT_INPUT
    )
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    stream_closed = False

    async def blocked_request() -> None:
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    model = create_autospec(ScriptedModel, instance=True)
    model.request.side_effect = blocked_request

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        nonlocal stream_closed
        try:
            await request_started.wait()
            yield event_log_pb2.EventEntry(
                cursor=1,
                event=event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command_id, reason=diagnostic)),
            )
            await asyncio.Event().wait()
        finally:
            stream_closed = True

    client.events.return_value = events()
    agent = Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0)
    async with asyncio.timeout(1):
        with pytest.raises(ExceptionGroup) as caught:
            await scripted_turn_with_admission_and_history(
                client, agent, command_id=command_id, prompt="test prompt", model=model
            )

    assert request_cancelled.is_set()
    model.reply.assert_not_awaited()
    assert stream_closed
    assert isinstance(caught.value.exceptions[0], AssertionError)
    assert diagnostic in str(caught.value.exceptions[0])


async def test_run_returns_successful_turn_outputs() -> None:
    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        for cursor, event in enumerate(
            [
                event_pb2.Event(
                    native=event_pb2.Native(direction=event_pb2.DIRECTION_TO_HARNESS, line='{"test": "input"}')
                ),
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(
                        item_id="test-tool", tool=event_pb2.ToolResult(output="test tool output", succeeded=True)
                    )
                ),
                event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="test-answer", text="test answer")),
                event_pb2.Event(
                    native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"test": "result"}')
                ),
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ],
            start=8,
        ):
            yield event_log_pb2.EventEntry(cursor=cursor, event=event)
        pytest.fail("Agent.run read past the terminal event")

    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.events.return_value = events()
    thread_id = UUID("00000000-0000-4000-8000-000000000001")
    turn = await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=thread_id, cursor=7).run(
        "test prompt"
    )

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert turn.tool_outputs == ["test tool output"]
    assert turn.answer == "test answer"
    assert turn.native == ['{"test": "result"}']
    dispatcher.submit_input.assert_awaited_once()
    assert dispatcher.submit_input.call_args.args[0] == str(thread_id)
    assert dispatcher.submit_input.call_args.args[2:4] == ("box", "box-uid")
    assert dispatcher.submit_input.call_args.args[4] == "test prompt"


@pytest.mark.parametrize(
    "status",
    [
        event_pb2.TURN_STATUS_FAILED,
        event_pb2.TURN_STATUS_INTERRUPTED,
        event_pb2.TURN_STATUS_PROCESS_LOST,
        event_pb2.TURN_STATUS_UNSPECIFIED,
    ],
)
async def test_run_rejects_unsuccessful_terminal_after_partial_output(status: event_pb2.TurnStatus) -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "test upstream unavailable\n\x1b[31mtest diagnostic"

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        for cursor, event in enumerate(
            [
                event_pb2.Event(
                    command_admitted=event_pb2.CommandAdmitted(
                        command=command_pb2.Command(
                            command_id=dispatcher.submit_input.call_args.args[1],
                            submit_input=command_pb2.SubmitInput(text=dispatcher.submit_input.call_args.args[4]),
                        )
                    )
                ),
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(item_id="test-partial", text="test partial output")
                ),
                event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-unfinished", text="test unfinished text")),
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=status, error=diagnostic)
                ),
            ],
            start=8,
        ):
            yield event_log_pb2.EventEntry(cursor=cursor, event=event)
        pytest.fail("Agent.run read past the non-successful terminal event")

    client.events.return_value = events()
    agent = Agent(
        client,
        dispatcher,
        sandbox="box",
        sandbox_uid="box-uid",
        thread_id=UUID("00000000-0000-4000-8000-000000000001"),
        cursor=7,
    )
    with pytest.raises(AssertionError) as caught:
        await agent.run("test admitted prompt")

    assert "test-turn" in str(caught.value)
    assert event_pb2.TurnStatus.Name(status) in str(caught.value)
    assert repr(diagnostic) in str(caught.value)
    assert "\x1b" not in str(caught.value)
    assert "test partial output" not in str(caught.value)
    dispatcher.submit_input.assert_awaited_once()
    first_command_id = dispatcher.submit_input.call_args.args[1]
    assert dispatcher.submit_input.call_args.args[4] == "test admitted prompt"

    async def next_turn() -> AsyncIterator[event_log_pb2.EventEntry]:
        yield event_log_pb2.EventEntry(
            cursor=12,
            event=event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id="test-next-turn", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        )

    client.events.return_value = next_turn()
    await agent.run("test explicit next prompt")
    assert dispatcher.submit_input.await_count == 2
    assert dispatcher.submit_input.call_args.args[1] != first_command_id
    assert dispatcher.submit_input.call_args.args[4] == "test explicit next prompt"
    assert client.events.call_args.kwargs["after"] == 11


async def test_imported_agent_resolves_and_pins_its_destination_via_native_dispatcher() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    dispatcher.session_target.return_value = ("original", "original-uid")
    thread_id = uuid4()

    async def events(cursor: int) -> AsyncIterator[event_log_pb2.EventEntry]:
        yield event_log_pb2.EventEntry(
            cursor=cursor,
            event=event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id=f"turn-{cursor}", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        )

    client.events.side_effect = [events(1), events(2)]
    agent = Agent(client, dispatcher, thread_id=thread_id, cursor=0)
    await agent.run("first")
    await agent.run("second")
    dispatcher.session_target.assert_awaited_once_with(str(thread_id))
    assert dispatcher.submit_input.await_count == 2
    assert all(call.args[2:4] == ("original", "original-uid") for call in dispatcher.submit_input.call_args_list)


def test_agent_rejects_a_partially_specified_destination() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    with pytest.raises(ValueError, match="Sandbox name and UID must be supplied together"):
        Agent(client, dispatcher, thread_id=uuid4(), cursor=0, sandbox="box")


if __name__ == "__main__":
    pytest_bazel.main()
