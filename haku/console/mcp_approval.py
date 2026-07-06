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
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, cast

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.config import Settings
from haku.console.database_migrate import run_migrations_for_connection
from haku.console.database_schema import McpToolCall, McpToolCallEvent, sqlalchemy_url
from haku.console.deps import SettingsDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-approval"])
Csrf = Annotated[CsrfProtect, Depends()]


class ToolCallStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}


class McpServerEntry(BaseModel):
    id: str
    server_url: str
    bearer_token_secret: str | None = None


class ConsoleMcpConfig(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)


class ToolMetadata(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    schema_source: Literal["mcp", "unavailable"] = "unavailable"
    degraded_reason: str | None = None


class ServerMetadata(BaseModel):
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)
    schema_source: Literal["mcp", "unavailable"] = "unavailable"
    degraded_reason: str | None = None


class ToolCapabilitiesResponse(BaseModel):
    servers: list[ServerMetadata] = Field(default_factory=list)


class SubmitToolCallRequest(BaseModel):
    server_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    title: str | None = None
    wait_for_ms: int = Field(default=0, ge=0, le=60_000)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = None


class ToolCallRecord(BaseModel):
    tool_call_id: str
    server_id: str
    tool_name: str
    caller_principal: str
    status: ToolCallStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    title: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class ToolCallEvent(BaseModel):
    event_id: int
    event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"]
    tool_call_id: str
    status: ToolCallStatus
    created_at: dt.datetime


class PendingApproval(BaseModel):
    tool_call_id: str
    server_id: str
    title: str
    tool_name: str
    caller_principal: str
    rationale: str
    arguments: dict[str, Any]
    created_at: dt.datetime


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApproval] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    next_cursor: str | None = None


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
        self, *, status: str | None = None, since: int | None = None, limit: int = 100
    ) -> ToolCallListResponse: ...

    def pending_approvals(self) -> PendingApprovalsResponse: ...

    def events_since(self, since: int = 0) -> ToolCallEventsResponse: ...

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]: ...


class PostgresToolCallLedger:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
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
            session.add(McpToolCall.from_record_data(record.model_dump(mode="python")))
            events = [
                self._insert_event(session, "tool_call_submitted", record),
                self._insert_event(session, "approval_pending", record),
            ]
            return record, events, True

    def get(self, tool_call_id: str) -> ToolCallRecord:
        with self._sessions.begin() as session:
            row = session.get(McpToolCall, tool_call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return ToolCallRecord.model_validate(row.to_record_data())

    def list(self, *, status: str | None = None, since: int | None = None, limit: int = 100) -> ToolCallListResponse:
        if status is not None and status != "terminal":
            try:
                ToolCallStatus(status)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"unknown status filter: {status}") from e
        with self._sessions.begin() as session:
            last_event = (
                select(func.max(McpToolCallEvent.event_id))
                .where(McpToolCallEvent.tool_call_id == McpToolCall.tool_call_id)
                .scalar_subquery()
            )
            stmt = select(McpToolCall)
            if since is not None:
                stmt = stmt.where(func.coalesce(last_event, 0) > since)
            if status == "terminal":
                stmt = stmt.where(McpToolCall.status.in_([s.value for s in TERMINAL_STATUSES]))
            elif status is not None:
                stmt = stmt.where(McpToolCall.status == status)
            rows = session.scalars(stmt.order_by(McpToolCall.created_at).limit(limit)).all()
            next_cursor = self._next_cursor(session)
        records = [ToolCallRecord.model_validate(row.to_record_data()) for row in rows]
        return ToolCallListResponse(tool_calls=records, next_cursor=next_cursor)

    def pending_approvals(self) -> PendingApprovalsResponse:
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(McpToolCall)
                .where(McpToolCall.status == ToolCallStatus.PENDING_APPROVAL.value)
                .order_by(McpToolCall.created_at)
            ).all()
        records = [ToolCallRecord.model_validate(row.to_record_data()) for row in rows]
        approvals = [
            PendingApproval(
                tool_call_id=r.tool_call_id,
                server_id=r.server_id,
                title=r.title or f"{r.server_id}: {r.tool_name}",
                tool_name=r.tool_name,
                caller_principal=r.caller_principal,
                rationale=r.rationale,
                arguments=r.arguments,
                created_at=r.created_at,
            )
            for r in records
        ]
        return PendingApprovalsResponse(approvals=approvals)

    def events_since(self, since: int = 0) -> ToolCallEventsResponse:
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(McpToolCallEvent).where(McpToolCallEvent.event_id > since).order_by(McpToolCallEvent.event_id)
            ).all()
        return ToolCallEventsResponse(events=[ToolCallEvent.model_validate(row.to_event_data()) for row in rows])

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            record = ToolCallRecord.model_validate(row.to_record_data())
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            row.status = ToolCallStatus.RUNNING.value
            row.updated_at = dt.datetime.now(dt.UTC)
            updated = ToolCallRecord.model_validate(row.to_record_data())
            event = self._insert_event(session, "tool_call_updated", updated)
            return updated, event

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            record = ToolCallRecord.model_validate(row.to_record_data())
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            row.status = ToolCallStatus.DENIED.value
            row.updated_at = dt.datetime.now(dt.UTC)
            updated = ToolCallRecord.model_validate(row.to_record_data())
            event = self._insert_event(session, "tool_call_updated", updated)
            return updated, event

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            row.status = status.value
            row.updated_at = dt.datetime.now(dt.UTC)
            row.result_json = result
            row.error = error
            updated = ToolCallRecord.model_validate(row.to_record_data())
            event = self._insert_event(session, "tool_call_updated", updated)
            return updated, event

    def _insert_event(
        self,
        session: Session,
        event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"],
        record: ToolCallRecord,
    ) -> ToolCallEvent:
        created_at = dt.datetime.now(dt.UTC)
        row = McpToolCallEvent(
            event_type=event_type, tool_call_id=record.tool_call_id, status=record.status.value, created_at=created_at
        )
        session.add(row)
        session.flush()
        return ToolCallEvent(
            event_id=row.event_id,
            event_type=event_type,
            tool_call_id=record.tool_call_id,
            status=record.status,
            created_at=created_at,
        )

    def _row_by_tool_call_id(self, session: Session, tool_call_id: str) -> McpToolCall:
        row = session.scalars(
            select(McpToolCall).where(McpToolCall.tool_call_id == tool_call_id).with_for_update()
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return row

    def _next_cursor(self, session: Session) -> str:
        return str(session.execute(select(func.coalesce(func.max(McpToolCallEvent.event_id), 0))).scalar_one())


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
            result = await client.call_tool(tool_name, arguments)
        return _mcp_result_to_json(result)


class McpMetadataProvider:
    async def metadata(self, server: McpServerEntry) -> ServerMetadata:
        try:
            async with Client(server.server_url, auth=_credential_token(server)) as client:
                tools = await client.list_tools()
        except Exception as e:
            return ServerMetadata(
                server_id=server.id, title=server.id, tools=[], schema_source="unavailable", degraded_reason=str(e)
            )
        reflected: list[ToolMetadata] = []
        for tool in tools:
            schema = tool.inputSchema
            if not isinstance(schema, dict):
                schema = {}
            reflected.append(
                ToolMetadata(name=tool.name, description=tool.description, input_schema=schema, schema_source="mcp")
            )
        return ServerMetadata(server_id=server.id, title=server.id, tools=reflected, schema_source="mcp")


def make_ledger(settings: Settings) -> ToolCallLedgerProtocol | None:
    if settings.mcp_approval_database_url is not None:
        return PostgresToolCallLedger(settings.mcp_approval_database_url.get_secret_value())
    return None


def make_event_hub() -> ToolCallEventHub:
    return ToolCallEventHub()


def make_executor() -> McpToolExecutor:
    return McpToolExecutor()


def make_metadata_provider() -> McpMetadataProvider:
    return McpMetadataProvider()


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


def _mcp_result_to_json(result: Any) -> dict[str, Any]:
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return {"structured_content": result.structured_content}
    if hasattr(result, "model_dump"):
        return cast(dict[str, Any], result.model_dump(mode="json"))
    return {"repr": repr(result)}


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
    status: str | None = None,
    since: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ToolCallListResponse:
    return ledger.list(status=status, since=since, limit=limit)


@router.get("/api/tool-calls/{tool_call_id}")
async def get_tool_call(tool_call_id: str, ledger: LedgerDep) -> ToolCallRecord:
    return ledger.get(tool_call_id)


@router.get("/api/approvals/pending")
async def pending_approvals(ledger: LedgerDep) -> PendingApprovalsResponse:
    return ledger.pending_approvals()


@router.get("/api/approvals/events")
async def approval_events(ledger: LedgerDep, since: int = 0) -> ToolCallEventsResponse:
    return ledger.events_since(since)


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
