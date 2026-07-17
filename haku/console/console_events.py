"""Server-pushed console events relayed across replicas through PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Annotated, ClassVar, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError

from haku.console import operator_auth
from haku.console.config import Settings
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.provider_connection_registry import ProviderConnectionKind

logger = logging.getLogger(__name__)
router = APIRouter(tags=["console-events"])


class McpOperatorAuthChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["mcp_operator_auth_changed"] = "mcp_operator_auth_changed"
    server_id: str
    status: Literal["connected", "disconnected"]


class ProviderConnectionChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["provider_connection_changed"] = "provider_connection_changed"
    provider: ProviderConnectionKind
    status: Literal["connected", "disconnected"]


class ConsoleHelloEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["hello"] = "hello"


class ToolCallsChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["tool_calls_changed"] = "tool_calls_changed"
    tool_call_id: str


type ConsoleEvent = ToolCallsChangedEvent | McpOperatorAuthChangedEvent | ProviderConnectionChangedEvent


class _RoutedConsoleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator_id: UUID
    event: ConsoleEvent


def _psycopg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class ConsoleEventHub:
    """Cross-replica Postgres LISTEN/NOTIFY fan-out to connected console tabs."""

    _CHANNEL = "haku_console_events"
    _START_TIMEOUT_SECONDS: ClassVar[float] = 15
    _CONNECT_TIMEOUT_SECONDS: ClassVar[int] = 5
    _PUBLISH_TIMEOUT_SECONDS: ClassVar[float] = 5
    _SOCKET_TIMEOUT_SECONDS: ClassVar[float] = 2
    _SESSION_REVALIDATION_SECONDS: ClassVar[float] = 30

    def __init__(self, database_url: str, *, operator_identity_store: PostgresOperatorIdentityStore) -> None:
        self._connections: dict[WebSocket, UUID] = {}
        self._tool_call_waiters: dict[tuple[UUID, str], set[asyncio.Event]] = {}
        self._dsn = _psycopg_dsn(database_url)
        self._operator_identity_store = operator_identity_store
        self._listen_task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()
        self._publisher: psycopg.AsyncConnection | None = None
        self._publish_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._listen_task is None:
            self._listen_task = asyncio.create_task(self._listen_loop())
        # A caller may publish immediately after app startup. Do not report the app ready until its
        # replica has actually subscribed, or that first notification can disappear in the gap.
        try:
            await asyncio.wait_for(self._listening.wait(), timeout=self._START_TIMEOUT_SECONDS)
        except TimeoutError as e:
            await self.aclose()
            raise RuntimeError("console event relay did not become ready") from e

    async def aclose(self) -> None:
        if self._listen_task is not None:
            listen_task = self._listen_task
            self._listen_task = None
            self._listening.clear()
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task
        if self._publisher is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._publisher.close(), timeout=self._SOCKET_TIMEOUT_SECONDS)
            self._publisher = None
        await self._close_connections(code=1001, reason="console event hub stopped")
        self._wake_all_tool_call_waiters()

    @contextlib.asynccontextmanager
    async def subscribe(self, operator_id: UUID, tool_call_id: str) -> AsyncIterator[asyncio.Event]:
        """Subscribe this replica to one actor-scoped tool-call invalidation."""
        key = (operator_id, tool_call_id)
        changed = asyncio.Event()
        self._tool_call_waiters.setdefault(key, set()).add(changed)
        try:
            yield changed
        finally:
            waiters = self._tool_call_waiters.get(key)
            if waiters is not None:
                waiters.discard(changed)
                if not waiters:
                    self._tool_call_waiters.pop(key, None)

    def _wake_all_tool_call_waiters(self) -> None:
        for waiters in self._tool_call_waiters.values():
            for waiter in waiters:
                waiter.set()

    async def connect(self, websocket: WebSocket, operator_id: UUID) -> bool:
        if not self._listening.is_set():
            await websocket.close(code=1013, reason="console event relay unavailable")
            return False
        await websocket.accept()
        self._connections[websocket] = operator_id
        return True

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def _close_connections(self, *, code: int, reason: str) -> None:
        connections = list(self._connections)
        self._connections.clear()

        async def close(websocket: WebSocket) -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=self._SOCKET_TIMEOUT_SECONDS)

        await asyncio.gather(*(close(websocket) for websocket in connections))

    async def broadcast(self, operator_id: UUID, events: Iterable[ConsoleEvent]) -> None:
        envelopes = [_RoutedConsoleEvent(operator_id=operator_id, event=event) for event in events]
        if not envelopes:
            return
        try:
            async with asyncio.timeout(self._PUBLISH_TIMEOUT_SECONDS):
                async with self._publish_lock:
                    publisher = self._publisher
                    if publisher is None:
                        publisher = await psycopg.AsyncConnection.connect(
                            self._dsn, autocommit=True, connect_timeout=self._CONNECT_TIMEOUT_SECONDS
                        )
                        self._publisher = publisher
                    for envelope in envelopes:
                        await publisher.execute("SELECT pg_notify(%s, %s)", (self._CHANNEL, envelope.model_dump_json()))
        except Exception:
            # The ledger/OAuth row is authoritative and may already be committed. A lossy UI
            # invalidation must never turn that successful mutation into a false 500 or strand a
            # RUNNING call before execution. Drop the producer so the next event reconnects, and
            # force local tabs to REST-sync; every tab also performs a bounded periodic sync.
            logger.exception("failed to publish console events")
            if self._publisher is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._publisher.close(), timeout=self._SOCKET_TIMEOUT_SECONDS)
                self._publisher = None
            self._wake_all_tool_call_waiters()
            await self._close_connections(code=1012, reason="console event publish failed")

    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None:
        await self.broadcast(operator_id, [ToolCallsChangedEvent(tool_call_id=tool_call_id)])

    async def _deliver_locally(self, event_operator_id: UUID, event: ConsoleEvent) -> None:
        if isinstance(event, ToolCallsChangedEvent):
            for waiter in self._tool_call_waiters.get((event_operator_id, event.tool_call_id), ()):
                waiter.set()
        if not self._connections:
            return
        if not await asyncio.to_thread(self._operator_identity_store.is_active, event_operator_id):
            disabled = [
                websocket
                for websocket, connected_operator_id in list(self._connections.items())
                if connected_operator_id == event_operator_id
            ]
            for websocket in disabled:
                self.disconnect(websocket)
            await asyncio.gather(
                *(
                    asyncio.wait_for(
                        websocket.close(code=1008, reason="operator is disabled or missing"),
                        timeout=self._SOCKET_TIMEOUT_SECONDS,
                    )
                    for websocket in disabled
                ),
                return_exceptions=True,
            )
            return
        message = event.model_dump(mode="json")

        async def send(websocket: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(websocket.send_json(message), timeout=self._SOCKET_TIMEOUT_SECONDS)
                return None
            except Exception:
                return websocket

        recipients = [
            websocket
            for websocket, connected_operator_id in list(self._connections.items())
            if connected_operator_id == event_operator_id
        ]
        dead = [websocket for websocket in await asyncio.gather(*(send(ws) for ws in recipients)) if websocket]
        for websocket in dead:
            self.disconnect(websocket)
        await asyncio.gather(
            *(
                asyncio.wait_for(
                    websocket.close(code=1011, reason="console event client is not receiving"),
                    timeout=self._SOCKET_TIMEOUT_SECONDS,
                )
                for websocket in dead
            ),
            return_exceptions=True,
        )

    async def _listen_loop(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True, connect_timeout=self._CONNECT_TIMEOUT_SECONDS
                ) as conn:
                    await conn.execute(f"LISTEN {self._CHANNEL}")
                    self._listening.set()
                    # Notifications committed while this replica was reconnecting are gone. Wake
                    # every waiter after each successful LISTEN so it re-reads the durable ledger.
                    self._wake_all_tool_call_waiters()
                    try:
                        async for note in conn.notifies():
                            try:
                                envelope = _RoutedConsoleEvent.model_validate_json(note.payload)
                            except ValidationError:
                                logger.exception("failed to parse console event notification payload")
                                continue
                            await self._deliver_locally(envelope.operator_id, envelope.event)
                    finally:
                        self._listening.clear()
                await self._close_connections(code=1012, reason="console event relay reconnecting")
                self._wake_all_tool_call_waiters()
                logger.warning("console event listen stream ended; reconnecting")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self._listening.clear()
                raise
            except Exception:
                self._listening.clear()
                # An open browser socket would otherwise look healthy while notifications are being
                # missed. Force reconnect + REST sync; attempts during the outage are rejected above.
                await self._close_connections(code=1012, reason="console event relay reconnecting")
                self._wake_all_tool_call_waiters()
                logger.exception("console event listen loop failed; reconnecting")
                await asyncio.sleep(1)


def _event_hub(request: Request) -> ConsoleEventHub:
    return cast(ConsoleEventHub, request.app.state.console_event_hub)


ConsoleEventHubDep = Annotated[ConsoleEventHub, Depends(_event_hub)]


@router.websocket("/api/events/ws")
async def console_events_ws(websocket: WebSocket, actor: operator_auth.OperatorActorDep) -> None:
    hub = cast(ConsoleEventHub, websocket.app.state.console_event_hub)
    settings = cast(Settings, websocket.app.state.settings)
    public_url = urlsplit(settings.public_base_url)
    expected_origin = f"{public_url.scheme}://{public_url.netloc}"
    if websocket.headers.get("origin") != expected_origin:
        await websocket.close(code=1008, reason="invalid websocket origin")
        return
    if not await hub.connect(websocket, actor.operator_id):
        return
    try:
        await websocket.send_json(ConsoleHelloEvent().model_dump(mode="json"))
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=hub._SESSION_REVALIDATION_SECONDS)
            if await asyncio.to_thread(operator_auth.operator_session, websocket) is None:
                await websocket.close(code=1008, reason="operator is disabled or missing")
                return
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)
