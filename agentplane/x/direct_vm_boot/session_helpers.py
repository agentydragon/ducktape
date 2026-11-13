"""Harness-neutral waits for History commits and a ready Runtime Session."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import grpc
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.system_tests.vm.guest_setup_probe import VM_SETUP_PROBE, assert_vm_setup_probe, setup_fence_probe


async def history_turn_completed(
    history: HistoryServiceClient, session_id: str, after_cursor: int
) -> list[event_log_pb2.EventEntry]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            try:
                response = await history.read_session_events(session_id, after_cursor=after_cursor, limit=1000)
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                entries: list[event_log_pb2.EventEntry] = []
            else:
                entries = list(response.entries)
            assert any(events.turn_completed(entry) for entry in entries), entries
            return entries
    raise AssertionError("History did not commit the Session turn")


async def wait_for_session_ready(
    gateway: GatewayProcess, client_token: Path, sandbox: SandboxCluster, session_id: str
) -> session_pb2.EnvironmentRuntimeStatus:
    remote = gateway.gateway_client(client_token, request_timeout_s=2)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            status = await remote.runner(sandbox.destination).status()
            sessions = [row for row in status.sessions if row.session_id == session_id]
            assert len(sessions) == 1, status
            assert sessions[0].execution_state != session_pb2.SESSION_EXECUTION_STATE_FAILED, sessions[0]
            assert sessions[0].execution_state == session_pb2.SESSION_EXECUTION_STATE_READY, sessions[0]
            return status
    raise AssertionError(f"Runtime did not make Session {session_id} ready")


async def _run_setup(
    dispatcher: SessionDispatcherClient, history: HistoryServiceClient, spec: session_pb2.SessionSpec, script: str
) -> tuple[str, str]:
    session_id = str(uuid4())
    await dispatcher.unary(
        dispatcher.stub.CreateSession,
        session_dispatcher_pb2.CreateSessionRequest(session_id=session_id, spec=spec, setup_script=script),
    )
    entries: list[event_log_pb2.EventEntry] = []
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            try:
                response = await history.read_session_events(session_id, after_cursor=0, limit=1000)
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                entries = []
            else:
                entries = list(response.entries)
            assert any(
                entry.event.HasField("setup_finished") or entry.event.HasField("setup_interrupted") for entry in entries
            ), entries
            break
    events.assert_contiguous(entries)
    events.assert_sourced(entries)
    finished = [entry.event.setup_finished for entry in entries if entry.event.HasField("setup_finished")]
    assert len(finished) == 1, entries
    assert finished[0].exit_code == 0, entries
    stdout = b"".join(entry.event.setup_output.stdout for entry in entries if entry.event.HasField("setup_output"))
    stderr = b"".join(entry.event.setup_output.stderr for entry in entries if entry.event.HasField("setup_output"))
    return stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def assert_guest_setup_is_supervised(
    dispatcher: SessionDispatcherClient, history: HistoryServiceClient, spec: session_pb2.SessionSpec
) -> None:
    setup_spec = session_pb2.SessionSpec()
    setup_spec.CopyFrom(spec)
    # Runtime must create and own this fresh nested workspace; the caller did not pre-create it.
    setup_spec.cwd = f"/state/workspaces/{uuid4().hex}/nested"
    stdout, stderr = await _run_setup(dispatcher, history, setup_spec, VM_SETUP_PROBE)
    assert_vm_setup_probe(stdout, stderr, cwd=setup_spec.cwd)
    checked, errors = await _run_setup(dispatcher, history, setup_spec, setup_fence_probe(stdout))
    assert checked == "SETUP_DOMAIN_EMPTY\n", (checked, errors)
    assert not errors, errors
