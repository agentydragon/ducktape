"""Exercise the native Session driver against the real Dispatcher, Runtime and History."""

import asyncio
import re
import signal
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

from agentplane.env.e2e.testing import events
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess
from agentplane.harness_host import harness_host_pb2
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import event_log_pb2, event_pb2, session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient, admitted
from agentplane.system_tests.driver.session import Session
from agentplane.testing.scripted_model import ScriptedModel, Text

# gazelle:include_dep @pypi//protobuf


async def history_after_turns(
    history: GoHistoryService, session_id: str, turn_count: int
) -> list[event_log_pb2.EventEntry]:
    async with asyncio.timeout(20):
        while True:
            entries = list((await history.reader.read_session_events(session_id, limit=1000)).entries)
            if len(events.of_kind(entries, "turn_completed")) >= turn_count:
                return entries
            await asyncio.sleep(0.05)


async def runtime_idle(remote: SandboxControlClient, sandbox: SandboxCluster, session_id: str) -> None:
    runner = remote.runner(sandbox.destination)
    async with asyncio.timeout(20):
        while True:
            sessions = [row for row in await runner.list_sessions() if row.session_id == session_id]
            if len(sessions) == 1:
                session = sessions[0]
                if (
                    not session.active_turn_id
                    and not session.pending_commands
                    and session.WhichOneof("host") == "environment_hosted"
                    and session.environment_hosted.setup != session_pb2.SETUP_STATE_RUNNING
                    and session.environment_hosted.harness.state != harness_host_pb2.HarnessAttachment.STATE_STARTING
                ):
                    return
            await asyncio.sleep(0.05)


@contextmanager
def paused(process: asyncio.subprocess.Process) -> Iterator[None]:
    process.send_signal(signal.SIGSTOP)
    try:
        yield
    finally:
        process.send_signal(signal.SIGCONT)


async def test_native_session_driver_reads_exact_turn_across_history_outage_and_prior_turn(
    dispatcher: SessionDispatcherClient,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    runtime_client: SandboxControlClient,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel[Any],
    spec: session_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep setup, exact turn admission and correction-aware reads outside App."""
    await start_runtime()
    session_id = uuid4()
    setup_script = "echo NATIVE_SETUP_OUT\necho NATIVE_SETUP_ERR >&2\n"
    session = await Session.create(
        dispatcher, history_service.reader, spec=spec, session_id=session_id, setup_script=setup_script
    )
    assert session.session_id == session_id
    stored = await dispatcher.get_session(str(session_id))
    assert stored.session_id == str(session_id)
    assert stored.placement.environment_hosted.environment == spec.placement.environment_hosted.environment
    assert await session.setup_output() == ("NATIVE_SETUP_OUT\n", "NATIVE_SETUP_ERR\n")

    # Leave a completed turn ahead of the driver's cursor. Its retained terminal must not satisfy
    # the exact command ID requested below.
    first_id = str(uuid4())
    first_text = "Reply FIRST"
    first_dispatch = await dispatcher.submit_input(str(session_id), first_id, sandbox.name, sandbox.uid, first_text)
    await admitted(dispatcher, first_dispatch)
    first_request = await model.request()
    assert first_request.user_texts == [first_text]
    await model.reply(first_request, Text("FIRST"))
    await history_after_turns(history_service, str(session_id), 1)

    # The Runtime can finish and report idle while its real History reader is stopped. The native
    # driver's read remains pending, then catches up from the retained journal when History returns.
    second_id = str(uuid4())
    second_text = "Reply SECOND"
    previous_timeout = history_service.reader.request_timeout_s
    read_timed_out = asyncio.Event()
    original_read = history_service.reader.read_session_events

    async def observe_history_read(
        observed_session_id: str, *, after_cursor: int = 0, limit: int = 128
    ) -> history_service_pb2.ReadSessionEventsResponse:
        try:
            return await original_read(observed_session_id, after_cursor=after_cursor, limit=limit)
        except TimeoutError:
            read_timed_out.set()
            raise

    history_service.reader.request_timeout_s = 0.5
    try:
        with monkeypatch.context() as observer:
            observer.setattr(history_service.reader, "read_session_events", observe_history_read)
            async with asyncio.TaskGroup() as tasks:
                with paused(history_service.process.process):
                    second_dispatch = await session.submit_input(second_id, second_text)
                    await admitted(dispatcher, second_dispatch)
                    collecting = tasks.create_task(session.next_turn(awaiting_command_id=second_id))
                    second_request = await model.request()
                    assert second_request.user_texts == [first_text, second_text]
                    await asyncio.wait_for(read_timed_out.wait(), timeout=5)
                    assert not collecting.done(), "the retained prior turn was accepted for the new command"
                    await model.reply(second_request, Text("SECOND"))
                    await runtime_idle(runtime_client, sandbox, str(session_id))
                    assert not collecting.done(), "Runtime completion was incorrectly reported as a History result"
                second_turn = await asyncio.wait_for(collecting, timeout=20)
    finally:
        history_service.reader.request_timeout_s = previous_timeout

    assert second_turn.answer == "SECOND"
    assert second_turn.text == ["SECOND"]
    assert "FIRST" not in second_turn.transcript

    third_text = "Reply THIRD"
    async with asyncio.TaskGroup() as tasks:
        third_run = tasks.create_task(session.run(third_text))
        third_request = await model.request()
        assert third_request.user_texts == [first_text, second_text, third_text]
        await model.reply(third_request, Text("THIRD"))
        third_turn = await asyncio.wait_for(third_run, timeout=20)
    assert third_turn.answer == "THIRD"

    entries = await history_after_turns(history_service, str(session_id), 3)
    admissions = [entry.event.command_admitted.command for entry in events.of_kind(entries, "command_admitted")]
    assert [(command.command_id, command.submit_input.text) for command in admissions] == [
        (first_id, first_text),
        (second_id, second_text),
        (admissions[2].command_id, third_text),
    ]
    assert re.fullmatch(r"input-[0-9a-f]{8}", admissions[2].command_id)
    assert [
        entry.event.item_completed.text
        for entry in events.of_kind(entries, "item_completed")
        if entry.event.item_completed.WhichOneof("outcome") == "text"
    ] == ["FIRST", "SECOND", "THIRD"]
    completed = [entry.event.turn_completed for entry in events.of_kind(entries, "turn_completed")]
    assert [row.status for row in completed] == [event_pb2.TURN_STATUS_COMPLETED] * 3
    assert model.request_count == 3
    events.assert_contiguous(entries)
