"""Runner-first sessions and commands; commands answer from the durable runner receipt."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.summaries import FeedError
from agentplane.app.threads.view.content import ContentStore

# Not `runner_pb2`: that name is the runner's protocol here.
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2, sandbox_service_pb2
from agentplane.runner.errors import RunnerError
from agentplane.runner.harness import Harness

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class RunnerAdmissionTimeoutError(Exception):
    """Command admission was not confirmed before the deadline; its outcome is uncertain."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"runner admission of command {command_id!r} was not confirmed; outcome uncertain")


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, object] = Field(
        description="Explicit proto-JSON SessionSpec fields; Sandbox-bound defaults fill omitted fields."
    )
    session_id: UUID = Field(
        description="A fresh UUID the client minted for this Session; a retry of the same Open repeats it."
    )
    setup_script: str | None = Field(default=None, max_length=65_536)


class CommandState(StrEnum):
    # Retained by the Runner Gateway; it may or may not have reached the runner.
    PENDING_ADMISSION = "pending_admission"
    ADMITTED = "admitted"
    REJECTED = "rejected"


_COMMAND_STATES = {
    sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION: CommandState.PENDING_ADMISSION,
    sandbox_service_pb2.COMMAND_STATE_ADMITTED: CommandState.ADMITTED,
    sandbox_service_pb2.COMMAND_STATE_REJECTED: CommandState.REJECTED,
}


class CommandStatus(BaseModel):
    state: CommandState
    admission: dict[str, object] | None = Field(
        default=None, description="The runner's CommandAdmitted EventEntry as proto-JSON, once admitted."
    )
    rejection: str | None = Field(default=None, description="Why the runner refused the command.")


class RunnerBridge:
    def __init__(self, *, runners: SandboxSessions, event_logs: EventLogStore, content: ContentStore) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._content = content

    async def list_sessions(self, sandbox: str) -> list[runner_pb2.SessionSummary]:
        return await self._runners.client(sandbox).list_sessions()

    async def create_session(
        self, sandbox: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        """Use the service's public ID; the app only materializes its Thread projection."""
        try:
            created = await self._runners.client(sandbox).create(
                session_id=session_id, spec=spec, setup_script=setup_script
            )
        except (ValueError, ParseError) as error:
            raise MalformedMessageError(f"invalid session overrides: {error}") from error
        public_id = created.session_id
        thread_id = await self._event_logs.open(public_id)
        if thread_id != UUID(public_id):
            raise RunnerError("service Session ID conflicts with the retained Thread identity")
        await self._event_logs.resume_pending(thread_id)
        attached = runner_pb2.Attached()
        attached.CopyFrom(created.attached)
        return attached

    async def resume_thread(
        self, thread_id: UUID, *, expected_harness: Harness | None, expected_cwd: str | None
    ) -> runner_pb2.Attached:
        """Resume the native session already bound to this Thread, using its runner-owned spec.

        The expectations are the Thread's view of its harness and workspace; None, before any
        runner attachment is folded, leaves nothing to check the retained spec against.
        """
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        sandbox = await self._session_sandbox(thread_id)
        session_id = str(thread_id)
        sessions = await self.list_sessions(sandbox)
        summary = next((row for row in sessions if row.session_id == session_id), None)
        if summary is None:
            raise RunnerError(f"runner has no retained session {session_id!r}; this Thread cannot be resumed")
        if summary.setup_state in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED):
            raise RunnerError("Thread setup failed or was interrupted; create a new Thread to try again")
        if (
            summary.spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX)
            or not summary.spec.cwd
            or not summary.spec.model
        ):
            raise RunnerError(f"runner session {session_id!r} has no recoverable session spec")
        if (expected_harness is not None and runner_pb2.Harness.Name(summary.spec.harness) != expected_harness) or (
            expected_cwd is not None and summary.spec.cwd != expected_cwd
        ):
            raise RunnerError("runner's retained session spec does not match this Thread's harness and workspace")
        attached = await self._runners.client(sandbox).resume(session_id)
        # The Thread mapping already exists. Do not make runner Resume depend on archive
        # catch-up; its feed exposes that later, and a stale view remains non-authoritative.
        await self._event_logs.resume_pending(thread_id)
        return attached

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return the runner's durable receipt; the app archive can catch up later.

        An exact retry already in the archive needs no runner, including when the Sandbox is
        gone. Otherwise the runner deduplicates the unchanged id/payload in its journal.
        """
        if admitted := await self._content.admitted_command(thread_id, command):
            return admitted
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        try:
            return await self._runners.gateway.command(str(thread_id), command)
        except TimeoutError as error:
            raise RunnerAdmissionTimeoutError(command.command_id) from error

    async def command_status(self, thread_id: UUID, command_id: str) -> CommandStatus:
        """What the Runner Gateway retained for the command, without contacting the runner."""
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        status = await self._runners.gateway.get_command(str(thread_id), command_id)
        return CommandStatus(
            state=_COMMAND_STATES[status.state],
            admission=MessageToDict(status.admission) if status.HasField("admission") else None,
            rejection=status.rejection if status.HasField("rejection") else None,
        )

    async def _session_sandbox(self, thread_id: UUID) -> str:
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        return await self._runners.sandbox_of(thread_id)


def _parse[M: command_pb2.Command | runner_pb2.SessionSpec](message: M, body: dict[str, object]) -> M:
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
async def list_sessions(bridge: Bridge, name: str) -> list[dict[str, object]]:
    return [MessageToDict(summary) for summary in await bridge.list_sessions(name)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(bridge: Bridge, name: str, body: NewSession) -> dict[str, object]:
    _parse(runner_pb2.SessionSpec(), body.spec)  # Validate without losing explicit empty overrides.
    return MessageToDict(await bridge.create_session(name, body.session_id, body.spec, body.setup_script))
