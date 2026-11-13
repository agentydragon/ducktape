"""Durable Thread history reads, including independent readers and archived turns nobody watched."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.app.testing.history_reads import SessionEventReader
from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import RUNNER_ANSWERS_SECONDS, TURN_SECONDS, Agent
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.scripted_model import Text

# History settlement may remove these streamed entries after their paired completion is archived.
_SETTLEABLE = frozenset({"native", "text_delta", "tool_arguments_delta", "tool_output_delta"})


type ObservationPredicate = Callable[[event_log_pb2.EventEntry, list[event_log_pb2.EventEntry]], bool]


async def _until(
    reader: SessionEventReader, predicate: ObservationPredicate, *, after_cursor: int = 0
) -> list[event_log_pb2.EventEntry]:
    """Return the corrected retained prefix through the first observation matching `predicate`."""
    with tracer.start_as_current_span(
        "system_test.thread_history.event_wait", record_exception=False, set_status_on_exception=False
    ):
        async with asyncio.timeout(TURN_SECONDS):
            while True:
                entries = list(reader.entries)
                for index, entry in enumerate(entries):
                    if entry.cursor <= after_cursor:
                        continue
                    prefix = [row for row in entries[: index + 1] if row.cursor > after_cursor]
                    if predicate(entry, prefix):
                        return prefix
                await reader.read_page()
                await reader.wait_for_next_page()


def _command_completed(entries: list[event_log_pb2.EventEntry], command_id: str, status: int) -> bool:
    admissions = [
        entry
        for entry in entries
        if entry.event.WhichOneof("observation") == "command_admitted"
        and entry.event.command_admitted.command.command_id == command_id
    ]
    if not admissions:
        return False
    admitted_at = admissions[0].cursor
    turn_ids = {
        entry.event.harness_user_message_confirmed.turn_id
        for entry in entries
        if entry.cursor > admitted_at
        and entry.event.WhichOneof("observation") == "harness_user_message_confirmed"
        and command_id in entry.event.harness_user_message_confirmed.origin_command_ids
    }
    return any(
        entry.cursor > admitted_at
        and entry.event.WhichOneof("observation") == "turn_completed"
        and entry.event.turn_completed.turn_id in turn_ids
        and entry.event.turn_completed.status == status
        for entry in entries
    )


async def _open(
    client: Client,
    dispatcher: SessionDispatcherClient,
    view: environment_service_pb2.EnvironmentDescriptor,
    harness: session_pb2.Harness,
    model: str,
) -> UUID:
    """Create a Thread whose runner feed is ready for history reads."""
    thread_id = (
        await Agent.create(
            client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=model
        )
    ).thread_id
    with tracer.start_as_current_span(
        "system_test.thread_history.feed_ready", record_exception=False, set_status_on_exception=False
    ):
        await client.wait_for_thread_feed(thread_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    return thread_id


def _input(label: str, text: str) -> command_pb2.Command:
    # Command IDs are deployment-wide. Keep retries on the returned command while
    # giving concurrent harness cases distinct identities.
    return command_pb2.Command(command_id=f"{label}-{uuid4()}", submit_input=command_pb2.SubmitInput(text=text))


async def _submit(
    dispatcher: SessionDispatcherClient,
    thread_id: UUID,
    view: environment_service_pb2.EnvironmentDescriptor,
    command: command_pb2.Command,
) -> None:
    """Submit `command`; the Dispatcher receipt names that same command."""
    with tracer.start_as_current_span(
        "system_test.thread_history.submit", record_exception=False, set_status_on_exception=False
    ):
        answer = await dispatcher.submit_input(
            str(thread_id), command.command_id, view.target.name, str(view.target.uid), command.submit_input.text
        )
    assert (answer.command_id, answer.operation) == (command.command_id, command_pb2.OPERATION_SUBMIT_INPUT)


async def _turn_status(client: Client, thread_id: UUID, status: str) -> None:
    """Wait for the Thread's summary to fold its latest turn to `status`."""
    with tracer.start_as_current_span(
        "system_test.thread_history.turn_status", record_exception=False, set_status_on_exception=False
    ):
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(REQUEST_SECONDS), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await client.thread(thread_id)).last_turn_status == status


def _ordered(entries: list[event_log_pb2.EventEntry]) -> None:
    cursors = [entry.cursor for entry in entries]
    assert cursors == sorted(set(cursors)), cursors


def _same_log(one: list[event_log_pb2.EventEntry], other: list[event_log_pb2.EventEntry]) -> None:
    """Independent reads agree on shared retained entries after settlement corrections."""
    held = {entry.cursor: entry.SerializeToString(deterministic=True) for entry in other}
    assert [entry.SerializeToString(deterministic=True) for entry in one if entry.cursor in held] == [
        held[entry.cursor] for entry in one if entry.cursor in held
    ]
    assert _retained(one) == _retained(other)


def _settleable(entry: event_log_pb2.EventEntry) -> bool:
    return entry.event.WhichOneof("observation") in _SETTLEABLE


def _retained(entries: list[event_log_pb2.EventEntry]) -> list[bytes]:
    return [entry.SerializeToString(deterministic=True) for entry in entries if not _settleable(entry)]


def _texts(entries: list[event_log_pb2.EventEntry]) -> list[str]:
    return [
        entry.event.item_completed.text
        for entry in entries
        if entry.event.WhichOneof("observation") == "item_completed"
        and entry.event.item_completed.WhichOneof("outcome") == "text"
    ]


async def an_unknown_history_session_is_not_found(client: Client) -> None:
    unknown = UUID("00000000-0000-0000-0000-000000000000")
    with pytest.raises(httpx.HTTPStatusError) as missing:
        await client.read_session_events(unknown)
    assert missing.value.response.status_code == 404


async def every_history_reader_observes_the_same_log_and_resume_uses_the_observed_cursor(
    client: Client,
    dispatcher: SessionDispatcherClient,
    view: environment_service_pb2.EnvironmentDescriptor,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """Independent readers resume at observed cursors; command IDs remain idempotent."""
    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    thread_id = await _open(client, dispatcher, view, harness, offered_model)
    first_reader = SessionEventReader(client, thread_id)
    second_reader = SessionEventReader(client, thread_id)

    command = _input("test-history-first", "Reply with exactly: HISTORY_FIRST")
    await _submit(dispatcher, thread_id, view, command)
    with pytest.raises(ServiceError) as reused:
        await dispatcher.submit_input(
            str(thread_id), command.command_id, view.target.name, str(view.target.uid), "Something else"
        )
    assert reused.value.code == grpc.StatusCode.ALREADY_EXISTS
    await _submit(dispatcher, thread_id, view, command)
    request = await model.request()
    assert request.user_texts[-1] == command.submit_input.text
    await model.reply(request, Text("HISTORY_FIRST"))

    first = await _until(
        first_reader,
        lambda _entry, prefix: _command_completed(prefix, command.command_id, event_pb2.TURN_STATUS_COMPLETED),
    )
    _ordered(first)
    terminal = first[-1]
    assert terminal.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    assert _texts(first) == ["HISTORY_FIRST"]
    assert [
        entry.event.command_admitted.command.command_id
        for entry in first
        if entry.event.WhichOneof("observation") == "command_admitted"
    ] == [command.command_id]
    assert all(entry.origin.source_id for entry in first)
    assert any(entry.event.WhichOneof("observation") == "native" for entry in first)

    replayed = await _until(
        second_reader,
        lambda _entry, prefix: _command_completed(prefix, command.command_id, event_pb2.TURN_STATUS_COMPLETED),
    )
    _same_log(first, replayed)

    next_command = _input("test-history-second", "Reply with exactly: HISTORY_SECOND")
    await _submit(dispatcher, thread_id, view, next_command)
    request = await model.request()
    assert request.user_texts[-1] == next_command.submit_input.text
    await model.reply(request, Text("HISTORY_SECOND"))
    on_first, on_second = await asyncio.gather(
        _until(
            first_reader,
            lambda _entry, prefix: _command_completed(prefix, next_command.command_id, event_pb2.TURN_STATUS_COMPLETED),
            after_cursor=terminal.cursor,
        ),
        _until(
            second_reader,
            lambda _entry, prefix: _command_completed(prefix, next_command.command_id, event_pb2.TURN_STATUS_COMPLETED),
            after_cursor=terminal.cursor,
        ),
    )
    _ordered(on_first)
    _same_log(on_first, on_second)
    assert on_first[0].cursor > terminal.cursor
    assert _texts(on_first) == ["HISTORY_SECOND"]

    # The page which contained the first completion may also have covered later entries. A fresh
    # reader resumes from that exact terminal entry, not from the page's broader coverage cursor.
    resumed_reader = SessionEventReader(client, thread_id, after_cursor=terminal.cursor)
    resumed = await _until(
        resumed_reader,
        lambda _entry, prefix: _command_completed(prefix, next_command.command_id, event_pb2.TURN_STATUS_COMPLETED),
    )
    _same_log(on_first, resumed)

    archived = await _until(
        SessionEventReader(client, thread_id),
        lambda _entry, prefix: _command_completed(prefix, next_command.command_id, event_pb2.TURN_STATUS_COMPLETED),
    )
    stable_pair = next(
        (one, other) for one, other in itertools.pairwise(archived) if not (_settleable(one) or _settleable(other))
    )
    cut, following = stable_pair
    reconnect = SessionEventReader(client, thread_id, after_cursor=cut.cursor)
    following_rows = await _until(reconnect, lambda entry, _prefix: entry.cursor == following.cursor)
    assert following_rows == [following]


async def unwatched_turns_are_archived_and_a_failed_turn_leaves_the_next_input_its_own_turn(
    client: Client,
    dispatcher: SessionDispatcherClient,
    view: environment_service_pb2.EnvironmentDescriptor,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """No history reader is open while either turn runs; the runner archives both outcomes."""
    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    thread_id = await _open(client, dispatcher, view, harness, offered_model)

    failing = _input("test-failing-input", "Reply with exactly: MODEL_HTTP_ERROR")
    await _submit(dispatcher, thread_id, view, failing)
    for _ in range(3):
        request = await model.request()
        assert request.user_texts[-1] == failing.submit_input.text
        await model.http_error(request)
    await _turn_status(client, thread_id, "TURN_STATUS_FAILED")

    failed = await _until(
        SessionEventReader(client, thread_id),
        lambda _entry, prefix: _command_completed(prefix, failing.command_id, event_pb2.TURN_STATUS_FAILED),
    )
    _ordered(failed)
    failed_terminal = failed[-1]
    assert failed_terminal.event.turn_completed.status == event_pb2.TURN_STATUS_FAILED
    assert [
        entry.event.command_admitted.command.command_id
        for entry in failed
        if entry.event.WhichOneof("observation") == "command_admitted"
    ] == [failing.command_id]
    assert any(entry.event.WhichOneof("observation") == "native" for entry in failed)

    reloaded = await _until(
        SessionEventReader(client, thread_id),
        lambda _entry, prefix: _command_completed(prefix, failing.command_id, event_pb2.TURN_STATUS_FAILED),
    )
    _same_log(failed, reloaded)

    follow_up = _input("test-follow-up-input", "Reply with exactly: AFTER_HTTP_ERROR_OK")
    await _submit(dispatcher, thread_id, view, follow_up)
    request = await model.request()
    assert request.user_texts[-1] == follow_up.submit_input.text
    await model.reply(request, Text("AFTER_HTTP_ERROR_OK"))
    await _turn_status(client, thread_id, "TURN_STATUS_COMPLETED")

    resumed = await _until(
        SessionEventReader(client, thread_id, after_cursor=failed_terminal.cursor),
        lambda _entry, prefix: _command_completed(prefix, follow_up.command_id, event_pb2.TURN_STATUS_COMPLETED),
    )
    assert resumed[-1].event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    assert resumed[-1].event.turn_completed.turn_id != failed_terminal.event.turn_completed.turn_id
    assert [
        entry.event.command_admitted.command.command_id
        for entry in resumed
        if entry.event.WhichOneof("observation") == "command_admitted"
    ] == [follow_up.command_id]
    assert _texts(resumed) == ["AFTER_HTTP_ERROR_OK"]


# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
