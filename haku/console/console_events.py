"""Server-pushed console events relayed across replicas through PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from haku.console import operator_auth
from haku.console.config import Settings
from haku.console.tool_calls import ToolCallEvent

logger = logging.getLogger(__name__)
router = APIRouter(tags=["console-events"])


class McpOperatorAuthChangedEvent(BaseModel):
    event_type: Literal["mcp_operator_auth_changed"] = "mcp_operator_auth_changed"
    server_id: str
    status: Literal["connected", "disconnected"]


class ConsoleHelloEvent(BaseModel):
    event_type: Literal["hello"] = "hello"


type ConsoleEvent = ToolCallEvent | McpOperatorAuthChangedEvent


class _RoutedConsoleEvent(BaseModel):
    operator_subject: str
    event: ConsoleEvent


def _psycopg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class ConsoleEventHub:
    """Cross-replica Postgres LISTEN/NOTIFY fan-out to connected console tabs."""

    _CHANNEL = "haku_console_events"

    def __init__(self, database_url: str) -> None:
        self._connections: dict[WebSocket, str] = {}
        self._dsn = _psycopg_dsn(database_url)
        self._listen_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def aclose(self) -> None:
        if self._listen_task is None:
            return
        self._listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._listen_task

    async def connect(self, websocket: WebSocket, operator_subject: str) -> None:
        await websocket.accept()
        self._connections[websocket] = operator_subject

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def broadcast(self, operator_subject: str, events: Iterable[ConsoleEvent]) -> None:
        envelopes = [_RoutedConsoleEvent(operator_subject=operator_subject, event=event) for event in events]
        if not envelopes:
            return
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            for envelope in envelopes:
                await conn.execute("SELECT pg_notify(%s, %s)", (self._CHANNEL, envelope.model_dump_json()))

    async def _deliver_locally(self, event_operator_subject: str, event: ConsoleEvent) -> None:
        if not self._connections:
            return
        dead: list[WebSocket] = []
        for websocket, connected_operator_subject in self._connections.items():
            if connected_operator_subject != event_operator_subject:
                continue
            try:
                await websocket.send_json(event.model_dump(mode="json"))
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)

    async def _listen_loop(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {self._CHANNEL}")
                    async for note in conn.notifies():
                        try:
                            envelope = _RoutedConsoleEvent.model_validate_json(note.payload)
                        except ValidationError:
                            logger.exception("failed to parse console event notification payload")
                            continue
                        await self._deliver_locally(envelope.operator_subject, envelope.event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("console event listen loop failed; reconnecting")
                await asyncio.sleep(1)


def _event_hub(request: Request) -> ConsoleEventHub:
    return cast(ConsoleEventHub, request.app.state.console_event_hub)


ConsoleEventHubDep = Annotated[ConsoleEventHub, Depends(_event_hub)]


@router.websocket("/api/events/ws")
async def console_events_ws(websocket: WebSocket) -> None:
    hub = cast(ConsoleEventHub, websocket.app.state.console_event_hub)
    settings = cast(Settings, websocket.app.state.settings)
    if settings.public_base_url is not None:
        public_url = urlsplit(settings.public_base_url)
        expected_origin = f"{public_url.scheme}://{public_url.netloc}"
        if websocket.headers.get("origin") != expected_origin:
            await websocket.close(code=1008, reason="invalid websocket origin")
            return
    operator_subject = operator_auth.operator_subject(websocket)
    if operator_subject is None:
        await websocket.close(code=1008, reason="operator authentication required")
        return
    await hub.connect(websocket, operator_subject)
    try:
        await websocket.send_json(ConsoleHelloEvent().model_dump(mode="json"))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
