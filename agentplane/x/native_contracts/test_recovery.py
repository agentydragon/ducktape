"""Bounded contract pilot: real Gateway, Runtime, History and durable stores."""

import asyncio
import os
import shutil
import signal
from collections.abc import Awaitable, Callable, Iterator
from contextlib import aclosing, contextmanager
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events
from agentplane.env.e2e.testing.claude_transcript import wait_for_claude_transcript
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, Text

# gazelle:include_dep @pypi//protobuf


async def recorded(
    history: GoHistoryService,
    session_id: str,
    after: int,
    predicate: Callable[[event_log_pb2.EventEntry], bool] = events.turn_completed,
) -> list[event_log_pb2.EventEntry]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            entries = list(
                (await history.reader.read_session_events(session_id, after_cursor=after, limit=1000)).entries
            )
            assert any(predicate(entry) for entry in entries)
            return entries
    raise AssertionError("History did not commit turn completion")


async def test_runtime_transport_outage_retains_history_and_accepts_next_input(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    client_token: Path,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    config: RunnerTestConfig,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    remote = gateway_service.gateway_client(client_token, request_timeout_s=2)
    session_id = uuid4()
    process = await start_runtime()
    await dispatcher.unary(
        dispatcher.stub.CreateSession,
        session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec),
    )
    await accepted_session(dispatcher, session_id)
    first_id = str(uuid4())
    first = await dispatcher.submit_input(str(session_id), first_id, sandbox.name, sandbox.uid, "Reply FIRST")
    first_receipt = (await admitted(dispatcher, first)).admission
    await model.reply(await model.request(), Text("FIRST"))
    before = await recorded(history_service, str(session_id), first_receipt.cursor - 1)

    # Remove the real Runtime listener. Retained History is independent of it.
    await process.stop()
    with pytest.raises(ServiceError) as unavailable:
        await remote.runner(sandbox.destination).status()
    assert unavailable.value.code == grpc.StatusCode.UNAVAILABLE
    # Shutdown may append later harness events; the captured prefix must remain exact.
    retained = await history_service.reader.read_session_events(
        str(session_id), after_cursor=first_receipt.cursor - 1, limit=len(before)
    )
    assert list(retained.entries) == before

    if os.environ.get("NATIVE_CONTRACT_DISCARD_RUNTIME_JOURNAL") == "1":
        shutil.rmtree(config.state_dir)
    await start_runtime()
    assert str(session_id) in {row.session_id for row in await remote.list_sessions(sandbox.destination)}
    # This is Dispatcher receipt replay; the separate native admission case bypasses it.
    replay = await dispatcher.submit_input(str(session_id), first_id, sandbox.name, sandbox.uid, "Reply FIRST")
    assert (await admitted(dispatcher, replay)).admission == first_receipt
    second_id = str(uuid4())
    second = await dispatcher.submit_input(str(session_id), second_id, sandbox.name, sandbox.uid, "Reply SECOND")
    second_receipt = (await admitted(dispatcher, second)).admission
    await model.reply(await model.request(), Text("SECOND"))
    after = await recorded(history_service, str(session_id), second_receipt.cursor - 1)
    all_entries = list((await history_service.reader.read_session_events(str(session_id), limit=1000)).entries)
    assert [
        row.event.command_admitted.command.command_id for row in events.of_kind(all_entries, "command_admitted")
    ] == [first_id, second_id]
    assert len(events.of_kind(all_entries, "turn_completed")) == 2
    assert any(events.turn_completed(entry) for entry in after)


async def create_session(dispatcher: SessionDispatcherClient, spec: session_pb2.SessionSpec) -> str:
    session_id = uuid4()
    await dispatcher.unary(
        dispatcher.stub.CreateSession,
        session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec),
    )
    await accepted_session(dispatcher, session_id)
    return str(session_id)


async def runtime_ready(remote: SandboxControlClient, sandbox: SandboxCluster, session_id: str) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            status = await remote.runner(sandbox.destination).status()
            session = next(row for row in status.sessions if row.session_id == session_id)
            assert session.execution_state != session_pb2.SESSION_EXECUTION_STATE_FAILED, session
            assert session.execution_state == session_pb2.SESSION_EXECUTION_STATE_READY


async def test_runtime_admission_is_idempotent_after_restart_without_dispatcher_replay(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    client_token: Path,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    runtime_client: SandboxControlClient,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    process = await start_runtime()
    session_id = await create_session(dispatcher, spec)
    remote = runtime_client
    command_id = str(uuid4())
    request = environment_runtime_pb2.SubmitInputRequest(
        session_id=session_id, command_id=command_id, text="Reply ORIGINAL"
    )
    with pytest.raises(ServiceError) as unauthorized:
        await gateway_service.gateway_client(client_token).runner_unary(
            sandbox.destination, lambda stub: stub.SubmitInput, request
        )
    assert unauthorized.value.code == grpc.StatusCode.PERMISSION_DENIED
    # Both calls reach the Runtime RPC. No Dispatcher SubmitInput row can answer them.
    first = await remote.runner_unary(sandbox.destination, lambda stub: stub.SubmitInput, request)
    duplicate = await remote.runner_unary(sandbox.destination, lambda stub: stub.SubmitInput, request)
    assert duplicate.admission == first.admission
    await model.reply(await model.request(), Text("ORIGINAL"))
    await recorded(history_service, session_id, first.admission.cursor - 1)
    await process.stop()
    await start_runtime()
    replay = await remote.runner_unary(sandbox.destination, lambda stub: stub.SubmitInput, request)
    assert replay.admission == first.admission
    with pytest.raises(ServiceError) as payload_collision:
        await remote.runner_unary(
            sandbox.destination,
            lambda stub: stub.SubmitInput,
            environment_runtime_pb2.SubmitInputRequest(session_id=session_id, command_id=command_id, text="DIFFERENT"),
        )
    assert payload_collision.value.code == grpc.StatusCode.ALREADY_EXISTS
    with pytest.raises(ServiceError) as operation_collision:
        await remote.runner_unary(
            sandbox.destination,
            lambda stub: stub.ChangeModel,
            environment_runtime_pb2.ChangeModelRequest(session_id=session_id, command_id=command_id, model="DIFFERENT"),
        )
    assert operation_collision.value.code == grpc.StatusCode.ALREADY_EXISTS
    next_id = str(uuid4())
    next_dispatch = await dispatcher.submit_input(session_id, next_id, sandbox.name, sandbox.uid, "Reply NEXT")
    next_admission = (await admitted(dispatcher, next_dispatch)).admission
    next_request = await model.request()
    assert next_request.user_texts == ["Reply ORIGINAL", "Reply NEXT"]
    await model.reply(next_request, Text("NEXT"))
    await recorded(history_service, session_id, next_admission.cursor - 1)
    entries = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    assert [row.event.command_admitted.command.command_id for row in events.of_kind(entries, "command_admitted")] == [
        command_id,
        next_id,
    ]
    assert len(events.of_kind(entries, "turn_completed")) == 2
    events.assert_contiguous(entries)


async def test_active_runtime_crash_preserves_history_and_does_not_resubmit_the_turn(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    client_token: Path,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    config: RunnerTestConfig,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    process = await start_runtime()
    session_id = await create_session(dispatcher, spec)
    seed_id, active_id, next_id = (str(uuid4()) for _ in range(3))
    seed = await dispatcher.submit_input(session_id, seed_id, sandbox.name, sandbox.uid, "Reply SEED")
    seed_receipt = (await admitted(dispatcher, seed)).admission
    await model.reply(await model.request(), Text("SEED"))
    seed_entries = await recorded(history_service, session_id, seed_receipt.cursor - 1)
    await wait_for_claude_transcript(spec, session_id, seed_entries, config.state_dir, "SEED")
    active = await dispatcher.submit_input(session_id, active_id, sandbox.name, sandbox.uid, "Reply ACTIVE")
    active_receipt = (await admitted(dispatcher, active)).admission
    active_request = await model.request()
    assert active_request.user_texts[-1] == "Reply ACTIVE"
    await model.hold(active_request)
    active_entries = await recorded(
        history_service, session_id, active_receipt.cursor - 1, events.is_kind("harness_user_message_confirmed")
    )
    retained = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    active_turn = events.of_kind(active_entries, "harness_user_message_confirmed")[
        -1
    ].event.harness_user_message_confirmed.turn_id
    harness_pids = [row.event.harness_started.pid for row in events.of_kind(retained, "harness_started")]
    assert harness_pids
    await process.crash(harness_pids)
    assert list((await history_service.reader.read_session_events(session_id, limit=1000)).entries) == retained
    await start_runtime()
    remote = gateway_service.gateway_client(client_token)
    await runtime_ready(remote, sandbox, session_id)
    recovered = await recorded(
        history_service,
        session_id,
        retained[-1].cursor,
        lambda row: (
            events.kind(row) == "conversation_reconciled" and row.event.conversation_reconciled.turn_id == active_turn
        ),
    )
    assert events.of_kind(recovered, "harness_lost")
    fresh = await dispatcher.submit_input(session_id, next_id, sandbox.name, sandbox.uid, "Reply NEXT")
    fresh_receipt = (await admitted(dispatcher, fresh)).admission
    fresh_request = await model.request()
    assert fresh_request.user_texts[-1] == "Reply NEXT"
    await model.reply(fresh_request, Text("NEXT"))
    await recorded(history_service, session_id, fresh_receipt.cursor - 1)
    entries = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    assert entries[: len(retained)] == retained
    assert [row.event.command_admitted.command.command_id for row in events.of_kind(entries, "command_admitted")] == [
        seed_id,
        active_id,
        next_id,
    ]
    confirmations = [
        command_id
        for row in events.of_kind(entries, "harness_user_message_confirmed")
        for command_id in row.event.harness_user_message_confirmed.origin_command_ids
    ]
    assert confirmations == [seed_id, active_id, next_id]
    assert len(events.of_kind(entries, "turn_completed")) == 3
    assert events.of_kind(entries, "harness_started")[-1].event.harness_started.resumed
    assert model.request_count == 3
    events.assert_contiguous(entries)


@contextmanager
def paused(process: asyncio.subprocess.Process) -> Iterator[None]:
    process.send_signal(signal.SIGSTOP)
    try:
        yield
    finally:
        process.send_signal(signal.SIGCONT)


async def test_history_outage_replays_the_runtime_journal_and_resumes_the_reader_cursor(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    client_token: Path,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    await start_runtime()
    session_id = await create_session(dispatcher, spec)
    first_id, second_id = (str(uuid4()) for _ in range(2))
    first = await dispatcher.submit_input(session_id, first_id, sandbox.name, sandbox.uid, "Reply FIRST")
    first_receipt = (await admitted(dispatcher, first)).admission
    await model.reply(await model.request(), Text("FIRST"))
    await recorded(history_service, session_id, first_receipt.cursor - 1)
    prefix = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    second = await dispatcher.submit_input(session_id, second_id, sandbox.name, sandbox.uid, "Reply OFFLINE")
    second_receipt = (await admitted(dispatcher, second)).admission
    request = await model.request()
    remote = gateway_service.gateway_client(client_token)
    async with HistoryServiceClient(
        history_service.read_target,
        token_file=history_service.reader_token_file,
        request_timeout_s=0.5,
        channel_credentials=history_service.reader.channel_credentials,
    ) as reader:
        with paused(history_service.process.process):
            with pytest.raises(TimeoutError):
                await reader.read_session_events(session_id)
            await model.reply(request, Text("OFFLINE"))
            # Observe completion at Runtime while History cannot serve or acknowledge it.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(20),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    status = await remote.runner(sandbox.destination).status()
                    session = next(row for row in status.sessions if row.session_id == session_id)
                    assert not session.active_turn_id
                    assert not session.busy
        await recorded(history_service, session_id, second_receipt.cursor - 1)
        resumed: list[event_log_pb2.EventEntry] = []
        async with (
            asyncio.timeout(20),
            aclosing(reader.follow_session(session_id, after_cursor=prefix[-1].cursor)) as stream,
        ):
            async for frame in stream:
                if frame.HasField("entries"):
                    resumed.extend(frame.entries.entries)
                if frame.HasField("caught_up"):
                    break
        complete = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
        assert [*prefix, *resumed] == complete
        assert [
            row.event.command_admitted.command.command_id for row in events.of_kind(complete, "command_admitted")
        ] == [first_id, second_id]
        assert len(events.of_kind(complete, "turn_completed")) == 2
        events.assert_contiguous(complete)

        third_id = str(uuid4())
        third = await dispatcher.submit_input(session_id, third_id, sandbox.name, sandbox.uid, "Reply AFTER HISTORY")
        third_receipt = (await admitted(dispatcher, third)).admission
        next_request = await model.request()
        assert next_request.user_texts == ["Reply FIRST", "Reply OFFLINE", "Reply AFTER HISTORY"]
        await model.reply(next_request, Text("AFTER HISTORY"))
        await recorded(history_service, session_id, third_receipt.cursor - 1)

    final_entries = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    assert [*prefix, *resumed] == complete
    assert [
        row.event.command_admitted.command.command_id for row in events.of_kind(final_entries, "command_admitted")
    ] == [first_id, second_id, third_id]
    assert len(events.of_kind(final_entries, "turn_completed")) == 3
    assert model.request_count == 3
    events.assert_contiguous(final_entries)


if __name__ == "__main__":
    pytest_bazel.main()
