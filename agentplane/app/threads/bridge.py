"""Runner-first sessions and commands, each answered once the ingester has archived what it caused."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.changes import Changes
from agentplane.app.sandbox_models import SandboxKind
from agentplane.app.threads.events.event_log import EventLogStore, FeedError, ThreadNotFoundError
from agentplane.app.threads.ingestion import Ingester
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.view.content import ContentStore
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.errors import RunnerError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

COMMAND_ADMISSION_S = 15
ADMISSION_REREAD_S = 2


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class RunnerAdmissionTimeoutError(Exception):
    """Command admission was not confirmed before the deadline; its outcome is uncertain."""

    def __init__(self, command_id: str) -> None:
        super().__init__(
            f"admission of command {command_id!r} was not confirmed within {COMMAND_ADMISSION_S} seconds; outcome uncertain"
        )


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    spec: dict[str, object] = Field(
        description="Explicit proto-JSON SessionSpec fields; Sandbox-bound defaults fill omitted fields."
    )
    setup_script: str | None = Field(default=None, max_length=65_536)


class RunnerBridge:
    def __init__(
        self,
        *,
        runners: SandboxSessions,
        event_logs: EventLogStore,
        content: ContentStore,
        ingester: Ingester,
        thread_changes: Changes,
    ) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._content = content
        self._ingester = ingester
        self._thread_changes = thread_changes

    async def list_sessions(
        self, sandbox: str, sandbox_kind: SandboxKind = "agent_sandbox"
    ) -> list[protocol_pb2.SessionSummary]:
        return await self._runners.client(sandbox, sandbox_kind).list_sessions()

    async def open_session(
        self,
        sandbox: str,
        session_id: str,
        spec: protocol_pb2.SessionSpec | dict[str, object],
        setup_script: str | None = None,
        sandbox_kind: SandboxKind = "agent_sandbox",
    ) -> protocol_pb2.Attached:
        existing = await self._event_logs.find(sandbox, session_id, sandbox_kind=sandbox_kind)
        if existing is not None:
            snapshot = await self._event_logs.feed_state(existing)
            if snapshot is not None and isinstance(snapshot.end, FeedError):
                raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        try:
            attached = await self._runners.client(sandbox, sandbox_kind).open(
                session_id, MessageToDict(spec) if isinstance(spec, protocol_pb2.SessionSpec) else spec, setup_script
            )
        except (ValueError, ParseError) as error:
            raise MalformedMessageError(f"invalid session overrides: {error}") from error
        return await self._archive_open(sandbox, sandbox_kind, session_id, attached)

    async def _archive_open(
        self, sandbox: str, sandbox_kind: SandboxKind, session_id: str, attached: protocol_pb2.Attached
    ) -> protocol_pb2.Attached:
        thread_id = await self._event_logs.open(sandbox, session_id, attached.spec, sandbox_kind=sandbox_kind)
        await self._ingester.start()
        # In particular, do not return a resumed session while the database still says its
        # previous harness ended. Commands remain runner-first; this only synchronizes Open.
        waiter = asyncio.Event()
        with self._thread_changes.subscribe(waiter):
            async with asyncio.timeout(15):
                while True:
                    waiter.clear()
                    if await self._event_logs.last_cursor(thread_id) >= attached.last_cursor:
                        break
                    await waiter.wait()
        return attached

    async def resume_thread(
        self, thread_id: UUID, *, expected_harness: str, expected_cwd: str
    ) -> protocol_pb2.Attached:
        """Resume the native session already bound to this Thread, using its runner-owned spec."""
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        runner_session = await self._event_logs.runner_session(thread_id)
        if runner_session is None:
            raise ThreadNotFoundError(thread_id)
        sessions = await self.list_sessions(runner_session.sandbox, runner_session.sandbox_kind)
        summary = next((row for row in sessions if row.session_id == runner_session.session_id), None)
        if summary is None:
            raise RunnerError(
                f"runner has no retained session {runner_session.session_id!r}; this Thread cannot be resumed"
            )
        if summary.setup_state in (protocol_pb2.SETUP_STATE_FAILED, protocol_pb2.SETUP_STATE_INTERRUPTED):
            raise RunnerError("Thread setup failed or was interrupted; create a new Thread to try again")
        if (
            summary.spec.harness not in (protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX)
            or not summary.spec.cwd
            or not summary.spec.model
        ):
            raise RunnerError(f"runner session {runner_session.session_id!r} has no recoverable session spec")
        if protocol_pb2.Harness.Name(summary.spec.harness) != expected_harness or summary.spec.cwd != expected_cwd:
            raise RunnerError("runner's retained session spec does not match this Thread's harness and workspace")
        attached = await self._runners.client(runner_session.sandbox, runner_session.sandbox_kind).resume(
            runner_session.session_id
        )
        return await self._archive_open(
            runner_session.sandbox, runner_session.sandbox_kind, runner_session.session_id, attached
        )

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return only after this Thread's matching runner admission is in the app archive."""
        if admitted := await self._content.admitted_command(thread_id, command):
            return admitted
        runner_session = await self._event_logs.runner_session(thread_id)
        if runner_session is None:
            raise ThreadNotFoundError(thread_id)
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        # A runner rejection can still have followed earlier events the archive has not copied.
        # Start its feed before relaying so the rejection path cannot strand that prefix.
        await self._ingester.start()
        try:
            await self._command(
                runner_session.sandbox_kind,
                runner_session.sandbox,
                runner_session.session_id,
                command,
                after_cursor=await self._event_logs.last_cursor(thread_id),
            )
            return await self._wait_for_admission(thread_id, command)
        except TimeoutError as error:
            raise RunnerAdmissionTimeoutError(command.command_id) from error

    async def _command(
        self,
        sandbox_kind: SandboxKind,
        sandbox: str,
        session_id: str,
        command: command_pb2.Command,
        *,
        after_cursor: int,
    ) -> None:
        await self._runners.client(sandbox, sandbox_kind).command(session_id, command, after_cursor=after_cursor)

    async def _wait_for_admission(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Wait for the ingester's committed prefix, never for a native command effect."""
        waiter = asyncio.Event()
        with self._thread_changes.subscribe(waiter):
            async with asyncio.timeout(COMMAND_ADMISSION_S):
                while True:
                    if admitted := await self._content.admitted_command(thread_id, command):
                        return admitted
                    waiter.clear()
                    # A commit between the first read and clear is visible here even if its NOTIFY
                    # was already consumed; notifications only wake this durable reread.
                    if admitted := await self._content.admitted_command(thread_id, command):
                        return admitted
                    # LISTEN/NOTIFY is deliberately only a wake-up. If a notification is lost
                    # while this app is attached, a bounded durable reread still finds the
                    # committed runner admission without asking the runner to repeat it.
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(ADMISSION_REREAD_S):
                            await waiter.wait()


def _parse[M: command_pb2.Command | protocol_pb2.SessionSpec](message: M, body: dict[str, object]) -> M:
    try:
        return ParseDict(body, message)
    except ParseError as error:
        raise MalformedMessageError(f"not a {type(message).__name__}: {error}") from error


def parse_command(body: dict[str, object]) -> command_pb2.Command:
    """Decode the one generated Command shape used by the Thread command route."""
    return _parse(command_pb2.Command(), body)


router = APIRouter(prefix="/sandboxes/{name}/sessions", tags=["sessions"])


def _bridge(request: Request) -> RunnerBridge:
    bridge = request.app.state.bridge
    if not isinstance(bridge, RunnerBridge):
        raise TypeError(f"app.state.bridge is {type(bridge).__name__}, not RunnerBridge")
    return bridge


Bridge = Annotated[RunnerBridge, Depends(_bridge)]


@router.get("")
async def list_sessions(bridge: Bridge, name: str, kind: SandboxKind = "agent_sandbox") -> list[dict[str, object]]:
    return [MessageToDict(summary) for summary in await bridge.list_sessions(name, kind)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def open_session(
    bridge: Bridge, name: str, body: NewSession, kind: SandboxKind = "agent_sandbox"
) -> dict[str, object]:
    _parse(protocol_pb2.SessionSpec(), body.spec)  # Validate without losing explicit empty overrides.
    attached = await bridge.open_session(name, body.session_id, body.spec, body.setup_script, kind)
    return MessageToDict(attached)
