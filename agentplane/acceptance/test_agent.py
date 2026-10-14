"""Offline acceptance-driver startup and terminal-result checks, not deployed harness evidence."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import Mock, call, create_autospec
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel

from agentplane.acceptance import conftest as fixtures
from agentplane.acceptance.agent import Agent
from agentplane.acceptance.conftest import SandboxNotReadyError, suspend_and_delete
from agentplane.app.live import SandboxesSnapshot
from agentplane.app.testing.client import Attachment, Client
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2
from agentplane.sandbox_resource import OperatingMode

# gazelle:include_dep @pypi//protobuf


async def test_cleanup_waits_for_effective_suspension_before_deleting() -> None:
    client = create_autospec(Client, instance=True)
    uid = uuid4()
    closed = False

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        nonlocal closed
        try:
            for name, fresh, mode in [
                ("owned-test-sandbox", True, OperatingMode.RUNNING),
                ("owned-test-sandbox", False, OperatingMode.SUSPENDED),
                ("other-sandbox", True, OperatingMode.SUSPENDED),
                ("owned-test-sandbox", True, OperatingMode.SUSPENDED),
            ]:
                client.delete_sandbox.assert_not_awaited()
                yield Mock(
                    watch=Mock(fresh=fresh), sandboxes=[SimpleNamespace(name=name, uid=uid, operating_mode=mode)]
                )
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()

    await suspend_and_delete(client, "owned-test-sandbox", uid)

    assert client.mock_calls == [
        call.suspend_sandbox("owned-test-sandbox"),
        call.sandbox_snapshots(),
        call.delete_sandbox("owned-test-sandbox"),
    ]
    assert closed


async def test_cleanup_does_not_delete_a_replacement_sandbox() -> None:
    client = create_autospec(Client, instance=True)

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        yield Mock(
            watch=Mock(fresh=True),
            sandboxes=[SimpleNamespace(name="owned-test-sandbox", uid=uuid4(), operating_mode=OperatingMode.SUSPENDED)],
        )

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(SandboxNotReadyError, match="has been replaced"):
        await suspend_and_delete(client, "owned-test-sandbox", uuid4())
    client.delete_sandbox.assert_not_awaited()


async def test_cleanup_bounds_a_watch_that_stops_making_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixtures, "SANDBOX_READY_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    closed = False

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        nonlocal closed
        try:
            yield Mock(watch=Mock(fresh=False), sandboxes=[])
            await asyncio.Event().wait()
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(TimeoutError):
        await suspend_and_delete(client, "owned-test-sandbox", uuid4())
    client.delete_sandbox.assert_not_awaited()
    assert closed


async def test_cleanup_does_not_retry_an_authorization_refusal_or_delete() -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/sandboxes/owned-test-sandbox")
    refused = httpx.HTTPStatusError("read refused", request=request, response=httpx.Response(403, request=request))

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        raise refused
        yield  # pragma: no cover

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(httpx.HTTPStatusError):
        await suspend_and_delete(client, "owned-test-sandbox", uuid4())

    client.sandbox_snapshots.assert_called_once()
    client.delete_sandbox.assert_not_awaited()


async def test_agent_open_retries_unavailable_without_creating_another_session() -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    unavailable = httpx.HTTPStatusError(
        "runner is not listening", request=request, response=httpx.Response(503, request=request)
    )
    client = create_autospec(Client, instance=True)
    client.create_session.side_effect = [
        unavailable,
        Attachment(runner_pb2.Attached(session_id="00000000-0000-4000-8000-000000000001", last_cursor=7)),
    ]
    client.thread.return_value = Mock(id=UUID("00000000-0000-4000-8000-000000000001"))

    await Agent.open(client, sandbox="test-sandbox", harness=runner_pb2.HARNESS_CODEX, model="test-model")

    first, retry = client.create_session.call_args_list
    assert first == retry
    assert first.args[0] == "test-sandbox"
    assert isinstance(first.args[1], UUID)
    assert first.kwargs["overrides"]["model"] == "test-model"
    client.thread.assert_awaited_once_with(UUID("00000000-0000-4000-8000-000000000001"))


@pytest.mark.parametrize("status", [409, 500, 502])
async def test_startup_refusals_and_other_server_failures_are_not_retried(status: int) -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    response = httpx.Response(status, request=request)
    client = create_autospec(Client, instance=True)
    client.create_session.side_effect = httpx.HTTPStatusError("open refused", request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await Agent.open(client, sandbox="test-sandbox", harness=runner_pb2.HARNESS_CODEX, model="test-model")
    assert caught.value.response is response
    client.create_session.assert_awaited_once()
    client.thread.assert_not_awaited()


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
    client.events.return_value = events()
    thread_id = UUID("00000000-0000-4000-8000-000000000001")
    turn = await Agent(client, thread_id=thread_id, cursor=7).run("test prompt")

    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    assert turn.tool_outputs == ["test tool output"]
    assert turn.answer == "test answer"
    assert turn.native == ['{"test": "result"}']
    client.command.assert_awaited_once()
    assert client.command.call_args.args[0] == thread_id
    assert client.command.call_args.args[1].submit_input.text == "test prompt"


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
    diagnostic = "test upstream unavailable\n\x1b[31mtest diagnostic"

    async def events() -> AsyncIterator[event_log_pb2.EventEntry]:
        for cursor, event in enumerate(
            [
                event_pb2.Event(command_admitted=event_pb2.CommandAdmitted(command=client.command.call_args.args[1])),
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
    agent = Agent(client, thread_id=UUID("00000000-0000-4000-8000-000000000001"), cursor=7)
    with pytest.raises(AssertionError) as caught:
        await agent.run("test admitted prompt")

    assert "test-turn" in str(caught.value)
    assert event_pb2.TurnStatus.Name(status) in str(caught.value)
    assert repr(diagnostic) in str(caught.value)
    assert "\x1b" not in str(caught.value)
    assert "test partial output" not in str(caught.value)
    client.command.assert_awaited_once()
    first_command = client.command.call_args.args[1]
    assert first_command.submit_input.text == "test admitted prompt"

    async def next_turn() -> AsyncIterator[event_log_pb2.EventEntry]:
        yield event_log_pb2.EventEntry(
            cursor=12,
            event=event_pb2.Event(
                turn_completed=event_pb2.TurnCompleted(turn_id="test-next-turn", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
        )

    client.events.return_value = next_turn()
    await agent.run("test explicit next prompt")
    assert client.command.await_count == 2
    assert client.command.call_args.args[1].command_id != first_command.command_id
    assert client.command.call_args.args[1].submit_input.text == "test explicit next prompt"
    assert client.events.call_args.kwargs["after"] == 11


if __name__ == "__main__":
    pytest_bazel.main()
