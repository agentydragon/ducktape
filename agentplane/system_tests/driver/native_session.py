"""Real scripted Session turns checked against Dispatcher and History contracts."""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
from tenacity import AsyncRetrying, retry_if_result, stop_after_delay, wait_fixed

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.session_events import SessionEventPages
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, identity_pb2
from agentplane.system_tests.driver.session import Session
from agentplane.system_tests.driver.turn import TURN_SECONDS, Turn
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text


async def native_scripted_turn_with_admission_and_history(
    history: HistoryServiceClient,
    session: Session,
    target: identity_pb2.EnvironmentTarget,
    model: ScriptedModel[Any],
    *,
    command_id: str,
    prompt: str,
    history_turns: int = 1,
    shell_command: str | None = None,
    shell_call_id: str | None = None,
    response: str = "Done.",
) -> tuple[Turn, list[event_log_pb2.EventEntry], ModelRequest[Any]]:
    """Submit one exact command, script its real harness exchange, and await History's fold.

    The returned events are the ordered raw History prefix through this turn's terminal event.
    Every caller-provided identity is checked against what Dispatcher and History recorded.
    """
    if not command_id or history_turns < 1:
        raise ValueError("a command ID and positive completed-turn count are required")
    if not target.namespace or not target.name or not target.uid:
        raise ValueError("History verification requires the exact Environment target")

    requests_before = model.request_count
    async with asyncio.TaskGroup() as turns:
        collecting = turns.create_task(session.next_turn(awaiting_command_id=command_id))
        admission = await session.submit_input(command_id, prompt)
        assert admission.command_id == command_id
        assert admission.operation == command_pb2.OPERATION_SUBMIT_INPUT

        request = await model.request()
        assert prompt in request.user_texts, request.user_texts
        if shell_command is None:
            await model.reply(request, Text(response))
        else:
            call_id = shell_call_id or f"call-{command_id}"
            await model.reply(request, ShellCall(call_id=call_id, command=shell_command))
            tool_result = await model.request()
            try:
                assert any(output.call_id == call_id for output in tool_result.tool_outputs), tool_result.tool_outputs
            finally:
                # Finish the real harness exchange even if the tool result is malformed.
                await model.reply(tool_result, Text(response))

    turn = collecting.result()
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    expected_requests = 1 if shell_command is None else 2
    assert model.request_count - requests_before == expected_requests, model.request_count - requests_before

    entries, terminal_cursor = await _history_through_turns(history, str(session.session_id), history_turns)
    admissions = [
        entry.event.command_admitted.command
        for entry in entries
        if entry.event.HasField("command_admitted") and entry.event.command_admitted.command.command_id == command_id
    ]
    assert admissions == [command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=prompt))]
    summaries: history_service_pb2.SessionSummary = await AsyncRetrying(
        stop=stop_after_delay(TURN_SECONDS),
        wait=wait_fixed(0.05),
        retry=retry_if_result(
            lambda summary: (
                summary.committed_cursor < terminal_cursor
                or summary.folded_through < terminal_cursor
                or not summary.HasField("sandbox")
            )
        ),
    )(history.get_session_summary, str(session.session_id))
    assert summaries.session_id == str(session.session_id)
    assert summaries.committed_cursor >= terminal_cursor
    assert summaries.folded_through >= terminal_cursor
    assert summaries.HasField("sandbox")
    assert summaries.sandbox.namespace == target.namespace
    assert summaries.sandbox.name == target.name
    assert summaries.sandbox.uid == target.uid
    assert summaries.last_turn_status == event_pb2.TURN_STATUS_COMPLETED
    return turn, entries, request


async def _history_through_turns(
    history: HistoryServiceClient, session_id: str, completed_turns: int
) -> tuple[list[event_log_pb2.EventEntry], int]:
    pages = SessionEventPages(after_cursor=0)
    async with asyncio.timeout(TURN_SECONDS):
        while True:
            try:
                page = await history.read_session_events(session_id, after_cursor=pages.cursor, limit=pages.limit)
            except TimeoutError:
                await asyncio.sleep(0.2)
                continue
            except ServiceError as error:
                if error.code != grpc.StatusCode.UNAVAILABLE:
                    raise
                await asyncio.sleep(0.2)
                continue
            pages.apply_page(page)
            entries = list(pages.entries)
            completed = [entry.cursor for entry in entries if entry.event.HasField("turn_completed")]
            if len(completed) >= completed_turns:
                assert len(completed) == completed_turns, completed
                terminal_cursor = completed[-1]
                assert terminal_cursor > 0
                return [entry for entry in entries if entry.cursor <= terminal_cursor], terminal_cursor
            await asyncio.sleep(0.05)


# gazelle:include_dep @pypi//protobuf
