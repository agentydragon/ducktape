"""Operator-approved MCP tool calls owned by haku-console.

This router is the privileged tool-call ledger: callers can discover connected MCP
servers, submit exact calls against any reflected tool, and read the console-owned
result/audit state. Calls run immediately only when a reviewed auto-approval policy
matches; all others wait for an operator decision in trusted console chrome. The
connected-server catalog lives in `mcp_config`; operator OAuth account
linkage (used when a server executes as the operator's own account) lives in
`mcp_operator_oauth`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import secrets
from collections.abc import Iterable
from typing import Annotated, Any, Literal, cast

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from mcp import types as mcp_types
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.auto_approval import auto_approve_tool_call
from haku.console.config import Settings
from haku.console.database_migrate import run_migrations_for_connection
from haku.console.database_schema import McpToolCall, McpToolCallEvent
from haku.console.deps import SettingsDep
from haku.console.mcp_config import (
    InProcessServers,
    McpServerEntry,
    _credential_token,
    _load_servers,
    _operator_oauth_enabled,
    _server_entry,
    _transport,
)
from haku.console.mcp_operator_oauth import (
    OAuthStoreDep,
    PostgresMcpOperatorOAuthStore,
    _maybe_oauth_store,
    _operator_principal,
)
from haku.console.tool_calls import (
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallEvent,
    ToolCallEventType,
    ToolCallRecord,
    ToolCallStatus,
)
from haku.console.tools.gmail_client import GmailToolsClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-approval"])
Csrf = Annotated[CsrfProtect, Depends()]


TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}


class ToolMetadataBase(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AliveToolMetadata(ToolMetadataBase):
    status: Literal["alive"] = "alive"


class DegradedToolMetadata(ToolMetadataBase):
    status: Literal["degraded"] = "degraded"
    degraded_reason: str


type ToolMetadata = Annotated[AliveToolMetadata | DegradedToolMetadata, Field(discriminator="status")]


class ServerMetadataBase(BaseModel):
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)


class AliveServerMetadata(ServerMetadataBase):
    status: Literal["alive"] = "alive"


class DegradedServerMetadata(ServerMetadataBase):
    status: Literal["degraded"] = "degraded"
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
    created_at: datetime.datetime
    auto_approval_evaluation: str | None = None


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApproval] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class ToolCallEventsResponse(BaseModel):
    events: list[ToolCallEvent] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    tool_call: ToolCallRecord


class PostgresToolCallLedger:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True)
        with self._engine.begin() as conn:
            run_migrations_for_connection(conn)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def submit(
        self,
        *,
        server: McpServerEntry,
        req: SubmitToolCallRequest,
        caller_principal: str,
        auto_approval_policy_id: str | None = None,
        auto_approval_evaluation: str | None = None,
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]:
        with self._sessions.begin() as session:
            now = datetime.datetime.now(datetime.UTC)
            status = ToolCallStatus.RUNNING if auto_approval_policy_id is not None else ToolCallStatus.PENDING_APPROVAL
            record = ToolCallRecord(
                tool_call_id=f"tc_{secrets.token_hex(12)}",
                server_id=server.id,
                tool_name=req.tool_name,
                caller_principal=caller_principal,
                status=status,
                created_at=now,
                updated_at=now,
                arguments=req.arguments,
                rationale=req.rationale,
                title=req.title,
                approval_policy_id=auto_approval_policy_id,
                auto_approval_evaluation=auto_approval_evaluation,
                approved_at=now if auto_approval_policy_id is not None else None,
            )
            session.add(McpToolCall.from_record(record))
            events = [self._insert_event(session, ToolCallEventType.TOOL_CALL_SUBMITTED, record)]
            events.append(
                self._insert_event(
                    session,
                    ToolCallEventType.TOOL_CALL_UPDATED
                    if auto_approval_policy_id is not None
                    else ToolCallEventType.APPROVAL_PENDING,
                    record,
                )
            )
            return record, events, True

    def get(self, tool_call_id: str) -> ToolCallRecord:
        with self._sessions.begin() as session:
            row = session.get(McpToolCall, tool_call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return row.to_record()

    def list(
        self,
        *,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> ToolCallListResponse:
        with self._sessions.begin() as session:
            stmt = select(McpToolCall)
            if since is not None:
                stmt = stmt.where(McpToolCall.updated_at > since)
            if statuses:
                stmt = stmt.where(McpToolCall.status.in_(statuses))
            # `newest_first` makes `limit` keep the most recent calls (the audit/history
            # view wants those); the default ascending order stays the queue-friendly
            # oldest-first for pending-approval reads.
            order = McpToolCall.created_at.desc() if newest_first else McpToolCall.created_at
            rows = session.scalars(stmt.order_by(order).limit(limit)).all()
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
        return self._transition_pending_approval(tool_call_id, ToolCallStatus.DENIED, denial_reason=reason)

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        if (result is None) == (error is None):
            raise ValueError("finish requires exactly one of result or error")
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            row.status = status
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.result_json = result
            row.error = error
            updated = row.to_record()
            event = self._insert_event(session, ToolCallEventType.TOOL_CALL_UPDATED, updated)
            return updated, event

    def _transition_pending_approval(
        self, tool_call_id: str, status: ToolCallStatus, *, denial_reason: str | None = None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._sessions.begin() as session:
            row = self._row_by_tool_call_id(session, tool_call_id)
            record = row.to_record()
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            row.status = status
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.denial_reason = denial_reason
            if status == ToolCallStatus.RUNNING:
                row.approved_at = row.updated_at
            updated = row.to_record()
            event = self._insert_event(session, ToolCallEventType.TOOL_CALL_UPDATED, updated)
            return updated, event

    def _insert_event(self, session: Session, event_type: ToolCallEventType, record: ToolCallRecord) -> ToolCallEvent:
        created_at = datetime.datetime.now(datetime.UTC)
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


def _psycopg_dsn(database_url: str) -> str:
    # SQLAlchemy's "+psycopg" driver suffix isn't a real libpq scheme; strip it so
    # psycopg's own AsyncConnection.connect() (used here for LISTEN/NOTIFY, outside
    # SQLAlchemy) accepts the DSN directly.
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class ToolCallEventHub:
    """WebSocket fan-out to this pod's connected clients, relayed across every
    haku-console replica via Postgres LISTEN/NOTIFY (when a database is configured) —
    so a client connected to one pod still gets the live nudge for an event a
    *different* pod handled, not just events its own pod happened to process.
    `broadcast()` always publishes (NOTIFY when DB-backed, direct local delivery
    otherwise, e.g. in tests with no database); actual WebSocket sends happen only in
    `_deliver_locally`, invoked either directly or from the NOTIFY listen loop — the
    publishing pod hears its own NOTIFY back too, so there's no separate "local vs.
    remote" delivery path to keep in sync.
    """

    _CHANNEL = "haku_console_tool_call_events"

    def __init__(self, database_url: str | None = None) -> None:
        self._connections: set[WebSocket] = set()
        self._dsn = _psycopg_dsn(database_url) if database_url else None
        self._listen_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._dsn is not None:
            self._listen_task = asyncio.create_task(self._listen_loop())

    async def aclose(self) -> None:
        if self._listen_task is None:
            return
        self._listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._listen_task

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, events: Iterable[ToolCallEvent]) -> None:
        payloads = [e.model_dump(mode="json") for e in events]
        if not payloads:
            return
        if self._dsn is None:
            await self._deliver_locally(payloads)
            return
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            for payload in payloads:
                await conn.execute("SELECT pg_notify(%s, %s)", (self._CHANNEL, json.dumps(payload)))

    async def _deliver_locally(self, payloads: list[dict[str, Any]]) -> None:
        if not self._connections:
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

    async def _listen_loop(self) -> None:
        assert self._dsn is not None
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {self._CHANNEL}")
                    async for note in conn.notifies():
                        try:
                            payload = json.loads(note.payload)
                        except ValueError:
                            logger.exception("failed to parse tool-call event notification payload")
                            continue
                        await self._deliver_locally([payload])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tool-call event listen loop failed; reconnecting")
                await asyncio.sleep(1)


class McpToolExecutor:
    def __init__(self, in_process_servers: InProcessServers | None = None) -> None:
        self._in_process = in_process_servers or {}

    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        async with Client(_transport(server, self._in_process), auth=auth_token) as client:
            result = await client.call_tool_mcp(tool_name, arguments)
        if result.isError:
            raise RuntimeError(_mcp_error_message(result))
        return _mcp_result_to_json(result)


class McpMetadataProvider:
    def __init__(self, in_process_servers: InProcessServers | None = None) -> None:
        self._in_process = in_process_servers or {}

    async def metadata(self, server: McpServerEntry, auth_token: str | None) -> ServerMetadata:
        try:
            async with Client(_transport(server, self._in_process), auth=auth_token) as client:
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


def _ledger(request: Request) -> PostgresToolCallLedger:
    ledger = request.app.state.tool_call_ledger
    if ledger is None:
        raise HTTPException(status_code=503, detail="MCP approval database is not configured")
    return cast(PostgresToolCallLedger, ledger)


def _hub(request: Request) -> ToolCallEventHub:
    return cast(ToolCallEventHub, request.app.state.tool_call_event_hub)


def _executor(request: Request) -> McpToolExecutor:
    return cast(McpToolExecutor, request.app.state.tool_call_executor)


def _metadata_provider(request: Request) -> McpMetadataProvider:
    return cast(McpMetadataProvider, request.app.state.tool_call_metadata_provider)


LedgerDep = Annotated[PostgresToolCallLedger, Depends(_ledger)]
HubDep = Annotated[ToolCallEventHub, Depends(_hub)]
ExecutorDep = Annotated[McpToolExecutor, Depends(_executor)]
MetadataProviderDep = Annotated[McpMetadataProvider, Depends(_metadata_provider)]


def _mcp_result_to_json(result: mcp_types.CallToolResult) -> dict[str, Any]:
    return cast(dict[str, Any], result.model_dump(mode="json", by_alias=True, exclude_none=True))


def _mcp_error_message(result: mcp_types.CallToolResult) -> str:
    text_blocks = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    return "\n".join(text_blocks) or "MCP tool returned isError=true"


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
        auto_approval_evaluation=record.auto_approval_evaluation,
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


async def _execution_auth(
    server: McpServerEntry, operator_principal: str, oauth_store: PostgresMcpOperatorOAuthStore
) -> str | None:
    if _operator_oauth_enabled(server):
        token = await oauth_store.access_token_for(server=server, operator_principal=operator_principal)
        if not token:
            raise HTTPException(
                status_code=409,
                detail=f"Connect your {server.id} MCP account in the console before approving this tool call.",
            )
        return token
    return _credential_token(server)


async def operator_authenticated_client(
    server_id: str, request: Request, settings: Settings, oauth_store: PostgresMcpOperatorOAuthStore
) -> Client:
    """Open a `fastmcp` client for `server_id`, authenticated exactly as an approved tool call
    for that server would be (the requesting operator's own `operator_oauth` token, or the
    server's configured bearer). The one public seam other `haku.console.tools.*` modules use
    to reach a remote MCP server's own read tools for preview/reference-data lookups (see
    `haku.console.tools.grocy`) — narrow and read-only by construction of what callers do with
    the returned client; it grants no more than a real approval already would, and is not a
    way to bypass the approval queue for mutating calls.
    """
    server = _server_entry(settings, server_id)
    auth_token = await _execution_auth(server, _operator_principal(request), oauth_store)
    return Client(_transport(server, {}), auth=auth_token)


async def _maybe_execute(
    record: ToolCallRecord,
    server: McpServerEntry,
    ledger: PostgresToolCallLedger,
    hub: ToolCallEventHub,
    executor: McpToolExecutor,
    auth_token: str | None,
) -> ToolCallRecord:
    if record.status != ToolCallStatus.RUNNING:
        return record
    try:
        result = await executor.execute(server, record.tool_name, record.arguments, auth_token)
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


async def _wait_terminal(ledger: PostgresToolCallLedger, tool_call_id: str, wait_for_ms: int) -> ToolCallRecord:
    deadline = asyncio.get_running_loop().time() + (wait_for_ms / 1000)
    while True:
        record = ledger.get(tool_call_id)
        if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
            return record
        if asyncio.get_running_loop().time() >= deadline:
            return record
        await asyncio.sleep(0.05)


async def _metadata_for_request(
    *, request: Request, server: McpServerEntry, metadata_provider: McpMetadataProvider
) -> ServerMetadata:
    if _operator_oauth_enabled(server):
        oauth_store = _maybe_oauth_store(request)
        operator_principal = _operator_principal(request)
        if oauth_store is None:
            return DegradedServerMetadata(
                server_id=server.id,
                title=server.id,
                tools=[],
                degraded_reason="MCP operator OAuth database is not configured.",
            )
        auth_token = await oauth_store.access_token_for(server=server, operator_principal=operator_principal)
        if not auth_token:
            return DegradedServerMetadata(
                server_id=server.id,
                title=server.id,
                tools=[],
                degraded_reason=f"Connect your {server.id} MCP account in the console to reflect this server's tools.",
            )
        return await metadata_provider.metadata(server, auth_token)
    try:
        auth_token = _credential_token(server)
    except Exception as e:
        return DegradedServerMetadata(server_id=server.id, title=server.id, tools=[], degraded_reason=str(e))
    return await metadata_provider.metadata(server, auth_token)


@router.get("/api/capabilities/mcp-servers")
async def mcp_servers(
    request: Request, settings: SettingsDep, metadata_provider: MetadataProviderDep
) -> ToolCapabilitiesResponse:
    return ToolCapabilitiesResponse(
        servers=[
            await _metadata_for_request(request=request, server=server, metadata_provider=metadata_provider)
            for server in _load_servers(settings)
        ]
    )


@router.post("/api/tool-calls")
async def submit_tool_call(
    body: SubmitToolCallRequest,
    request: Request,
    settings: SettingsDep,
    ledger: LedgerDep,
    hub: HubDep,
    executor: ExecutorDep,
    oauth_store: OAuthStoreDep,
) -> ToolCallRecord:
    server = _server_entry(settings, body.server_id)
    caller = _caller_principal(request, settings)
    in_process_servers = cast(InProcessServers, request.app.state.in_process_servers)
    auto_approval_policy_id, auto_approval_evaluation = await auto_approve_tool_call(
        caller_principal=caller,
        server_id=server.id,
        tool_name=body.tool_name,
        arguments=body.arguments,
        label_prefix=settings.gmail_auto_approve_label_prefix,
        gmail=cast(GmailToolsClient | None, request.app.state.gmail_client),
        mcp=in_process_servers.get(server.id),
    )
    record, events, created = ledger.submit(
        server=server,
        req=body,
        caller_principal=caller,
        auto_approval_policy_id=auto_approval_policy_id,
        auto_approval_evaluation=auto_approval_evaluation,
    )
    await hub.broadcast(events)
    if created:
        logger.info(
            "tool call %s submitted status=%s server=%s tool=%s caller=%s approval_policy=%s auto_approval=%s",
            record.tool_call_id,
            record.status,
            record.server_id,
            record.tool_name,
            caller,
            record.approval_policy_id,
            record.auto_approval_evaluation,
        )
    if record.status == ToolCallStatus.RUNNING:
        auth_token = await _execution_auth(server, caller, oauth_store)
        record = await _maybe_execute(record, server, ledger, hub, executor, auth_token)
    return await _wait_terminal(ledger, record.tool_call_id, body.wait_for_ms)


@router.get("/api/tool-calls")
async def list_tool_calls(
    ledger: LedgerDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: datetime.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    newest_first: bool = False,
) -> ToolCallListResponse:
    return ledger.list(statuses=status, since=since, limit=limit, newest_first=newest_first)


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
    oauth_store: OAuthStoreDep,
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
    pending = ledger.get(tool_call_id)
    server = _server_entry(settings, pending.server_id)
    auth_token = await _execution_auth(server, _operator_principal(request), oauth_store)
    running, running_event = ledger.mark_running(tool_call_id)
    await hub.broadcast([running_event])
    finished = await _maybe_execute(running, server, ledger, hub, executor, auth_token)
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
