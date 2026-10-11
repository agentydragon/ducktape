"""Runner-first sessions and commands; commands answer from the durable runner receipt."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.ingestion import Ingester
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.summaries import FeedError
from agentplane.app.threads.view.content import ContentStore
from agentplane.proto import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.errors import RunnerError
from agentplane.runner.harness import Harness

# Not `protocol_pb2`: that name is the runner's protocol here.
from agentplane.sandbox_service import protocol_pb2 as service_pb2

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


class OpenStatus(BaseModel):
    status: Literal["absent", "unconfirmed", "failed", "ready"]
    session_id: str | None = None


class CommandState(StrEnum):
    # Retained by the Sandbox Service; it may or may not have reached the runner.
    PENDING_ADMISSION = "pending_admission"
    ADMITTED = "admitted"
    REJECTED = "rejected"


_COMMAND_STATES = {
    service_pb2.COMMAND_STATE_PENDING_ADMISSION: CommandState.PENDING_ADMISSION,
    service_pb2.COMMAND_STATE_ADMITTED: CommandState.ADMITTED,
    service_pb2.COMMAND_STATE_REJECTED: CommandState.REJECTED,
}


class CommandStatus(BaseModel):
    state: CommandState
    admission: dict[str, object] | None = Field(
        default=None, description="The runner's CommandAdmitted EventEntry as proto-JSON, once admitted."
    )
    rejection: str | None = Field(default=None, description="Why the runner refused the command.")


class RunnerBridge:
    def __init__(
        self, *, runners: SandboxSessions, event_logs: EventLogStore, content: ContentStore, ingester: Ingester
    ) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._content = content
        self._ingester = ingester

    async def list_sessions(self, sandbox: str) -> list[protocol_pb2.SessionSummary]:
        return await self._runners.client(sandbox).list_sessions()

    async def create_session(
        self, sandbox: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> protocol_pb2.Attached:
        """Use the service's public ID; the app only materializes its Thread projection."""
        try:
            created = await self._runners.client(sandbox).create(
                session_id=session_id, spec=spec, setup_script=setup_script
            )
        except (ValueError, ParseError) as error:
            raise MalformedMessageError(f"invalid session overrides: {error}") from error
        public_id = created.session_id
        thread_id = await self._event_logs.open(sandbox, public_id)
        if thread_id != UUID(public_id):
            raise RunnerError("service Session ID conflicts with the retained Thread identity")
        await self._event_logs.resume_pending(thread_id)
        await self._ingester.start()
        attached = protocol_pb2.Attached()
        attached.CopyFrom(created.attached)
        return attached

    async def lookup_session(self, sandbox: str, session_id: UUID) -> OpenStatus:
        """Reconcile a lost Open without replaying its potentially secret-bearing inputs.

        A reservation is not proof the runner accepted Open; only inventory permits a
        Thread projection. Never return the summary (which includes the frozen spec).
        """
        # The create sent the Session ID as its Open key too.
        result = await self._runners.client(sandbox).lookup(idempotency_key=str(session_id))
        if not result.session_id:
            return OpenStatus(status="absent")
        if result.failed:
            return OpenStatus(status="failed", session_id=result.session_id)
        if not result.HasField("summary"):
            return OpenStatus(status="unconfirmed", session_id=result.session_id)
        thread_id = await self._event_logs.open(sandbox, result.session_id)
        if thread_id != UUID(result.session_id):
            raise RunnerError("service Session ID conflicts with the retained Thread identity")
        await self._event_logs.resume_pending(thread_id)
        await self._ingester.start()
        return OpenStatus(status="ready", session_id=result.session_id)

    async def resume_thread(
        self, thread_id: UUID, *, expected_harness: Harness | None, expected_cwd: str | None
    ) -> protocol_pb2.Attached:
        """Resume the native session already bound to this Thread, using its runner-owned spec.

        The expectations are the Thread's view of its harness and workspace; None, before any
        runner attachment is folded, leaves nothing to check the retained spec against.
        """
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        runner_session = await self._event_logs.service_session(thread_id)
        if runner_session is None:
            raise ThreadNotFoundError(thread_id)
        sessions = await self.list_sessions(runner_session.sandbox)
        summary = next((row for row in sessions if row.session_id == str(thread_id)), None)
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
        if (expected_harness is not None and protocol_pb2.Harness.Name(summary.spec.harness) != expected_harness) or (
            expected_cwd is not None and summary.spec.cwd != expected_cwd
        ):
            raise RunnerError("runner's retained session spec does not match this Thread's harness and workspace")
        attached = await self._runners.client(runner_session.sandbox).resume(runner_session.session_id)
        # The Thread mapping already exists. Do not make runner Resume depend on archive
        # catch-up; its feed exposes that later, and a stale view remains non-authoritative.
        await self._event_logs.resume_pending(thread_id)
        await self._ingester.start()
        return attached

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return the runner's durable receipt; the app archive can catch up later.

        An exact retry already in the archive needs no runner, including when the Sandbox is
        gone. Otherwise the runner deduplicates the unchanged id/payload in its journal.
        """
        if admitted := await self._content.admitted_command(thread_id, command):
            return admitted
        runner_session = await self._event_logs.service_session(thread_id)
        if runner_session is None:
            raise ThreadNotFoundError(thread_id)
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise RunnerError(f"runner history is rejected: {snapshot.end.message}")
        # Start ingestion before relaying, so even an immediate runner rejection cannot strand
        # the prefix preceding it. The response does not wait for that copy.
        await self._ingester.start()
        try:
            return await self._command(
                runner_session.sandbox,
                runner_session.session_id,
                command,
                after_cursor=await self._event_logs.last_cursor(thread_id),
            )
        except TimeoutError as error:
            raise RunnerAdmissionTimeoutError(command.command_id) from error

    async def command_status(self, thread_id: UUID, command_id: str) -> CommandStatus:
        """What the Sandbox Service retained for the command, without contacting the runner."""
        runner_session = await self._event_logs.service_session(thread_id)
        if runner_session is None:
            raise ThreadNotFoundError(thread_id)
        status = await self._runners.client(runner_session.sandbox).get_command(runner_session.session_id, command_id)
        return CommandStatus(
            state=_COMMAND_STATES[status.state],
            admission=MessageToDict(status.admission) if status.HasField("admission") else None,
            rejection=status.rejection if status.HasField("rejection") else None,
        )

    async def _command(
        self, sandbox: str, session_id: str, command: command_pb2.Command, *, after_cursor: int
    ) -> event_log_pb2.EventEntry:
        return await self._runners.client(sandbox).command(session_id, command, after_cursor=after_cursor)


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
async def list_sessions(bridge: Bridge, name: str) -> list[dict[str, object]]:
    return [MessageToDict(summary) for summary in await bridge.list_sessions(name)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(bridge: Bridge, name: str, body: NewSession) -> dict[str, object]:
    _parse(protocol_pb2.SessionSpec(), body.spec)  # Validate without losing explicit empty overrides.
    return MessageToDict(await bridge.create_session(name, body.session_id, body.spec, body.setup_script))


@router.get("/open", response_model=OpenStatus)
async def lookup_session(bridge: Bridge, name: str, session_id: UUID) -> OpenStatus:
    return await bridge.lookup_session(name, session_id)
