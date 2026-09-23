"""Real app processes with test-only gates at ingestion commit and browser sync delivery.

The store's production record/fencing/projection code is unchanged. A SQLAlchemy transaction
subclass pauses only the selected record call, after its real writes or after its real commit.
Process readiness and checkpoint observations travel through a pipe, never filesystem sentinels.
The replay gate holds actual Electric/reconciliation response bytes without rewriting data.
"""

import asyncio
import multiprocessing
import signal
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, AsyncSessionTransaction, async_sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.api import create_app
from agentplane.app.bridge import RunnerBridge
from agentplane.app.database import connect
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import CallerIdentity, CallerKind, require_caller
from agentplane.app.ingestion import Ingester, Ingestion
from agentplane.app.inventory import ProvisioningState, SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import Harness
from agentplane.app.runners import Runners
from agentplane.app.testing.kubernetes import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi, pod, sandbox
from agentplane.app.testing.replication_source import SANDBOX
from agentplane.app.thread.content import ContentStore
from agentplane.app.thread.store import ThreadStore
from agentplane.app.thread.updates import ThreadUpdates
from agentplane.protocol import event_log_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg


class CommitBoundary(StrEnum):
    BEFORE = "before-commit"
    AFTER = "after-commit"


@dataclass(frozen=True)
class Ready:
    url: str


@dataclass(frozen=True)
class Checkpoint:
    boundary: CommitBoundary


@dataclass(frozen=True)
class ReplayHeld:
    cursor: int


class ReplayAction(StrEnum):
    RELEASE = "release"
    DISCONNECT = "disconnect"


class ReplayGate:
    def __init__(self, after_cursor: int, connection: Connection) -> None:
        self.after_cursor = after_cursor
        self._connection = connection
        self._release: asyncio.Task[ReplayAction] | None = None

    async def hold(self, cursor: int) -> ReplayAction:
        if self._release is None:
            self._release = asyncio.create_task(self._wait_for_release())
        if not self._release.done():
            self._connection.send(ReplayHeld(cursor))
        # Closing the old browser document cancels its response, not the gate shared with the
        # reloaded document's new sync connection.
        pending = self._release
        action = await asyncio.shield(pending)
        if action is ReplayAction.DISCONNECT and self._release is pending:
            self._release = None
        return action

    async def _wait_for_release(self) -> ReplayAction:
        command = await receive(self._connection)
        assert isinstance(command, ReplayAction), command
        return command


class GatedConversationDelivery:
    def __init__(self, app: ASGIApp, *, gate: ReplayGate, event_logs: EventLogStore) -> None:
        self._app = app
        self._gate = gate
        self._event_logs = event_logs

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        selected = scope["type"] == "http" and path.endswith(
            ("/sync/entities", "/sync/commands", "/commands/reconcile")
        )
        if not selected:
            await self._app(scope, receive, send)
            return
        thread_id = UUID(path.split("/")[2])
        messages: list[Message] = []

        async def gated_send(message: Message) -> None:
            messages.append(message)
            if message["type"] != "http.response.body" or message.get("more_body", False):
                return
            # Only these finite, bounded-interest responses are buffered. The real backend
            # continues ingesting and Electric still owns snapshot/offset reconciliation.
            cursor = await self._event_logs.last_cursor(thread_id)
            if cursor > self._gate.after_cursor and await self._gate.hold(cursor) is ReplayAction.DISCONNECT:
                await send({"type": "http.response.start", "status": 503, "headers": []})
                await send({"type": "http.response.body", "body": b"test transport interruption"})
                return
            for buffered in messages:
                await send(buffered)

        await self._app(scope, receive, gated_send)


@dataclass(frozen=True)
class Gate:
    boundary: CommitBoundary
    connection: Connection

    async def pause(self) -> None:
        self.connection.send(Checkpoint(self.boundary))
        await asyncio.Future[None]()  # Only SIGKILL releases this test process.


_record_gate: ContextVar[Gate | None] = ContextVar("test_ingestion_commit_gate", default=None)


class GatedTransaction(AsyncSessionTransaction):
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        gate = _record_gate.get() if exc_type is None else None
        if gate is not None and gate.boundary is CommitBoundary.BEFORE:
            await self.session.flush()
            await gate.pause()
        await super().__aexit__(exc_type, exc, traceback)
        if gate is not None and gate.boundary is CommitBoundary.AFTER:
            await gate.pause()


class GatedSession(AsyncSession):
    def begin(self) -> AsyncSessionTransaction:
        return GatedTransaction(self)


class GatedIngestion(Ingestion):
    def __init__(self, engine: AsyncEngine, gate: Gate, cursor: int) -> None:
        super().__init__(engine)
        self._sessions = async_sessionmaker(engine, class_=GatedSession, expire_on_commit=False)
        self._gate = gate
        self._cursor = cursor

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        token = _record_gate.set(self._gate if any(entry.cursor == self._cursor for entry in entries) else None)
        try:
            await super().record(thread_id, entries, lease=lease)
        finally:
            _record_gate.reset(token)


class ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, connection: Connection, url: str) -> None:
        super().__init__(config)
        self._connection = connection
        self._url = url

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        assert self.started
        self._connection.send(Ready(self._url))


def _run(
    database_url: str,
    runner_port: int,
    connection: Connection,
    boundary: CommitBoundary | None,
    cursor: int,
    frontend_directory: Path | None,
    sandbox_state: ProvisioningState | None,
    replay_after: int | None,
    electric_url: str | None,
) -> None:
    asyncio.run(
        _serve(
            database_url,
            runner_port,
            connection,
            boundary,
            cursor,
            frontend_directory,
            sandbox_state,
            replay_after,
            electric_url,
        )
    )


async def _serve(
    database_url: str,
    runner_port: int,
    connection: Connection,
    boundary: CommitBoundary | None,
    cursor: int,
    frontend_directory: Path | None,
    sandbox_state: ProvisioningState | None,
    replay_after: int | None,
    electric_url: str | None,
) -> None:
    engine = connect(database_url)
    store, event_logs, content = ThreadStore(engine), EventLogStore(engine), ContentStore(engine)
    ingestion = Ingestion(engine) if boundary is None else GatedIngestion(engine, Gate(boundary, connection), cursor)
    thread_updates = ThreadUpdates(engine.url)
    custom, core = cast(Any, FakeCustomObjectsApi()), cast(Any, FakeCoreV1Api())
    index = LiveIndex(stale_after_seconds=90, refreshed={"sandboxes": datetime.now(UTC), "pods": datetime.now(UTC)})
    if sandbox_state is not None:
        raw = sandbox(
            SANDBOX, operating_mode="Suspended" if sandbox_state is ProvisioningState.SUSPENDED else "Running"
        )
        custom.objects[("sandboxes", SANDBOX)] = raw
        index.sandboxes[SANDBOX] = raw
        if sandbox_state in (ProvisioningState.RUNNING, ProvisioningState.WAITING_FOR_POD_READY):
            # Where `ReplicationSource.serve` listens: the bridge dials this address at `runner_port`.
            running = pod(SANDBOX, phase="Running", ready=sandbox_state is ProvisioningState.RUNNING, ip="127.0.0.1")
            core.pods[SANDBOX] = running
            index.pods[SANDBOX] = running
    runners = Runners(index, runner_port)
    ingester = Ingester(runners=runners, event_logs=event_logs, ingestion=ingestion)
    bridge = RunnerBridge(
        runners=runners,
        event_logs=event_logs,
        content=content,
        ingester=ingester,
        thread_changes=thread_updates.changes,
    )
    async with (
        httpx.AsyncClient(base_url="http://test-unused-decisions.invalid") as decisions_http,
        httpx.AsyncClient(base_url=electric_url or "http://test-unused-electric.invalid", timeout=65) as electric_http,
    ):
        app = create_app(
            SandboxInventory(namespace=NAMESPACE, custom_objects=custom, core_v1=core),
            bridge,
            store,
            {harness: ["test-model-before", "test-model-after"] for harness in Harness},
            EgressInventory(namespace=NAMESPACE, custom_objects=custom, default_policies=[]),
            DecisionsClient(decisions_http),
            index,
            ActionPolicyInventory(namespace=NAMESPACE, custom_objects=custom),
            electric=ElectricProxy(electric_http, content) if electric_url is not None else None,
            event_logs=event_logs,
            content=content,
            thread_updates=thread_updates,
            operator_sessions=OperatorSessionStore(engine),
        )
        # Authentication is tested separately; the production routes, HTTP transport, ingestion,
        # PostgreSQL notifications, and SSE generator all run here unchanged.
        app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
        if replay_after is not None:
            app.add_middleware(
                GatedConversationDelivery, gate=ReplayGate(replay_after, connection), event_logs=event_logs
            )
        if frontend_directory is not None:
            app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="test-frontend")
        await thread_updates.start()
        await ingester.start()
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                url = f"http://127.0.0.1:{listener.getsockname()[1]}"
                await ReadyServer(uvicorn.Config(app, log_level="warning"), connection, url).serve(sockets=[listener])
        finally:
            await ingester.close()
            await runners.close()
            await thread_updates.close()
            await engine.dispose()


async def receive(connection: Connection) -> object:
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(connection.fileno(), ready.set)
    try:
        await ready.wait()
        return connection.recv()
    finally:
        loop.remove_reader(connection.fileno())


@dataclass
class AppProcess:
    url: str
    process: BaseProcess
    observations: Connection

    async def checkpoint(self) -> Checkpoint:
        observation = await receive(self.observations)
        assert isinstance(observation, Checkpoint), observation
        return observation

    async def replay_held(self) -> ReplayHeld:
        observation = await receive(self.observations)
        assert isinstance(observation, ReplayHeld), observation
        return observation

    def release_replay(self) -> None:
        self.observations.send(ReplayAction.RELEASE)

    def disconnect_replay(self) -> None:
        self.observations.send(ReplayAction.DISCONNECT)

    async def kill(self) -> None:
        self.process.kill()
        await asyncio.to_thread(self.process.join)
        assert self.process.exitcode == -signal.SIGKILL


@asynccontextmanager
async def app_process(
    database_url: str,
    runner_port: int,
    *,
    boundary: CommitBoundary | None = None,
    cursor: int = 0,
    frontend_directory: Path | None = None,
    sandbox_state: ProvisioningState | None = ProvisioningState.RUNNING,
    replay_after: int | None = None,
    electric_url: str | None = None,
) -> AsyncIterator[AppProcess]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_run,
        args=(
            database_url,
            runner_port,
            child,
            boundary,
            cursor,
            frontend_directory,
            sandbox_state,
            replay_after,
            electric_url,
        ),
    )
    process.start()
    child.close()
    try:
        ready = await receive(parent)
        assert isinstance(ready, Ready), ready
        yield AppProcess(ready.url, process, parent)
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join)
        parent.close()
        process.close()
