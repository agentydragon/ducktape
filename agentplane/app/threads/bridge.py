"""Runner sessions and Dispatcher-backed commands."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.sandbox_models import sandbox_view
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.summaries import FeedError
from agentplane.app.threads.view.content import ContentStore

# Not `runner_pb2`: that name is the runner's protocol here.
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.runner.harness import Harness

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class ThreadStateError(Exception):
    """The Thread's history or its runner Session's retained state refuses the operation."""


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class DispatchNotFoundError(Exception):
    """The dispatch does not belong to the requested Thread."""


class SessionOpeningNotFoundError(Exception):
    """The opening is not for the requested Sandbox."""


class DispatcherRequestTimeoutError(Exception):
    """The Dispatcher did not confirm a durable result before the deadline; retry the same ID."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"Dispatcher result for command {command_id!r} was not confirmed; outcome uncertain")


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, object] = Field(
        description="Explicit proto-JSON SessionSpec fields; configured preset defaults fill omitted fields."
    )
    session_id: UUID = Field(
        description="A fresh UUID the client minted for this Session; a retry of the same Open repeats it."
    )
    setup_script: str | None = Field(default=None, max_length=65_536)


class RunnerBridge:
    def __init__(
        self,
        *,
        runners: RunnerDirectory,
        event_logs: EventLogStore,
        content: ContentStore,
        dispatcher: SessionDispatcherClient,
    ) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._content = content
        self._dispatcher = dispatcher

    async def list_sessions(self, sandbox: str) -> list[runner_pb2.SessionSummary]:
        return await self._runners.client(sandbox).list_sessions()

    async def create_session(
        self, sandbox: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
        """Queue a complete Session spec with the Dispatcher."""
        return await self._dispatcher.open_session(
            sandbox=sandbox, session_id=str(session_id), spec=spec, setup_script=setup_script
        )

    async def session_opening(self, sandbox: str, session_id: UUID) -> session_dispatcher_pb2.SessionOpening:
        opening = await self._dispatcher.get_session_opening(str(session_id))
        if opening.sandbox != sandbox:
            raise SessionOpeningNotFoundError(str(session_id))
        return opening

    async def resume_thread(
        self, thread_id: UUID, *, expected_harness: Harness | None, expected_cwd: str | None
    ) -> runner_pb2.Attached:
        """Resume the native session already bound to this Thread, using its runner-owned spec.

        The expectations are the Thread's view of its harness and workspace; None, before any
        runner attachment is folded, leaves nothing to check the retained spec against.
        """
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise ThreadStateError(f"runner history is rejected: {snapshot.end.message}")
        sandbox = await self._session_sandbox(thread_id)
        session_id = str(thread_id)
        sessions = await self.list_sessions(sandbox)
        summary = next((row for row in sessions if row.session_id == session_id), None)
        if summary is None:
            raise ThreadStateError(f"runner has no retained session {session_id!r}; this Thread cannot be resumed")
        if summary.setup_state in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED):
            raise ThreadStateError("Thread setup failed or was interrupted; create a new Thread to try again")
        if (
            summary.spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX)
            or not summary.spec.cwd
            or not summary.spec.model
        ):
            raise ThreadStateError(f"runner session {session_id!r} has no recoverable session spec")
        if (expected_harness is not None and runner_pb2.Harness.Name(summary.spec.harness) != expected_harness) or (
            expected_cwd is not None and summary.spec.cwd != expected_cwd
        ):
            raise ThreadStateError("runner's retained session spec does not match this Thread's harness and workspace")
        # The Thread mapping already exists. Do not make runner Resume depend on archive
        # catch-up; its feed exposes that later, and a stale view remains non-authoritative.
        return await self._runners.client(sandbox).resume(session_id)

    async def command(
        self, thread_id: UUID, command: command_pb2.Command, *, sandbox: str | None = None
    ) -> event_log_pb2.EventEntry | session_dispatcher_pb2.Dispatch:
        """Return a Dispatcher receipt, or an archived admission for an exact retry.

        An exact retry already in the archive needs no runner, including when the Sandbox is
        gone. Otherwise the Dispatcher stores a durable queue entry or immediately delivers an
        interrupt. A retry keeps the same immutable command ID and payload if the response is lost.
        """
        if admitted := await self._content.admitted_command(thread_id, command):
            return admitted
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise ThreadStateError(f"runner history is rejected: {snapshot.end.message}")
        target_sandbox = sandbox if sandbox is not None else await self._runners.sandbox_of(thread_id)
        try:
            return await self._dispatcher.command(sandbox=target_sandbox, session_id=str(thread_id), command=command)
        except TimeoutError as error:
            raise DispatcherRequestTimeoutError(command.command_id) from error

    async def dispatch(self, thread_id: UUID, command_id: str) -> session_dispatcher_pb2.Dispatch:
        """Read only this Thread's dispatch; a guessed ID for another Session stays hidden."""
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        dispatch = await self._dispatcher.get_dispatch(command_id)
        if dispatch.session_id != str(thread_id):
            raise DispatchNotFoundError(command_id)
        return dispatch

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
    bridge = request.app.state.services.bridge
    if not isinstance(bridge, RunnerBridge):
        raise TypeError(f"app.state.services.bridge is {type(bridge).__name__}, not RunnerBridge")
    return bridge


Bridge = Annotated[RunnerBridge, Depends(_bridge)]


@router.get("")
async def list_sessions(bridge: Bridge, name: str) -> list[dict[str, object]]:
    return [MessageToDict(summary) for summary in await bridge.list_sessions(name)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(request: Request, bridge: Bridge, name: str, body: NewSession) -> dict[str, object]:
    services = request.app.state.services
    sandbox = sandbox_view(await services.inventory.get(name))
    try:
        defaults = services.presets.defaults_for_sandbox(sandbox.preset)
    except KeyError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    spec = _parse(
        runner_pb2.SessionSpec(), defaults.model_dump(mode="json", exclude_none=True, exclude={"setup_script"})
    )
    spec = _parse(spec, body.spec)  # Request fields, including explicit empty strings, override the preset.
    spec.cwd = spec.cwd.replace("{session_id}", str(body.session_id))
    setup_script = body.setup_script if "setup_script" in body.model_fields_set else defaults.setup_script or None
    opening = await bridge.create_session(name, body.session_id, spec, setup_script)
    return MessageToDict(opening)


@router.get("/{session_id}/opening")
async def session_opening(bridge: Bridge, name: str, session_id: UUID) -> dict[str, object]:
    return MessageToDict(await bridge.session_opening(name, session_id))
