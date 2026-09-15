"""Runner-first commands and database-backed conversation delivery across app replicas."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta
from typing import Annotated
from uuid import UUID

import grpc
from fastapi import APIRouter, Depends, Request, status
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from x.agentplane.app.changes import Changes
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory, SandboxNotFoundError
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.presets import PresetCatalog
from x.agentplane.app.trajectory import (
    EventReplicationError,
    FeedEnd,
    FeedError,
    IngestionLease,
    IngestionLeaseLostError,
    ThreadNotFoundError,
    TrajectoryStore,
)
from x.agentplane.protocol import command_pb2, event_log_pb2
from x.agentplane.runner import protocol_pb2
from x.agentplane.runner.client import Attachment, RunnerClient, RunnerError, StreamClosedError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
REPLAY_PAGE = 1000
KEEPALIVE_S = 15
RECONCILE_S = 2
LEASE_DURATION = timedelta(seconds=30)
AddressOf = Callable[[str], Awaitable[str]]
DiscoverSandboxes = Callable[[], Awaitable[list[str]]]


class SandboxNotReachableError(Exception):
    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class MalformedMessageError(Exception):
    """A request body is not the proto-JSON of the message the route takes."""


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    spec: dict[str, object] = Field(
        description="Explicit proto-JSON SessionSpec fields; Sandbox-bound defaults fill omitted fields."
    )


def runner_address(index: LiveIndex, port: int) -> AddressOf:
    async def address_of(name: str) -> str:
        view = index.sandbox_view(name)
        if view is None:
            raise SandboxNotFoundError(name)
        if view.state is not ProvisioningState.RUNNING or view.pod is None or view.pod.ip is None:
            raise SandboxNotReachableError(name, view.state)
        return f"{view.pod.ip}:{port}"

    return address_of


class Feed:
    """One lease owner's ingestion connection. Browsers never subscribe to this object."""

    def __init__(self, *, session_id: str, client: RunnerClient, store: TrajectoryStore, lease: IngestionLease):
        self.session_id = session_id
        self.client = client
        self.store = store
        self.lease = lease
        self.task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        attachment: Attachment | None = None
        try:
            async with asyncio.timeout(10):
                attachment = await self.client.attach(self.session_id)
            attached = attachment.attached
            thread_id = await self.store.thread(self.lease.sandbox, self.session_id, attached.spec)
            stored = await self.store.last_cursor(thread_id)
            if stored > attached.last_cursor:
                if await self.store.feed_state(thread_id) is None:
                    await self.store.set_attached(thread_id, attached, lease=self.lease)
                await self.store.end_feed(
                    thread_id,
                    lease=self.lease,
                    error="runner log cursor regressed; refusing to merge a different session history",
                )
                return
            if stored:
                attachment.cancel()
                async with asyncio.timeout(10):
                    # Replay the boundary entry too: the same cursor must still identify the
                    # exact archived Event and source even if the runner has no new entries.
                    attachment = await self.client.attach(self.session_id, after_cursor=stored - 1)
            await self.store.set_attached(thread_id, attachment.attached, lease=self.lease)
            try:
                while True:
                    entry = await attachment.next_entry()
                    await self.store.record(thread_id, [entry], lease=self.lease)
                    attachment.seen.clear()
            except StreamClosedError:
                copied = await self.store.last_cursor(thread_id)
                await self.store.end_feed(
                    thread_id,
                    lease=self.lease,
                    error=(
                        f"runner replay ended at cursor {copied} before promised cursor {attachment.attached.last_cursor}"
                        if copied < attachment.attached.last_cursor
                        else None
                    ),
                )
            except EventReplicationError as error:
                logger.error("invalid runner history for %s/%s", self.lease.sandbox, self.session_id, exc_info=True)
                await self.store.end_feed(thread_id, lease=self.lease, error=str(error))
        except IngestionLeaseLostError:
            logger.info("ingestion lease lost for %s/%s", self.lease.sandbox, self.session_id)
        except grpc.aio.AioRpcError, ConnectionError, SQLAlchemyError, RunnerError, TimeoutError:
            # Reconcile retries from the committed cursor. A transport loss is not session end.
            logger.warning("ingestion interrupted for %s/%s", self.lease.sandbox, self.session_id, exc_info=True)
        finally:
            if attachment is not None:
                attachment.cancel()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class RunnerBridge:
    def __init__(
        self,
        *,
        address_of: AddressOf,
        store: TrajectoryStore,
        discover_sandboxes: DiscoverSandboxes | None = None,
        sandbox_changes: Changes | None = None,
    ) -> None:
        self._address_of = address_of
        self._store = store
        self._discover_sandboxes = discover_sandboxes
        self._sandbox_changes = sandbox_changes
        self._clients: dict[str, RunnerClient] = {}
        self._feeds: dict[tuple[str, str], Feed] = {}
        self._leases: dict[str, IngestionLease] = {}
        self._sandboxes: set[str] = set()
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._coordinator: asyncio.Task[None] | None = None

    async def _client(self, sandbox: str) -> RunnerClient:
        address = await self._address_of(sandbox)
        if address not in self._clients:
            self._clients[address] = RunnerClient(address)
        return self._clients[address]

    async def start(self, running_sandboxes: list[str]) -> None:
        self._sandboxes.update(running_sandboxes)
        if self._coordinator is None:
            self._coordinator = asyncio.create_task(self._coordinate(), name="sandbox-ingestion")
        self._changed.set()

    async def _coordinate(self) -> None:
        with contextlib.ExitStack() as subscriptions:
            if self._sandbox_changes is not None:
                subscriptions.enter_context(self._sandbox_changes.subscribe(self._changed))
            await self._coordinate_subscribed()

    async def _coordinate_subscribed(self) -> None:
        while True:
            self._changed.clear()
            try:
                if self._discover_sandboxes is not None:
                    self._sandboxes = set(await self._discover_sandboxes())
                await self.reconcile()
            except SQLAlchemyError, grpc.aio.AioRpcError, OSError:
                logger.warning("sandbox ingestion reconciliation failed; will retry", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=RECONCILE_S)

    async def reconcile(self) -> None:
        """Renew ownership and discover sessions opened through any replica."""
        async with self._reconcile_lock:
            for sandbox in set(self._leases) - self._sandboxes:
                await self._release(sandbox)
            async with asyncio.TaskGroup() as tasks:
                for sandbox in sorted(self._sandboxes):
                    tasks.create_task(self._reconcile_sandbox(sandbox))

    async def _reconcile_sandbox(self, sandbox: str) -> None:
        try:
            async with asyncio.timeout(10):
                lease = self._leases.get(sandbox)
                if lease is not None and not await self._store.renew_ingestion(lease, LEASE_DURATION):
                    await self._release(sandbox)
                    lease = None
                if lease is None:
                    lease = await self._store.acquire_ingestion(sandbox, LEASE_DURATION)
                    if lease is None:
                        return
                    self._leases[sandbox] = lease
                try:
                    async with asyncio.timeout(5):
                        client = await self._client(sandbox)
                        summaries = await client.list_sessions()
                    for summary in summaries:
                        key = (sandbox, summary.session_id)
                        feed = self._feeds.get(key)
                        if feed is not None and feed.task is not None and not feed.task.done():
                            if feed.client is client:
                                continue
                            await feed.close()
                        thread_id = await self._store.thread(sandbox, summary.session_id, summary.spec)
                        snapshot = await self._store.feed_state(thread_id)
                        if (
                            summary.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
                            and snapshot is not None
                            and snapshot.end is not None
                            and await self._store.last_cursor(thread_id) == summary.last_cursor
                        ):
                            continue
                        feed = Feed(session_id=summary.session_id, client=client, store=self._store, lease=lease)
                        feed.task = asyncio.create_task(feed.run(), name=f"ingest-{sandbox}-{summary.session_id}")
                        self._feeds[key] = feed
                except grpc.aio.AioRpcError, SandboxNotReachableError, SandboxNotFoundError, TimeoutError:
                    logger.warning("sandbox %s ingestion discovery unavailable", sandbox, exc_info=True)
        except SQLAlchemyError, OSError, TimeoutError:
            logger.warning("sandbox %s ingestion reconciliation failed; will retry", sandbox, exc_info=True)

    async def _release(self, sandbox: str) -> None:
        for key in [key for key in self._feeds if key[0] == sandbox]:
            await self._feeds.pop(key).close()
        await self._store.release_ingestion(self._leases.pop(sandbox))

    async def list_sessions(self, sandbox: str) -> list[protocol_pb2.SessionSummary]:
        return await (await self._client(sandbox)).list_sessions()

    async def initialize(self, sandbox: str, script: str) -> protocol_pb2.InitializeResult:
        try:
            result = await (await self._client(sandbox)).initialize(script)
        except grpc.aio.AioRpcError as error:
            if error.code() == grpc.StatusCode.FAILED_PRECONDITION:
                raise RunnerError(f"sandbox bootstrap refused: {error.details()}") from error
            raise
        if result.exit_code != 0:
            raise RunnerError(
                f"sandbox bootstrap failed with exit {result.exit_code}; output remains available "
                "from the runner's initialization stream"
            )
        return result

    async def open_session(
        self, sandbox: str, session_id: str, spec: protocol_pb2.SessionSpec
    ) -> protocol_pb2.Attached:
        attachment = await (await self._client(sandbox)).attach(session_id, spec=spec)
        try:
            await attachment.detach()
            await attachment.drain_until_end()
            thread_id = await self._store.thread(sandbox, session_id, attachment.attached.spec)
            await self.start([sandbox])
            # In particular, do not return a resumed session while the database still says its
            # previous harness ended. Commands remain runner-first; this only synchronizes Open.
            waiter = asyncio.Event()
            with self._store.changes.subscribe(waiter):
                async with asyncio.timeout(15):
                    while True:
                        waiter.clear()
                        if await self._store.last_cursor(thread_id) >= attachment.attached.last_cursor:
                            break
                        await waiter.wait()
            return attachment.attached
        finally:
            attachment.cancel()

    async def command(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Return only after this Thread's matching runner admission is in the app archive."""
        if admitted := await self._store.admitted_command(thread_id, command):
            return admitted
        thread = await self._store.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)
        await self._command(thread.sandbox, thread.session_id, command)
        await self.start([thread.sandbox])
        return await self._wait_for_admission(thread_id, command)

    async def _command(self, sandbox: str, session_id: str, command: command_pb2.Command) -> None:
        attachment = await (await self._client(sandbox)).attach(session_id)
        try:
            if attachment.attached.harness_state != protocol_pb2.HARNESS_STATE_RUNNING:
                raise RunnerError("session is stopped; explicitly open it before sending commands")
            await attachment.command(command)
            # Detach is ordered after the Command on this bidi stream, but the command's native
            # operation may continue long after runner admission. The feed, not this relay
            # attachment, copies its resulting Events; do not wait for a Stop's process exit or
            # another command's harness effect before returning the saved admission receipt.
            await attachment.detach()
        finally:
            attachment.cancel()

    async def _wait_for_admission(self, thread_id: UUID, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """Wait for the ingester's committed prefix, never for a native command effect."""
        waiter = asyncio.Event()
        with self._store.changes.subscribe(waiter):
            async with asyncio.timeout(15):
                while True:
                    if admitted := await self._store.admitted_command(thread_id, command):
                        return admitted
                    waiter.clear()
                    # A commit between the first read and clear is visible here even if its NOTIFY
                    # was already consumed; notifications only wake this durable reread.
                    if admitted := await self._store.admitted_command(thread_id, command):
                        return admitted
                    # LISTEN/NOTIFY is deliberately only a wake-up. If a notification is lost
                    # while this app is attached, a bounded durable reread still finds the
                    # committed runner admission without asking the runner to repeat it.
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(RECONCILE_S):
                            await waiter.wait()

    async def events(self, thread_id: UUID, *, after_cursor: int) -> AsyncGenerator[bytes]:
        """Follow the committed archive without requiring or starting a runner attachment."""
        if await self._store.get_thread(thread_id) is None:
            raise ThreadNotFoundError(thread_id)
        waiter = asyncio.Event()
        cursor = after_cursor
        with self._store.changes.subscribe(waiter):
            attached_sent = False
            while True:
                waiter.clear()
                snapshot = await self._store.feed_state(thread_id)
                if not attached_sent and snapshot is not None:
                    yield _frame("attached", MessageToDict(snapshot.attached))
                    attached_sent = True
                while page := await self._store.events(thread_id, after_cursor=cursor, limit=REPLAY_PAGE):
                    for entry in page:
                        yield _frame("event", MessageToDict(entry), event_id=entry.cursor)
                        cursor = entry.cursor
                snapshot = await self._store.feed_state(thread_id)
                if snapshot is not None and snapshot.end is not None:
                    if await self._store.last_cursor(thread_id) > cursor:
                        continue
                    match snapshot.end:
                        case FeedEnd():
                            yield _frame("end", {})
                        case FeedError(message=message):
                            yield _frame("error", {"message": message})
                    return
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield b": keepalive\n\n"

    async def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._coordinator
            self._coordinator = None
        for sandbox in list(self._leases):
            await self._release(sandbox)
        await asyncio.gather(*(client.close() for client in self._clients.values()))


def _frame(event: str, data: dict[str, object], *, event_id: int | None = None) -> bytes:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data)}")
    return ("\n".join(lines) + "\n\n").encode()


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
async def open_session(bridge: Bridge, name: str, body: NewSession, request: Request) -> dict[str, object]:
    inventory = request.app.state.inventory
    presets = request.app.state.presets
    if not isinstance(inventory, SandboxInventory) or not isinstance(presets, PresetCatalog):
        raise TypeError("the app's inventory or preset catalog is not configured")
    try:
        binding = await inventory.binding(name)
    except SandboxNotFoundError:
        # Preserve the old concrete-spec path: its runner address remains the authority that decides
        # whether the sandbox is reachable. Tests and non-Kubernetes embeddings may supply one
        # without keeping a second inventory record solely for preset lookup.
        binding = None
    resolved = dict(body.spec)
    if binding is not None and binding.thread_defaults is not None:
        resolved = binding.thread_defaults.proto_json(body.session_id) | resolved
    if binding is not None and binding.bootstrap:
        await bridge.initialize(name, binding.bootstrap)
    spec = _parse(protocol_pb2.SessionSpec(), resolved)
    spec.instructions = presets.instructions_for(spec.instructions)
    attached = await bridge.open_session(name, body.session_id, spec)
    return MessageToDict(attached)
