"""Matrix MCP server with background inbox and yield semantics.

Features (MVP):
- Background sync watcher that collects new text messages in a single room.
- Emits ResourceUpdatedNotification on each new message via EnhancedFastMCP.

- Notes
- Designed for unencrypted rooms first; E2EE can be added later with mautrix + a persisted
  state store. For now we rely on plaintext rooms.
- Network credentials are supplied via MatrixConfig; callers construct this server
  in-proc via MatrixServer().
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, cast

from aiohttp import ClientSession
from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from mautrix.api import HTTPAPI
from mautrix.client import Client as MautrixClient
from mautrix.client.state_store import FileStateStore, StateStore
from mautrix.types import EventType, MessageEvent, RoomAlias, RoomID, TextMessageEventContent, UserID
from pydantic import BaseModel, Field

from agent_server.server.bus import ServerBus, UiEndTurn
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Matrix SDK uses millisecond timeouts for sync; keep constants explicit
SYNC_PRIME_TIMEOUT_MS = 1_000
SYNC_LOOP_TIMEOUT_MS = 30_000

logger = logging.getLogger(__name__)


class MatrixConfig(BaseModel):
    homeserver: str = Field(description="Base URL, e.g. https://matrix.example.com")
    user_id: str = Field(description="Matrix user id, e.g. @bot:example.com")
    access_token: str | None = Field(default=None, description="Access token for the device (preferred)")
    password: str | None = Field(default=None, description="Password (fallback if no access token)")
    room: str = Field(description="Room id or alias to join/watch (e.g. !id:server or #alias:server)")
    store_path: str | None = Field(
        default=None, description="Optional path for mautrix state store (enable if you need persistence/E2EE)"
    )


class IncomingMessage(BaseModel):
    event_id: str
    room_id: str
    sender: str
    timestamp_ms: int
    body: str


class DrainResult(BaseModel):
    messages: list[IncomingMessage]
    last_event_id: str | None = None


class SendMessageInput(OpenAIStrictModeBaseModel):
    content: str = Field(description="Plaintext content to send to the room")


class YieldInput(OpenAIStrictModeBaseModel):
    last_seen_event_id: str = Field(description="The last event id the agent processed; used to advance cursor")


class MessageSendResult(BaseModel):
    ok: bool = True
    event_id: str | None = None


@dataclass
class _Inbox:
    queue: list[IncomingMessage] = field(default_factory=list)
    new_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_seen_event_id: str | None = None

    def enqueue(self, msg: IncomingMessage) -> None:
        self.queue.append(msg)
        # Signal that new messages are available
        if not self.new_event.is_set():
            self.new_event.set()

    def drain(self) -> DrainResult:
        if not self.queue:
            return DrainResult(messages=[], last_event_id=self.last_seen_event_id)
        msgs = list(self.queue)
        self.queue.clear()
        last_id = msgs[-1].event_id
        return DrainResult(messages=msgs, last_event_id=last_id)

    def ack(self, event_id: str) -> None:
        self.last_seen_event_id = event_id
        # If queue was drained and nothing new came in, lower the event to block next waiters
        if not self.queue and self.new_event.is_set():
            self.new_event.clear()


def _parse_text_event(event: MessageEvent, self_user: str) -> IncomingMessage | None:
    """Build an IncomingMessage from a RoomMessageText or return None to skip.

    Skips self-sent messages and non-text bodies.
    """
    if str(event.sender) == self_user:
        return None
    content = event.content
    if not isinstance(content, TextMessageEventContent):
        return None
    return IncomingMessage(
        event_id=str(event.event_id),
        room_id=str(event.room_id),
        sender=str(event.sender),
        timestamp_ms=int(event.timestamp or 0),
        body=content.body,
    )


class _MatrixClient:
    """Thin wrapper around a mautrix client with guarded imports."""

    def __init__(self, cfg: MatrixConfig, inbox: _Inbox, notify: Callable[[str], Any]):
        self.cfg = cfg
        self.inbox = inbox
        self._notify = notify
        self._client: MautrixClient | None = None
        self._http_session: ClientSession | None = None
        self._room_id: RoomID | None = None
        self._sync_task: asyncio.Future[Any] | None = None
        self._state_store: StateStore | None = None

    async def start(self) -> None:
        state_store = FileStateStore(self.cfg.store_path) if self.cfg.store_path else None
        self._state_store = state_store
        # Create HTTP session that we'll manage explicitly (HTTPAPI doesn't close it)
        self._http_session = ClientSession()
        api = HTTPAPI(self.cfg.homeserver, client_session=self._http_session)
        self._client = MautrixClient(mxid=UserID(self.cfg.user_id), api=api, state_store=state_store)
        c = self._client

        if self.cfg.access_token:
            c.api.token = self.cfg.access_token
        else:
            if not self.cfg.password:
                raise RuntimeError("Matrix password or access_token required")
            await c.login(password=self.cfg.password, device_name="adgn-matrix-mcp")

        room = self.cfg.room
        is_alias = room.startswith("#")
        room_identifier: RoomID | RoomAlias = RoomAlias(room) if is_alias else RoomID(room)

        try:
            joined_room = await c.join_room(room_identifier)
            self._room_id = joined_room
        except Exception as join_exc:
            logger.warning("matrix join failed for %s: %s", room_identifier, join_exc)
            if is_alias:
                try:
                    resolved = await c.resolve_room_alias(RoomAlias(room))
                except Exception as resolve_exc:
                    raise RuntimeError(f"Matrix alias {room} could not be resolved") from resolve_exc
                canonical_room_id = RoomID(resolved.room_id)
            else:
                canonical_room_id = RoomID(room)
            self._room_id = canonical_room_id

        async def _on_message(event: MessageEvent) -> None:
            try:
                event_room_id = RoomID(str(event.room_id))
                if self._room_id and event_room_id != self._room_id:
                    return
                msg = _parse_text_event(event, str(c.mxid))
                if msg is None:
                    return
                self.inbox.enqueue(msg)
                try:
                    uri = f"matrix://inbox/{(self._room_id or event_room_id)!s}/{msg.event_id}"
                    task = asyncio.create_task(self._notify(uri))
                    task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)
                except Exception as notify_exc:
                    logger.warning("matrix notify failed: %s", notify_exc)
            except Exception as exc:
                logger.exception("matrix on_message callback failed: %s", exc)

        c.add_event_handler(EventType.ROOM_MESSAGE, _on_message)

        try:
            await c.sync(timeout=SYNC_PRIME_TIMEOUT_MS)
        except (TimeoutError, OSError):
            logger.debug("initial matrix sync failed; continuing", exc_info=True)

        self._sync_task = c.start(None)

    async def stop(self) -> None:
        c = self._client
        if c is None:
            return
        try:
            c.stop()
        except Exception as exc:
            logger.warning("matrix client stop failed", exc_info=exc)
        task = self._sync_task
        if task:
            task.cancel()
            with suppress(Exception):
                await task
        # Close HTTP session
        if self._http_session is not None:
            try:
                if not self._http_session.closed:
                    await self._http_session.close()
            except Exception as exc:
                logger.warning("matrix HTTP session close failed", exc_info=exc)
        if self._state_store is not None:
            with suppress(Exception):
                await self._state_store.close()

    async def send_text(self, content: str) -> dict[str, Any]:
        c = self._client
        rid = self._room_id
        if c is None or rid is None:
            raise RuntimeError("Matrix client not started or room not resolved")
        event_id = await c.send_text(rid, text=content)
        return {"ok": True, "event_id": str(event_id)}


class MatrixServer(EnhancedFastMCP):
    """Matrix MCP server with typed tool access.

    Provides Matrix integration for receiving and sending messages with background sync,
    inbox management, and turn-based interaction.
    """

    # Tool references (assigned in __init__)
    send_tool: FlatTool[Any, Any]
    drain_new_messages_tool: FlatTool[Any, Any]
    do_yield_tool: FlatTool[Any, Any]

    def __init__(self, bus: ServerBus, cfg: MatrixConfig):
        """Create a Matrix MCP server with background sync.

        Args:
            bus: ServerBus for turn management
            cfg: Matrix configuration (homeserver, credentials, room)
        """
        inbox = _Inbox()
        # Background client managed via server lifespan; broadcast notifications on new msgs
        client_holder: dict[str, _MatrixClient] = {}

        # Lifespan wires the mautrix client and uses the provided server instance for notifications
        @asynccontextmanager
        async def _lifespan(server: FastMCP):
            async def _broadcast(uri: str) -> None:
                srv = cast(EnhancedFastMCP, server)
                await srv.broadcast_resource_updated(uri)

            mc = _MatrixClient(cfg, inbox, _broadcast)
            client_holder["client"] = mc
            await mc.start()
            try:
                yield
            finally:
                try:
                    await mc.stop()
                except Exception as e:
                    logger.debug("matrix client stop failed: %s", e)

        display = "Matrix MCP Server"
        instructions = (
            "Matrix bridge: receive DMs via notifications; use the provided tools to\n"
            "read new messages, reply, and end your turn. Do not emit plain text;\n"
            "always communicate via tools."
        )
        super().__init__(display, instructions=instructions, lifespan=_lifespan)

        # Tools
        async def send(input: SendMessageInput) -> MessageSendResult:
            """Send a plaintext message to the configured room."""
            if (mc := client_holder.get("client")) is None:
                # Surface as tool error; FastMCP converts to protocol-level error
                raise ToolError("matrix client not running")
            res = await mc.send_text(input.content)
            return MessageSendResult(ok=True, event_id=str(res.get("event_id")))

        self.send_tool = self.flat_model()(send)

        def drain_new_messages() -> DrainResult:
            """Return and clear queued inbound messages."""
            return inbox.drain()

        self.drain_new_messages_tool = self.flat_model()(drain_new_messages)

        def do_yield(input: YieldInput) -> UiEndTurn:
            """End the current turn and record the last seen event id."""
            inbox.ack(input.last_seen_event_id)
            bus.push_end_turn()
            return UiEndTurn()

        self.do_yield_tool = self.flat_model()(do_yield)
