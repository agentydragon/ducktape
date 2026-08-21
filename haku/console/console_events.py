"""Server-pushed console events relayed across replicas through PostgreSQL.

**Nothing here forbids unknown fields.** These envelopes cross replicas, which under
`maxUnavailable: 0` may run different releases (<README.md> § Perimeter / deploy): a field the next
release adds to an event would otherwise cost the previous one every invalidation on the channel,
including the kinds it does understand. An `event_type` it does not know still fails the union
parse, and is dropped — the tab it would have reached re-syncs on its own 30s timer, so a wake lost
for the length of a roll delays a view rather than losing anything from it.

Forwarding an unrecognised event to the tabs verbatim, so a browser holding the *new* bundle could
act on it, is possible and deliberately not built: it needs a passthrough arm that round-trips raw
JSON through a discriminated union, and the periodic sync already covers the window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Annotated, Any, ClassVar, Literal, cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import make_url

from haku.console import operator_auth
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

logger = logging.getLogger(__name__)
router = APIRouter(tags=["console-events"])

# An application close code (the 4000-4999 range) for the one outcome the shell must act on rather
# than retry: the operator session reached its absolute deadline. Everything else that closes a
# socket is a transport or authority problem the client should back off and reconnect through.
# Mirrored in frontend/console_events.ts.
OPERATOR_SESSION_EXPIRED_CLOSE_CODE = 4001


class McpOperatorAuthChangedEvent(BaseModel):
    event_type: Literal["mcp_operator_auth_changed"] = "mcp_operator_auth_changed"
    server_id: str
    status: Literal["connected", "disconnected"]


class OperatorConnectionChangedEvent(BaseModel):
    event_type: Literal["operator_connection_changed"] = "operator_connection_changed"
    connection: str
    status: Literal["connected", "disconnected"]


class ConsoleHelloEvent(BaseModel):
    event_type: Literal["hello"] = "hello"


class ToolCallsChangedEvent(BaseModel):
    event_type: Literal["tool_calls_changed"] = "tool_calls_changed"
    tool_call_id: str


class SessionChangedEvent(BaseModel):
    """A chat session's rows changed; a surface showing it should re-read them.

    An invalidation, not a payload: the transcript stays a REST read, so a tab that missed events
    entirely still lands correct by refetching, and no consumer has to decide whether the socket
    or the API is the truth. Carrying the message itself would make this a second source of one.
    """

    event_type: Literal["session_changed"] = "session_changed"
    session_id: UUID


type ConsoleEvent = (
    ToolCallsChangedEvent | SessionChangedEvent | McpOperatorAuthChangedEvent | OperatorConnectionChangedEvent
)
type ConsoleEventListener = Callable[[UUID, ConsoleEvent], None]


class _RoutedConsoleEvent(BaseModel):
    operator_id: UUID
    event: ConsoleEvent


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


def _libpq_dsn(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


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
        self._dsn = _libpq_dsn(database_url)
        self._operator_identity_store = operator_identity_store
        self._listen_task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()
        self._publisher: asyncpg.Connection[Any] | None = None
        self._inbound: asyncio.Queue[str] = asyncio.Queue()
        self._deliver_task: asyncio.Task[None] | None = None
        self._publish_lock = asyncio.Lock()
        self._listeners: list[ConsoleEventListener] = []

    def add_listener(self, listener: ConsoleEventListener) -> None:
        """Observe each decoded cross-replica event before browser-socket routing."""
        self._listeners.append(listener)

    async def start(self) -> None:
        if self._deliver_task is None:
            self._deliver_task = asyncio.create_task(self._deliver_loop())
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
        if self._deliver_task is not None:
            deliver_task, self._deliver_task = self._deliver_task, None
            deliver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await deliver_task
        if self._publisher is not None:
            with contextlib.suppress(Exception):
                await self._publisher.close(timeout=self._SOCKET_TIMEOUT_SECONDS)
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
                        publisher = await asyncpg.connect(self._dsn, timeout=self._CONNECT_TIMEOUT_SECONDS)
                        self._publisher = publisher
                    for envelope in envelopes:
                        await publisher.execute("SELECT pg_notify($1, $2)", self._CHANNEL, envelope.model_dump_json())
        except Exception:
            # The ledger/OAuth row is authoritative and may already be committed. A lossy UI
            # invalidation must never turn that successful mutation into a false 500 or strand a
            # RUNNING call before execution. Drop the producer so the next event reconnects, and
            # force local tabs to REST-sync; every tab also performs a bounded periodic sync.
            logger.exception("failed to publish console events")
            if self._publisher is not None:
                with contextlib.suppress(Exception):
                    await self._publisher.close(timeout=self._SOCKET_TIMEOUT_SECONDS)
                self._publisher = None
            self._wake_all_tool_call_waiters()
            await self._close_connections(code=1012, reason="console event publish failed")

    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None:
        await self.broadcast(operator_id, [ToolCallsChangedEvent(tool_call_id=tool_call_id)])

    async def deliver_locally(self, event_operator_id: UUID, event: ConsoleEvent) -> None:
        """Send *event* to the sockets **this replica** holds, without publishing it.

        For a producer whose source is already broadcast: the session runtime's own
        `LISTEN`/`NOTIFY` channel reaches every replica, so each one can turn what it hears into
        sends on its own sockets. Routing that through `broadcast` would `NOTIFY` a second time
        for one change and deliver it to every tab twice.
        """
        if isinstance(event, ToolCallsChangedEvent):
            for waiter in self._tool_call_waiters.get((event_operator_id, event.tool_call_id), ()):
                waiter.set()
        for listener in self._listeners:
            listener(event_operator_id, event)
        if not self._connections:
            return
        if not await self._operator_identity_store.is_active(event_operator_id):
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

    def _on_notification(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        """asyncpg dispatches on its reader task, so this may neither block nor await.

        Payloads are queued rather than delivered here, which also preserves the ordering the
        previous `notifies()` iterator gave: one consumer, in arrival order.
        """
        self._inbound.put_nowait(str(payload))

    async def _deliver_loop(self) -> None:
        while True:
            payload = await self._inbound.get()
            try:
                envelope = _RoutedConsoleEvent.model_validate_json(payload)
            except ValidationError:
                logger.exception("failed to parse console event notification payload")
                continue
            try:
                await self.deliver_locally(envelope.operator_id, envelope.event)
            except Exception:
                # One bad delivery must not stop the consumer; the socket layer already
                # drops connections it cannot write to.
                logger.exception("failed to deliver a console event locally")

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=self._CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                await connection.add_listener(self._CHANNEL, self._on_notification)
                self._listening.set()
                # Notifications committed while this replica was reconnecting are gone. Wake
                # every waiter after each successful LISTEN so it re-reads the durable ledger.
                self._wake_all_tool_call_waiters()
                await terminated.wait()
                logger.warning("console event listen stream ended; reconnecting")
            except asyncio.CancelledError:
                self._listening.clear()
                raise
            except Exception:
                logger.exception("console event listen loop failed; reconnecting")
            finally:
                self._listening.clear()
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close(timeout=self._SOCKET_TIMEOUT_SECONDS)
            # An open browser socket would otherwise look healthy while notifications are being
            # missed. Force reconnect + REST sync; attempts during the outage are rejected above.
            await self._close_connections(code=1012, reason="console event relay reconnecting")
            self._wake_all_tool_call_waiters()
            await asyncio.sleep(1)


def _event_hub(request: Request) -> ConsoleEventHub:
    return cast(ConsoleEventHub, request.app.state.console_event_hub)


ConsoleEventHubDep = Annotated[ConsoleEventHub, Depends(_event_hub)]


@router.websocket("/api/events/ws")
async def console_events_ws(websocket: WebSocket, actor: operator_auth.OperatorActorDep) -> None:
    hub = cast(ConsoleEventHub, websocket.app.state.console_event_hub)
    if not operator_auth.exact_operator_origin(websocket):
        await websocket.close(code=1008, reason="invalid websocket origin")
        return
    if not await hub.connect(websocket, actor.operator_id):
        return
    try:
        await websocket.send_json(ConsoleHelloEvent().model_dump(mode="json"))
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=hub._SESSION_REVALIDATION_SECONDS)
            # Expiry is the ordinary case and needs no database round trip: the deadline is signed
            # into the cookie. The shell re-authenticates on this code instead of showing the
            # channel as merely offline.
            if operator_auth.signed_operator_session(websocket) is None:
                await websocket.close(code=OPERATOR_SESSION_EXPIRED_CLOSE_CODE, reason="operator session expired")
                return
            if await operator_auth.operator_session(websocket) is None:
                await websocket.close(code=1008, reason="operator is disabled or missing")
                return
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)
