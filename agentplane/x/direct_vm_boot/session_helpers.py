"""Harness-neutral waits for History commits and a ready Runtime Session."""

from __future__ import annotations

from pathlib import Path

import grpc
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster


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
