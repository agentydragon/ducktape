"""Runner-first commands and database-backed conversation delivery across app replicas."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta
from typing import Annotated

import grpc
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from x.agentplane.app.changes import Changes
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory, SandboxNotFoundError
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.presets import PresetCatalog, Provider, SandboxBinding
from x.agentplane.app.shutdown import Shutdown
from x.agentplane.app.trajectory import FeedEnd, FeedError, IngestionLease, IngestionLeaseLostError, TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb
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
    spec: dict[str, object] = Field(description="Explicit proto-JSON SessionSpec fields; these override a preset.")
    preset: str | None = Field(default=None, description="Optional ThreadPreset override.")


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
            stored = await self.store.last_sequence(thread_id)
            if stored > attached.last_sequence:
                if await self.store.feed_state(thread_id) is None:
                    await self.store.set_attached(thread_id, attached, lease=self.lease)
                await self.store.end_feed(
                    thread_id,
                    lease=self.lease,
                    error="runner log sequence regressed; refusing to merge a different session history",
                )
                return
            if stored:
                attachment.cancel()
                async with asyncio.timeout(10):
                    attachment = await self.client.attach(self.session_id, after_sequence=stored)
            await self.store.set_attached(thread_id, attachment.attached, lease=self.lease)
            try:
                while True:
                    event = await attachment.next_event()
                    await self.store.record(thread_id, [event], lease=self.lease)
                    attachment.seen.clear()
            except StreamClosedError:
                await self.store.end_feed(thread_id, lease=self.lease, error=None)
        except IngestionLeaseLostError:
            logger.info("ingestion lease lost for %s/%s", self.lease.sandbox, self.session_id)
        except (grpc.aio.AioRpcError, ConnectionError, SQLAlchemyError, RunnerError, TimeoutError):
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
            except (SQLAlchemyError, grpc.aio.AioRpcError, OSError):
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
                            summary.harness == pb.HARNESS_STATE_STOPPED
                            and snapshot is not None
                            and snapshot.end is not None
                            and await self._store.last_sequence(thread_id) == summary.last_sequence
                        ):
                            continue
                        feed = Feed(session_id=summary.session_id, client=client, store=self._store, lease=lease)
                        feed.task = asyncio.create_task(feed.run(), name=f"ingest-{sandbox}-{summary.session_id}")
                        self._feeds[key] = feed
                except (grpc.aio.AioRpcError, SandboxNotReachableError, SandboxNotFoundError, TimeoutError):
                    logger.warning("sandbox %s ingestion discovery unavailable", sandbox, exc_info=True)
        except (SQLAlchemyError, OSError, TimeoutError):
            logger.warning("sandbox %s ingestion reconciliation failed; will retry", sandbox, exc_info=True)

    async def _release(self, sandbox: str) -> None:
        for key in [key for key in self._feeds if key[0] == sandbox]:
            await self._feeds.pop(key).close()
        await self._store.release_ingestion(self._leases.pop(sandbox))

    async def list_sessions(self, sandbox: str) -> list[pb.SessionSummary]:
        return await (await self._client(sandbox)).list_sessions()

    async def initialize(self, sandbox: str, script: str) -> pb.InitializeResult:
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

    async def open_session(self, sandbox: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
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
                        if await self._store.last_sequence(thread_id) >= attachment.attached.last_sequence:
                            break
                        await waiter.wait()
            return attachment.attached
        finally:
            attachment.cancel()

    async def send(self, sandbox: str, session_id: str, message: pb.Input) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.send(message.input_id, message.text))

    async def interrupt(self, sandbox: str, session_id: str) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.interrupt())

    async def switch_model(self, sandbox: str, session_id: str, switch_id: str, model: str) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.switch_model(switch_id, model))

    async def shutdown(self, sandbox: str, session_id: str) -> None:
        await self._command(sandbox, session_id, lambda attachment: attachment.shutdown(), ends_stream=True)

    async def _command(
        self,
        sandbox: str,
        session_id: str,
        command: Callable[[Attachment], Awaitable[None]],
        *,
        ends_stream: bool = False,
    ) -> None:
        attachment = await (await self._client(sandbox)).attach(session_id)
        try:
            if attachment.attached.harness != pb.HARNESS_STATE_RUNNING:
                raise RunnerError("session is stopped; explicitly open it before sending commands")
            await command(attachment)
            if not ends_stream:
                await attachment.detach()
            await attachment.drain_until_end()
            await self.start([sandbox])
        finally:
            attachment.cancel()

    async def events(self, sandbox: str, session_id: str, *, after_sequence: int) -> AsyncGenerator[bytes]:
        threads = await self._store.list_threads(sandbox=sandbox, session_id=session_id)
        if threads:
            thread_id = threads[0].id
        else:
            summaries = await self.list_sessions(sandbox)
            summary = next((item for item in summaries if item.session_id == session_id), None)
            if summary is None:
                raise RunnerError(f"session {session_id} does not exist")
            thread_id = await self._store.thread(sandbox, session_id, summary.spec)
        await self.start([sandbox])
        waiter = asyncio.Event()
        cursor = after_sequence
        with self._store.changes.subscribe(waiter):
            async with asyncio.timeout(15):
                while True:
                    waiter.clear()
                    snapshot = await self._store.feed_state(thread_id)
                    if snapshot is not None:
                        break
                    await waiter.wait()
            if after_sequence > snapshot.attached.last_sequence:
                raise RunnerError("after_sequence is beyond the stored session log")
            yield _frame("attached", MessageToDict(snapshot.attached))
            while True:
                waiter.clear()
                while page := await self._store.events(thread_id, after_sequence=cursor, limit=REPLAY_PAGE):
                    for event in page:
                        yield _frame("event", MessageToDict(event), event_id=event.sequence)
                        cursor = event.sequence
                snapshot = await self._store.feed_state(thread_id)
                if snapshot is not None and snapshot.end is not None:
                    if await self._store.last_sequence(thread_id) > cursor:
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


def _parse[M: pb.Input | pb.SessionSpec](message: M, body: dict[str, object]) -> M:
    try:
        return ParseDict(body, message)
    except ParseError as error:
        raise MalformedMessageError(f"not a {type(message).__name__}: {error}") from error


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
        binding_raw = await inventory.preset_binding(name)
    except SandboxNotFoundError:
        # Preserve the old concrete-spec path: its runner address remains the authority that decides
        # whether the sandbox is reachable. Tests and non-Kubernetes embeddings may supply one
        # without keeping a second inventory record solely for preset lookup.
        binding_raw = None
    binding = SandboxBinding.model_validate(binding_raw) if binding_raw is not None else None
    resolved = dict(body.spec)
    if body.preset is not None:
        resolved = presets.thread(body.preset).defaults().proto_json(body.session_id) | resolved
    elif binding is not None:
        resolved = presets.thread_defaults(binding).proto_json(body.session_id) | resolved
    if binding is not None:
        bootstrap = presets.sandbox(binding.sandbox_preset).bootstrap
        if bootstrap:
            await bridge.initialize(name, bootstrap)
    spec = _parse(pb.SessionSpec(), resolved)
    spec.instructions = presets.instructions_for(spec.instructions)
    attached = await bridge.open_session(name, body.session_id, spec)
    return MessageToDict(attached)


@router.get("/{session_id}/events")
async def session_events(
    bridge: Bridge,
    shutdown: Shutdown,
    name: str,
    session_id: str,
    after: Annotated[int, Query(ge=0, description="Replay events with a greater sequence.")] = 0,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    # A browser's automatic reconnect sends the last id it saw; that wins over the query parameter.
    after_sequence = last_event_id if last_event_id is not None else after
    return StreamingResponse(
        shutdown.until(bridge.events(name, session_id, after_sequence=after_sequence)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{session_id}/inputs", status_code=status.HTTP_202_ACCEPTED)
async def send_input(bridge: Bridge, name: str, session_id: str, body: dict[str, object]) -> Response:
    await bridge.send(name, session_id, _parse(pb.Input(), body))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{session_id}/interrupt", status_code=status.HTTP_202_ACCEPTED)
async def interrupt_session(bridge: Bridge, name: str, session_id: str) -> Response:
    await bridge.interrupt(name, session_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


class ModelSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    switch_id: str = Field(min_length=1)
    model: str = Field(min_length=1)


@router.post("/{session_id}/model", status_code=status.HTTP_202_ACCEPTED)
async def switch_session_model(
    bridge: Bridge, name: str, session_id: str, body: ModelSwitch, request: Request
) -> Response:
    summaries = await bridge.list_sessions(name)
    summary = next((item for item in summaries if item.session_id == session_id), None)
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown session {session_id!r}")
    provider = Provider.CLAUDE if summary.spec.provider == pb.PROVIDER_CLAUDE else Provider.CODEX
    catalog = request.app.state.models
    if not isinstance(catalog, dict) or body.model not in catalog[provider]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="model is incompatible with this harness"
        )
    await bridge.switch_model(name, session_id, body.switch_id, body.model)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{session_id}/shutdown", status_code=status.HTTP_202_ACCEPTED)
async def shutdown_session(bridge: Bridge, name: str, session_id: str) -> Response:
    await bridge.shutdown(name, session_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
