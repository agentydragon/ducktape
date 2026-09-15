"""Real app processes with test-only gates around the ingestion transaction's commit.

The store's production record/fencing/projection code is unchanged. A SQLAlchemy transaction
subclass pauses only the selected record call, after its real writes or after its real commit.
Process readiness and checkpoint observations travel through a pipe, never filesystem sentinels.
"""

import asyncio
import multiprocessing
import signal
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import httpx
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker, create_async_engine

from x.agentplane.app.action_policy import ActionPolicyInventory
from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import CallerIdentity, CallerKind, require_caller
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.presets import Harness
from x.agentplane.app.testing.kubernetes import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi
from x.agentplane.app.testing.replication_source import SANDBOX
from x.agentplane.app.trajectory import IngestionLease, TrajectoryStore
from x.agentplane.protocol import event_log_pb2

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


class GatedStore(TrajectoryStore):
    def __init__(self, database_url: str, gate: Gate, cursor: int) -> None:
        engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
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


def _run(database_url: str, target: str, connection: Connection, boundary: CommitBoundary | None, cursor: int) -> None:
    asyncio.run(_serve(database_url, target, connection, boundary, cursor))


async def _serve(
    database_url: str, target: str, connection: Connection, boundary: CommitBoundary | None, cursor: int
) -> None:
    store = (
        TrajectoryStore.connect(database_url)
        if boundary is None
        else GatedStore(database_url, Gate(boundary, connection), cursor)
    )

    async def address_of(name: str) -> str:
        assert name == SANDBOX
        return target

    bridge = RunnerBridge(address_of=address_of, store=store)
    custom, core = cast(Any, FakeCustomObjectsApi()), cast(Any, FakeCoreV1Api())
    async with httpx.AsyncClient(base_url="http://test-unused-decisions.invalid") as decisions_http:
        app = create_app(
            SandboxInventory(namespace=NAMESPACE, custom_objects=custom, core_v1=core),
            bridge,
            store,
            {harness: ["test-model-before", "test-model-after"] for harness in Harness},
            EgressInventory(namespace=NAMESPACE, custom_objects=custom, default_policies=[]),
            DecisionsClient(decisions_http),
            LiveIndex(stale_after_seconds=90),
            ActionPolicyInventory(namespace=NAMESPACE, custom_objects=custom),
        )
        # Authentication is tested separately; the production routes, HTTP transport, ingestion,
        # PostgreSQL notifications, and SSE generator all run here unchanged.
        app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
        await store.start_updates()
        await bridge.start([SANDBOX])
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                url = f"http://127.0.0.1:{listener.getsockname()[1]}"
                await ReadyServer(uvicorn.Config(app, log_level="warning"), connection, url).serve(sockets=[listener])
        finally:
            await bridge.close()
            await store.close()


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

    async def kill(self) -> None:
        self.process.kill()
        await asyncio.to_thread(self.process.join)
        assert self.process.exitcode == -signal.SIGKILL


@asynccontextmanager
async def app_process(
    database_url: str, target: str, *, boundary: CommitBoundary | None = None, cursor: int = 0
) -> AsyncIterator[AppProcess]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_run, args=(database_url, target, child, boundary, cursor))
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
