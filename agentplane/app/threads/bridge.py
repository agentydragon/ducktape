"""Runner sessions and Dispatcher-backed commands."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.sandbox_models import sandbox_view
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.summaries import FeedError
from agentplane.app.threads.view.content import ContentStore

# Not `runner_pb2`: that name is the runner's protocol here.
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2

tracer = trace.get_tracer(__name__)

# gazelle:include_dep @pypi//grpcio


class ThreadStateError(Exception):
    """The Thread's history or its runner Session's retained state refuses the operation."""


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


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
        self.dispatcher = dispatcher

    async def list_sessions(self, sandbox: str) -> list[runner_pb2.SessionSummary]:
        return await self._runners.client(sandbox).list_sessions()

    async def runner_status(self, sandbox: str) -> runner_pb2.RunnerStatus:
        return await self._runners.client(sandbox).status()

    async def create_session(
        self, sandbox: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
        """Queue a complete Session spec with the Dispatcher."""
        request = session_dispatcher_pb2.OpenSessionRequest(sandbox=sandbox, session_id=str(session_id), spec=spec)
        if setup_script is not None:
            request.setup_script = setup_script
        with tracer.start_as_current_span("agentplane.dispatcher.open_session"):
            response = cast(
                session_dispatcher_pb2.OpenSessionResponse,
                await self.dispatcher.unary(self.dispatcher.stub.OpenSession, request),
            )
        return response.opening

    async def session_opening(self, sandbox: str, session_id: UUID) -> session_dispatcher_pb2.SessionOpening:
        opening = cast(
            session_dispatcher_pb2.GetSessionOpeningResponse,
            await self.dispatcher.unary(
                self.dispatcher.stub.GetSessionOpening,
                session_dispatcher_pb2.GetSessionOpeningRequest(session_id=str(session_id)),
            ),
        ).opening
        if opening.sandbox != sandbox:
            raise SessionOpeningNotFoundError(str(session_id))
        return opening

    async def command_ready(self, thread_id: UUID) -> None:
        """Reject absent or failed Thread history before a new Dispatcher submission."""
        if not await self._event_logs.exists(thread_id):
            raise ThreadNotFoundError(thread_id)
        snapshot = await self._event_logs.feed_state(thread_id)
        if snapshot is not None and isinstance(snapshot.end, FeedError):
            raise ThreadStateError(f"runner history is rejected: {snapshot.end.message}")


def _parse(message: runner_pb2.SessionSpec, body: dict[str, object]) -> runner_pb2.SessionSpec:
    try:
        return ParseDict(body, message)
    except ParseError as error:
        raise MalformedMessageError(f"not a {type(message).__name__}: {error}") from error


router = APIRouter(prefix="/sandboxes/{name}/sessions", tags=["sessions"])


def _bridge(request: Request) -> RunnerBridge:
    bridge = request.app.state.services.bridge
    if not isinstance(bridge, RunnerBridge):
        raise TypeError(f"app.state.services.bridge is {type(bridge).__name__}, not RunnerBridge")
    return bridge


Bridge = Annotated[RunnerBridge, Depends(_bridge)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(request: Request, bridge: Bridge, name: str, body: NewSession) -> dict[str, object]:
    with tracer.start_as_current_span("agentplane.session.create"):
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
        await services.store.open_thread(body.session_id)
        opening = await bridge.create_session(name, body.session_id, spec, setup_script)
        return MessageToDict(opening)


@router.get("/{session_id:uuid}/opening")
async def session_opening(bridge: Bridge, name: str, session_id: UUID) -> dict[str, object]:
    return MessageToDict(await bridge.session_opening(name, session_id))
