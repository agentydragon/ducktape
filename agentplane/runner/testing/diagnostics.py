"""Bounded, content-free diagnostics for runner command admission tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import grpc

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.service import Runner

logger = logging.getLogger(__name__)

SAFE_LOG_TEMPLATES = {
    "session %s: command failed": "runner_command_failed",
    "session %s: open failed": "runner_open_failed",
    "notification source retry: inbox=%s subscription=%s cause=%s retry_seconds=%s": "notification_source_retry",
    "retired stale notification inbox: inbox=%s reason=%s": "notification_inbox_retired",
    "notification delivery unavailable: inbox=%s cause=%s": "notification_delivery_unavailable",
    "notification storage unavailable; retrying": "notification_storage_retry",
}


@dataclass
class AdmissionAttempt:
    phase: str
    progress: str
    session_id: str
    command_id: str
    operation: str | None
    after_cursor: int
    result: str = "pending"
    admitted_cursor: int | None = None
    error_type: str | None = None
    grpc_status: str | None = None
    mutation_outcome: str | None = None


class AdmissionTrace:
    def __init__(self) -> None:
        self.phase = "test setup"
        self.progress = "starting"
        self.attempts: list[AdmissionAttempt] = []
        self.client_responses: list[dict[str, str | int]] = []

    async def command(
        self,
        session_id: str,
        command: command_pb2.Command,
        *,
        after_cursor: int,
        send: Callable[[], Awaitable[event_log_pb2.EventEntry]],
    ) -> event_log_pb2.EventEntry:
        attempt = AdmissionAttempt(
            phase=self.phase,
            progress=self.progress,
            session_id=session_id,
            command_id=command.command_id,
            operation=command.WhichOneof("operation"),
            after_cursor=after_cursor,
        )
        self.attempts.append(attempt)
        try:
            receipt = await send()
        except BaseException as error:
            attempt.result = "raised"
            attempt.error_type = type(error).__name__
            cause: BaseException | None = error
            while cause is not None:
                if isinstance(cause, grpc.aio.AioRpcError):
                    attempt.grpc_status = cause.code().name
                    break
                cause = cause.__cause__
            if attempt.grpc_status == "DEADLINE_EXCEEDED" or isinstance(error, (TimeoutError, ConnectionError)):
                attempt.mutation_outcome = "uncertain"
            raise
        attempt.result = "admitted"
        attempt.admitted_cursor = receipt.cursor
        return receipt


def case_id(nodeid: str) -> str:
    return hashlib.sha256(nodeid.encode()).hexdigest()[:16]


def service_log_evidence(records: Iterable[logging.LogRecord]) -> list[dict[str, str]]:
    return [
        {"logger": record.name, "level": record.levelname, "event": SAFE_LOG_TEMPLATES[record.msg]}
        for record in records
        if (
            record.name in {"agentplane.runner.service", "agentplane.notification_service.service"}
            and isinstance(record.msg, str)
            and record.msg in SAFE_LOG_TEMPLATES
        )
    ][-512:]


def write_admission_diagnostics(
    output_dir: Path,
    *,
    nodeid: str,
    attempts: list[AdmissionAttempt],
    client_responses: list[dict[str, str | int]],
    service_logs: list[dict[str, str]],
    native_sessions: list[dict[str, object]],
    capture_errors: list[str],
    trigger: str,
) -> Path:
    """Write only identifiers, operation metadata, status classes, cursors, and safe log templates."""
    target = output_dir / f"agentplane-admission-{case_id(nodeid)}.json"
    target.write_text(
        json.dumps(
            {
                "test": nodeid,
                "trigger": trigger,
                "attempts": [asdict(attempt) for attempt in attempts],
                "client_responses": client_responses,
                "service_logs": service_logs,
                "service_log_scope": (
                    "allowlisted Python service message templates only; native process stdout/stderr are not captured; "
                    "empty means no allowed event was observed"
                ),
                "native_sessions": native_sessions,
                "capture_errors": capture_errors,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return target


def best_effort_write_admission_diagnostics(
    output_dir: Path,
    *,
    nodeid: str,
    attempts: list[AdmissionAttempt],
    client_responses: list[dict[str, str | int]],
    service_logs: list[dict[str, str]],
    native_sessions: list[dict[str, object]],
    capture_errors: list[str],
    trigger: str,
) -> Path | None:
    try:
        return write_admission_diagnostics(
            output_dir,
            nodeid=nodeid,
            attempts=attempts,
            client_responses=client_responses,
            service_logs=service_logs,
            native_sessions=native_sessions,
            capture_errors=capture_errors,
            trigger=trigger,
        )
    except Exception as error:
        logger.warning("admission diagnostics capture failed: %s", type(error).__name__)
        return None


async def native_journal_evidence(runner: Runner, *, limit: int = 512) -> list[dict[str, object]]:
    """Capture a bounded journal tail without command or native output content."""
    sessions: list[dict[str, object]] = []
    async with asyncio.timeout(2):
        for session_id, session in runner.sessions.items():
            first_cursor = max(0, session.journal.last_cursor - limit)
            entries = await session.journal.since(first_cursor, limit=limit)
            events: list[dict[str, str | int]] = []
            for entry in entries:
                kind = entry.event.WhichOneof("observation") or "unknown"
                event: dict[str, str | int] = {"cursor": entry.cursor, "kind": kind}
                match kind:
                    case "command_admitted":
                        command = entry.event.command_admitted.command
                        event["command_id"] = command.command_id
                        event["operation"] = command.WhichOneof("operation") or "unknown"
                    case "command_failed" | "command_noop":
                        if kind == "command_failed":
                            event["command_id"] = entry.event.command_failed.command_id
                        else:
                            event["command_id"] = entry.event.command_noop.command_id
                    case "model_changed":
                        event["command_id"] = entry.event.model_changed.command_id
                    case "reasoning_effort_changed":
                        event["command_id"] = entry.event.reasoning_effort_changed.command_id
                events.append(event)
            sessions.append(
                {
                    "session_id": session_id,
                    "last_cursor": session.journal.last_cursor,
                    "truncated": first_cursor > 0,
                    "harness_running": session.harness_running,
                    "process_running": session.running,
                    "active_turn_id": session.active_turn_id,
                    "setup_state": protocol_pb2.SetupState.Name(session.setup_state),
                    "events": events,
                }
            )
    return sessions
