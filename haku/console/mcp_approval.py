"""Operator-approved MCP tool calls owned by haku-console.

This router is the privileged tool-call ledger: callers can discover connected MCP
servers, submit exact calls against any reflected tool, and read the console-owned
result/audit state. In v1 every execution waits for an operator decision in trusted
console chrome.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import secrets
from collections.abc import Iterable
from typing import Annotated, Any, Literal, Protocol, cast

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from mcp import types as mcp_types
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.config import Settings
from haku.console.database_migrate import run_migrations_for_connection
from haku.console.database_schema import McpToolCall, McpToolCallEvent
from haku.console.deps import SettingsDep
from haku.console.mcp_models import (
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallEvent,
    ToolCallEventType,
    ToolCallRecord,
    ToolCallStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-approval"])
Csrf = Annotated[CsrfProtect, Depends()]


TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}


class McpServerEntry(BaseModel):
    id: str
    server_url: str
    bearer_token_secret: str | None = None


class ConsoleMcpConfig(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)


class AliveToolMetadata(BaseModel):
    status: Literal["alive"] = "alive"
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class DegradedToolMetadata(BaseModel):
    status: Literal["degraded"] = "degraded"
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    degraded_reason: str


type ToolMetadata = Annotated[AliveToolMetadata | DegradedToolMetadata, Field(discriminator="status")]


class AliveServerMetadata(BaseModel):
    status: Literal["alive"] = "alive"
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)


class DegradedServerMetadata(BaseModel):
    status: Literal["degraded"] = "degraded"
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)
    degraded_reason: str


type ServerMetadata = Annotated[AliveServerMetadata | DegradedServerMetadata, Field(discriminator="status")]


class ToolCapabilitiesResponse(BaseModel):
    servers: list[ServerMetadata] = Field(default_factory=list)


class PendingApproval(BaseModel):
    tool_call_id: str
    server_id: str
    title: str | None = None
    tool_name: str
    caller_principal: str
    rationale: str
    arguments: dict[str, Any]
    created_at: dt.datetime


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApproval] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class ToolCallEventsResponse(BaseModel):
    events: list[ToolCallEvent] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    tool_call: ToolCallRecord


class ToolCallLedgerProtocol(Protocol):
    def submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]: ...

    def get(self, tool_call_id: str) -> ToolCallRecord: ...

    def list(
        self, *, statuses: list[ToolCallStatus] | None = None, since: dt.datetime | None = None, limit: int = 100
    ) -> ToolCallListResponse: ...

    def events_after_id(self, after_event_id: int = 0) -> ToolCallEventsResponse: ...

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]: ...


class PostgresToolCallLedger:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True)
        with self._engine.begin() as conn:
            run_migrations_for_connection(conn)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]:
        with self._sessions.begin() as session:
            now = dt.datetime.now(dt.UTC)
            record = ToolCallRecord(
                tool_call_id=f"tc_{secrets.token_hex(12)}",
                server_id=server.id,
                tool_name=req.tool_name,
                caller_principal=caller_principal,
                status=ToolCallStatus.PENDING_APPROVAL,
                created_at=now,
                updated_at=now,
                arguments=req.arguments,
                rationale=req.rationale,
                title=req.title,
            )
            session.add(McpToolCall.from_record(record))
            events = [
                self._insert_event(session, ToolCallEventType.TOOL_CALL_SUBMITTED, record),
                self._insert_event(session, ToolCallEventType.APPROVAL_PENDING, record),
            ]
            return record, events, True

    def get(self, tool_call_id: str) -> ToolCallRecord:
        with self._sessions.begin() as session:
            row = session.get(McpToolCall, tool_call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return row.to_record()

    def list(
        self, *, statuses: list[ToolCallStatus] | None = None, since: dt.datetime | None = None, limit: int = 100
    ) -> ToolCallListResponse:
        with self._sessions.begin() as session:
            stmt = select(McpToolCall)
            if since is not None:
                stmt = stmt.where(McpToolCall.updated_at > since)
            if statuses:
                stmt = stmt.where(McpToolCall.status.in_(statuses))
            rows = session.scalars(stmt.order_by(McpToolCall.created_at).limit(limit)).all()
        records = [row.to_record() for row in rows]
        return ToolCallListResponse(tool_calls=records)

    def events_after_id(self, after_event_id: int = 0) -> ToolCallEventsResponse:
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(McpToolCallEvent)
                .where(McpToolCallEvent.event_id > after_event_id)
                .order_by(McpToolCallEvent.event_id)
            ).all()
        return ToolCallEventsResponse(events=[row.to_event() for row in rows])

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]:
        return self._transition_pending_approval(tool_call_id, ToolCallStatus.RUNNING)

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]:
        return self._transition_pending_approval(tool_call_id, ToolCallStatus.DENIED)

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        if (result is None) == (error is None):
            raise ValueError("finish requires exactly one of result or error")
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            row.status = status
            row.updated_at = dt.datetime.now(dt.UTC)
            row.result_json = result
            row.error = error
            updated = row.to_record()
            event = self._insert_event(session, ToolCallEventType.TOOL_CALL_UPDATED, updated)
            return updated, event

    def _transition_pending_approval(
        self, tool_call_id: str, status: ToolCallStatus
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            record = row.to_record()
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            row.status = status
            row.updated_at = dt.datetime.now(dt.UTC)
            updated = row.to_record()
            event = self._insert_event(session, ToolCallEventType.TOOL_CALL_UPDATED, updated)
            return updated, event

    def _insert_event(self, session: Session, event_type: ToolCallEventType, record: ToolCallRecord) -> ToolCallEvent:
        created_at = dt.datetime.now(dt.UTC)
        row = McpToolCallEvent(
            event_type=event_type, tool_call_id=record.tool_call_id, status=record.status, created_at=created_at
        )
        session.add(row)
        session.flush()
        return row.to_event()

    def _row_by_tool_call_id(self, session: Session, tool_call_id: str) -> McpToolCall:
        row = session.scalars(
            select(McpToolCall).where(McpToolCall.tool_call_id == tool_call_id).with_for_update()
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return row


class ToolCallEventHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, events: Iterable[ToolCallEvent]) -> None:
        payloads = [e.model_dump(mode="json") for e in events]
        if not payloads or not self._connections:
            return
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                for payload in payloads:
                    await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class McpToolExecutor:
    async def execute(self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with Client(server.server_url, auth=_credential_token(server)) as client:
            result = await client.call_tool_mcp(tool_name, arguments)
        if result.isError:
            raise RuntimeError(_mcp_error_message(result))
        return _mcp_result_to_json(result)


class McpMetadataProvider:
    async def metadata(self, server: McpServerEntry) -> ServerMetadata:
        try:
            async with Client(server.server_url, auth=_credential_token(server)) as client:
                tools = await client.list_tools()
        except Exception as e:
            return DegradedServerMetadata(server_id=server.id, title=server.id, tools=[], degraded_reason=str(e))
        reflected: list[ToolMetadata] = []
        for tool in tools:
            schema = tool.inputSchema
            if not isinstance(schema, dict):
                schema = {}
            reflected.append(AliveToolMetadata(name=tool.name, description=tool.description, input_schema=schema))
        return AliveServerMetadata(server_id=server.id, title=server.id, tools=reflected)


def _ledger(request: Request) -> ToolCallLedgerProtocol:
    ledger = request.app.state.tool_call_ledger
    if ledger is None:
        raise HTTPException(status_code=503, detail="MCP approval database is not configured")
    return cast(ToolCallLedgerProtocol, ledger)


def _hub(request: Request) -> ToolCallEventHub:
    return cast(ToolCallEventHub, request.app.state.tool_call_event_hub)


def _executor(request: Request) -> McpToolExecutor:
    return cast(McpToolExecutor, request.app.state.tool_call_executor)


def _metadata_provider(request: Request) -> McpMetadataProvider:
    return cast(McpMetadataProvider, request.app.state.tool_call_metadata_provider)


LedgerDep = Annotated[ToolCallLedgerProtocol, Depends(_ledger)]
HubDep = Annotated[ToolCallEventHub, Depends(_hub)]
ExecutorDep = Annotated[McpToolExecutor, Depends(_executor)]
MetadataProviderDep = Annotated[McpMetadataProvider, Depends(_metadata_provider)]


def _mcp_result_to_json(result: mcp_types.CallToolResult) -> dict[str, Any]:
    return cast(dict[str, Any], result.model_dump(mode="json", by_alias=True, exclude_none=True))


def _mcp_error_message(result: mcp_types.CallToolResult) -> str:
    text_blocks = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    return "\n".join(text_blocks) or "MCP tool returned isError=true"


def _credential_env_name(bearer_token_secret: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", bearer_token_secret).strip("_").upper()
    return f"HAKU_CONSOLE_MCP_CREDENTIAL_{suffix}"


def _credential_token(server: McpServerEntry) -> str | None:
    if server.bearer_token_secret is None:
        return None
    env_name = _credential_env_name(server.bearer_token_secret)
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeError(f"missing MCP bearer token env var {env_name} for MCP server {server.id}")
    return token


def _load_servers(settings: Settings) -> list[McpServerEntry]:
    path = settings.config_file
    if path is None or not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ConsoleConfigFile.model_validate(raw).mcp.servers


def _server_entry(settings: Settings, server_id: str) -> McpServerEntry:
    for server in _load_servers(settings):
        if server.id == server_id:
            return server
    raise HTTPException(status_code=404, detail=f"unknown MCP server: {server_id}")


def _pending_approval_from_record(record: ToolCallRecord) -> PendingApproval:
    return PendingApproval(
        tool_call_id=record.tool_call_id,
        server_id=record.server_id,
        title=record.title,
        tool_name=record.tool_name,
        caller_principal=record.caller_principal,
        rationale=record.rationale,
        arguments=record.arguments,
        created_at=record.created_at,
    )


def _caller_principal(request: Request, settings: Settings) -> str:
    auth = request.headers.get("authorization", "")
    operator = request.headers.get("x-authentik-username")
    expected = settings.agent_api_token.get_secret_value() if settings.agent_api_token else None
    if expected and auth == f"Bearer {expected}":
        return "haku-agent-api-token"
    if operator:
        return operator
    if expected and request.method == "POST" and request.url.path == "/api/tool-calls":
        raise HTTPException(status_code=401, detail="missing or invalid tool-call API token")
    return "operator"


async def _maybe_execute(
    record: ToolCallRecord,
    server: McpServerEntry,
    ledger: ToolCallLedgerProtocol,
    hub: ToolCallEventHub,
    executor: McpToolExecutor,
) -> ToolCallRecord:
    if record.status != ToolCallStatus.RUNNING:
        return record
    try:
        result = await executor.execute(server, record.tool_name, record.arguments)
    except Exception as e:
        updated, event = ledger.finish(record.tool_call_id, result=None, error=str(e))
    else:
        updated, event = ledger.finish(record.tool_call_id, result=result, error=None)
    await hub.broadcast([event])
    logger.info(
        "tool call %s finished status=%s server=%s tool=%s",
        updated.tool_call_id,
        updated.status,
        updated.server_id,
        updated.tool_name,
    )
    return updated


async def _wait_terminal(ledger: ToolCallLedgerProtocol, tool_call_id: str, wait_for_ms: int) -> ToolCallRecord:
    deadline = asyncio.get_running_loop().time() + (wait_for_ms / 1000)
    while True:
        record = ledger.get(tool_call_id)
        if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
            return record
        if asyncio.get_running_loop().time() >= deadline:
            return record
        await asyncio.sleep(0.05)


@router.get("/api/capabilities/mcp-servers")
async def mcp_servers(settings: SettingsDep, metadata_provider: MetadataProviderDep) -> ToolCapabilitiesResponse:
    return ToolCapabilitiesResponse(
        servers=[await metadata_provider.metadata(server) for server in _load_servers(settings)]
    )


@router.post("/api/tool-calls")
async def submit_tool_call(
    body: SubmitToolCallRequest,
    request: Request,
    settings: SettingsDep,
    ledger: LedgerDep,
    hub: HubDep,
    executor: ExecutorDep,
) -> ToolCallRecord:
    server = _server_entry(settings, body.server_id)
    caller = _caller_principal(request, settings)
    record, events, created = ledger.submit(server=server, req=body, caller_principal=caller)
    await hub.broadcast(events)
    if created:
        logger.info(
            "tool call %s submitted status=%s server=%s tool=%s caller=%s",
            record.tool_call_id,
            record.status,
            record.server_id,
            record.tool_name,
            caller,
        )
    if record.status == ToolCallStatus.RUNNING:
        record = await _maybe_execute(record, server, ledger, hub, executor)
    return await _wait_terminal(ledger, record.tool_call_id, body.wait_for_ms)


@router.get("/api/tool-calls")
async def list_tool_calls(
    ledger: LedgerDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: dt.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ToolCallListResponse:
    return ledger.list(statuses=status, since=since, limit=limit)


@router.get("/api/tool-calls/{tool_call_id}")
async def get_tool_call(tool_call_id: str, ledger: LedgerDep) -> ToolCallRecord:
    return ledger.get(tool_call_id)


@router.get("/api/approvals/pending")
async def pending_approvals(ledger: LedgerDep) -> PendingApprovalsResponse:
    records = ledger.list(statuses=[ToolCallStatus.PENDING_APPROVAL]).tool_calls
    return PendingApprovalsResponse(approvals=[_pending_approval_from_record(r) for r in records])


@router.get("/api/approvals/events")
async def approval_events(ledger: LedgerDep, after_event_id: int = 0) -> ToolCallEventsResponse:
    return ledger.events_after_id(after_event_id)


@router.post("/api/tool-calls/{tool_call_id}/decision")
async def decide_approval(
    tool_call_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    csrf_protect: Csrf,
    settings: SettingsDep,
    ledger: LedgerDep,
    hub: HubDep,
    executor: ExecutorDep,
) -> ApprovalDecisionResponse:
    await csrf_protect.validate_csrf(request)
    if body.decision == "deny":
        record, event = ledger.deny(tool_call_id, body.reason)
        await hub.broadcast([event])
        logger.info(
            "tool call %s denied server=%s tool=%s reason=%r",
            record.tool_call_id,
            record.server_id,
            record.tool_name,
            body.reason,
        )
        return ApprovalDecisionResponse(tool_call=record)
    running, running_event = ledger.mark_running(tool_call_id)
    await hub.broadcast([running_event])
    server = _server_entry(settings, running.server_id)
    finished = await _maybe_execute(running, server, ledger, hub, executor)
    return ApprovalDecisionResponse(tool_call=finished)


@router.websocket("/api/approvals/ws")
async def approvals_ws(websocket: WebSocket) -> None:
    hub = websocket.app.state.tool_call_event_hub
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
