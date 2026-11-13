"""Offline system test driver startup and terminal-result checks, not deployed harness evidence."""

import asyncio
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.history_service.api.history import history_service_pb2
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import agent as agent_module
from agentplane.system_tests.driver.agent import RUNNER_ANSWERS_SECONDS, Agent, scripted_turn_with_admission_and_history
from agentplane.testing.scripted_model import ScriptedModel

# gazelle:include_dep @pypi//protobuf


def _entry(cursor: int, event: event_pb2.Event) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(cursor=cursor, event=event)


def _page(*entries: event_log_pb2.EventEntry) -> history_service_pb2.ReadSessionEventsResponse:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=entries[-1].cursor if entries else 0)
    page.entries.extend(entries)
    return page


async def test_agent_create_retries_unavailable_without_creating_another_session() -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    unavailable = httpx.HTTPStatusError(
        "runner is not listening", request=request, response=httpx.Response(503, request=request)
    )
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.create_session_and_wait.side_effect = [unavailable, None]

    await Agent.create(
        client,
        dispatcher,
        sandbox="test-sandbox",
        sandbox_uid="00000000-0000-4000-8000-000000000001",
        harness=session_pb2.HARNESS_CODEX,
        model="test-model",
    )

    first, retry = client.create_session_and_wait.call_args_list
    assert first == retry
    assert first.args[0] == "test-sandbox"
    assert first.kwargs["sandbox_uid"] == "00000000-0000-4000-8000-000000000001"
    assert isinstance(first.args[1], UUID)
    assert first.kwargs["overrides"]["model"] == "test-model"
    assert first.kwargs["overrides"]["placement"]["environmentHosted"] == {
        "environment": {"name": "test-sandbox", "uid": "00000000-0000-4000-8000-000000000001"},
        "harness": "HARNESS_CODEX",
    }
    client.wait_for_thread_projection.assert_awaited_once_with(first.args[1], timeout_s=RUNNER_ANSWERS_SECONDS)


@pytest.mark.parametrize("status", [409, 500, 502])
async def test_create_refusals_and_other_server_failures_are_not_retried(status: int) -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    response = httpx.Response(status, request=request)
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.create_session_and_wait.side_effect = httpx.HTTPStatusError(
        "open refused", request=request, response=response
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await Agent.create(
            client,
            dispatcher,
            sandbox="test-sandbox",
            sandbox_uid="00000000-0000-4000-8000-000000000001",
            harness=session_pb2.HARNESS_CODEX,
            model="test-model",
        )
    assert caught.value.response is response
    client.create_session_and_wait.assert_awaited_once()
    client.thread.assert_not_awaited()


async def test_run_deadline_bounds_empty_history_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "TURN_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    client.read_session_events.return_value = history_service_pb2.ReadSessionEventsResponse()
    with pytest.raises(TimeoutError):
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
            "test prompt"
        )
    client.read_session_events.assert_awaited_once()
    dispatcher.submit_input.assert_awaited_once()


@pytest.mark.parametrize("failure_kind", ["command_failed", "harness_launch_failed"])
async def test_run_reports_terminal_runner_failure(failure_kind: str) -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "test startup failed\n\x1b[31mtest detail"

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="test prompt"))
        failure = (
            event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command_id, reason=diagnostic))
            if failure_kind == "command_failed"
            else event_pb2.Event(harness_launch_failed=event_pb2.HarnessLaunchFailed(reason=diagnostic))
        )
        return _page(
            _entry(after_cursor + 1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(after_cursor + 2, failure),
        )

    client.read_session_events.side_effect = history_page
    with pytest.raises(AssertionError) as caught:
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
            "test prompt"
        )

    assert repr(diagnostic) in str(caught.value)
    assert "\x1b" not in str(caught.value)


async def test_run_ignores_prior_turns_and_failures_until_its_command_is_admitted() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="test prompt"))
        return _page(
            _entry(
                after_cursor + 1,
                event_pb2.Event(
                    command_failed=event_pb2.CommandFailed(command_id="retained-command", reason="old failure")
                ),
            ),
            _entry(
                after_cursor + 2,
                event_pb2.Event(harness_launch_failed=event_pb2.HarnessLaunchFailed(reason="old launch failure")),
            ),
            _entry(
                after_cursor + 3,
                event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="old", text="old answer")),
            ),
            _entry(
                after_cursor + 4,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="old-turn", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ),
            _entry(after_cursor + 5, event_pb2.Event(harness_started=event_pb2.HarnessStarted(resumed=True))),
            _entry(after_cursor + 6, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(
                after_cursor + 7,
                event_pb2.Event(
                    harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                        harness_message_id="message", origin_command_ids=[command_id]
                    )
                ),
            ),
            _entry(
                after_cursor + 8,
                event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="new", text="new answer")),
            ),
            _entry(
                after_cursor + 9,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="new-turn", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ),
        )

    client.read_session_events.side_effect = history_page
    turn = await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
        "test prompt"
    )

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert turn.answer == "new answer"
    assert turn.resumed
    assert turn.input_confirmed
    assert client.read_session_events.call_args.kwargs["after_cursor"] == 0


async def test_run_fails_on_matching_command_failure_before_admission() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "command rejected before admission"

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        return _page(
            _entry(
                after_cursor + 1,
                event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command_id, reason=diagnostic)),
            )
        )

    client.read_session_events.side_effect = history_page

    with pytest.raises(AssertionError, match=diagnostic):
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).run(
            "test prompt"
        )


async def test_next_turn_re_reads_entries_after_the_returned_terminal_in_a_batched_page() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    thread_id = uuid4()

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput())
        if dispatcher.submit_input.await_count == 1:
            return _page(
                _entry(1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
                _entry(
                    2,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(turn_id="first", status=event_pb2.TURN_STATUS_COMPLETED)
                    ),
                ),
                _entry(
                    3,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(
                            turn_id="later-retained", status=event_pb2.TURN_STATUS_COMPLETED
                        )
                    ),
                ),
            )
        assert after_cursor == 2
        return _page(
            _entry(
                3,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="later-retained", status=event_pb2.TURN_STATUS_COMPLETED
                    )
                ),
            ),
            _entry(4, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(5, event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="second", text="second answer"))),
            _entry(
                6,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="second", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ),
        )

    client.read_session_events.side_effect = history_page
    agent = Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=thread_id, cursor=0)

    first = await agent.run("first prompt")
    second = await agent.run("second prompt")

    assert first.status == event_pb2.TURN_STATUS_COMPLETED
    assert second.answer == "second answer"
    assert [call.kwargs["after_cursor"] for call in client.read_session_events.call_args_list] == [0, 2]


async def test_next_turn_reports_a_new_command_failure_without_an_awaited_id() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    diagnostic = "test notification command failed"
    client.read_session_events.return_value = _page(
        _entry(
            1, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id="incoming-command", reason=diagnostic))
        )
    )
    with pytest.raises(AssertionError) as caught:
        await Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0).next_turn()

    assert repr(diagnostic) in str(caught.value)


async def test_credentials_turn_cancels_model_wait_when_collection_fails() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    command_id = "test-command"
    diagnostic = "test runner launch failed"
    dispatcher.submit_input.return_value = session_dispatcher_pb2.Dispatch(
        command_id=command_id, operation=command_pb2.OPERATION_SUBMIT_INPUT
    )
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def blocked_request() -> None:
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    model = create_autospec(ScriptedModel, instance=True)
    model.request.side_effect = blocked_request

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del after_cursor, limit
        await request_started.wait()
        return _page(
            _entry(1, event_pb2.Event(command_failed=event_pb2.CommandFailed(command_id=command_id, reason=diagnostic)))
        )

    client.read_session_events.side_effect = history_page
    agent = Agent(client, dispatcher, sandbox="box", sandbox_uid="box-uid", thread_id=uuid4(), cursor=0)
    async with asyncio.timeout(1):
        with pytest.raises(ExceptionGroup) as caught:
            await scripted_turn_with_admission_and_history(
                client, agent, command_id=command_id, prompt="test prompt", model=model
            )

    assert request_cancelled.is_set()
    model.reply.assert_not_awaited()
    assert isinstance(caught.value.exceptions[0], AssertionError)
    assert diagnostic in str(caught.value.exceptions[0])


async def test_run_returns_successful_turn_outputs() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    thread_id = UUID("00000000-0000-4000-8000-000000000001")

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="test prompt"))
        return _page(
            _entry(
                after_cursor + 1,
                event_pb2.Event(
                    native=event_pb2.Native(direction=event_pb2.DIRECTION_TO_HARNESS, line='{"test": "input"}')
                ),
            ),
            _entry(after_cursor + 2, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(
                after_cursor + 3,
                event_pb2.Event(
                    item_completed=event_pb2.ItemCompleted(
                        item_id="test-tool", tool=event_pb2.ToolResult(output="test tool output", succeeded=True)
                    )
                ),
            ),
            _entry(
                after_cursor + 4,
                event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="test-answer", text="test answer")),
            ),
            _entry(
                after_cursor + 5,
                event_pb2.Event(
                    native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"test": "result"}')
                ),
            ),
            _entry(
                after_cursor + 6,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED)
                ),
            ),
        )

    client.read_session_events.side_effect = history_page
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

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        if dispatcher.submit_input.await_count == 1:
            command = command_pb2.Command(
                command_id=command_id, submit_input=command_pb2.SubmitInput(text="test admitted prompt")
            )
            return _page(
                _entry(after_cursor + 1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
                _entry(
                    after_cursor + 2,
                    event_pb2.Event(
                        item_completed=event_pb2.ItemCompleted(item_id="test-partial", text="test partial output")
                    ),
                ),
                _entry(
                    after_cursor + 3,
                    event_pb2.Event(
                        text_delta=event_pb2.TextDelta(item_id="test-unfinished", text="test unfinished text")
                    ),
                ),
                _entry(
                    after_cursor + 4,
                    event_pb2.Event(
                        turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=status, error=diagnostic)
                    ),
                ),
            )
        command = command_pb2.Command(
            command_id=command_id, submit_input=command_pb2.SubmitInput(text="test explicit next prompt")
        )
        return _page(
            _entry(after_cursor + 1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(
                after_cursor + 2,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id="test-next-turn", status=event_pb2.TURN_STATUS_COMPLETED
                    )
                ),
            ),
        )

    client.read_session_events.side_effect = history_page
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

    await agent.run("test explicit next prompt")
    assert dispatcher.submit_input.await_count == 2
    assert dispatcher.submit_input.call_args.args[1] != first_command_id
    assert dispatcher.submit_input.call_args.args[4] == "test explicit next prompt"
    assert client.read_session_events.call_args.kwargs["after_cursor"] == 11


async def test_imported_agent_resolves_and_pins_its_destination_via_native_dispatcher() -> None:
    client = create_autospec(Client, instance=True)
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    dispatcher.session_target.return_value = ("original", "original-uid")
    thread_id = uuid4()

    async def history_page(
        _thread_id: UUID, *, after_cursor: int, limit: int
    ) -> history_service_pb2.ReadSessionEventsResponse:
        del limit
        command_id = dispatcher.submit_input.call_args.args[1]
        command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput())
        return _page(
            _entry(after_cursor + 1, event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=command))),
            _entry(
                after_cursor + 2,
                event_pb2.Event(
                    turn_completed=event_pb2.TurnCompleted(
                        turn_id=f"turn-{dispatcher.submit_input.await_count}", status=event_pb2.TURN_STATUS_COMPLETED
                    )
                ),
            ),
        )

    client.read_session_events.side_effect = history_page
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
