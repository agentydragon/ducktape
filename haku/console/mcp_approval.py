"""Operator-approved MCP tool calls owned by haku-console.

This router is the privileged tool-call ledger: callers can discover connected MCP
servers, submit exact calls against any reflected tool, and read the console-owned
result/audit state. In v1 every execution waits for an operator decision in trusted
console chrome.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import html
import logging
import os
import re
import secrets
from collections.abc import Iterable
from typing import Annotated, Any, Literal, Protocol, cast
from urllib.parse import quote, urlencode, urljoin

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from mcp import types as mcp_types
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    handle_token_response_scopes,
)
from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.config import Settings
from haku.console.database_migrate import run_migrations_for_connection
from haku.console.database_schema import (
    McpOperatorOAuthAssociation,
    McpOperatorOAuthFlow,
    McpToolCall,
    McpToolCallEvent,
)
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
MCP_OPERATOR_AUTH_CALLBACK_PATH = "/api/mcp/operator-auth/callback"
MCP_OPERATOR_AUTH_FLOW_TTL = dt.timedelta(minutes=10)
MCP_OPERATOR_AUTH_REFRESH_SKEW = dt.timedelta(seconds=60)


class McpOperatorOAuthConfig(BaseModel):
    enabled: bool = True
    client_name: str = "Haku Console"
    scopes: list[str] | None = None


class McpServerEntry(BaseModel):
    id: str
    server_url: str
    bearer_token_secret: str | None = None
    operator_oauth: McpOperatorOAuthConfig | None = None


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


class McpOperatorAuthStatus(BaseModel):
    server_id: str
    status: Literal["connected", "unconnected"]
    operator_principal: str
    connected_at: dt.datetime | None = None
    token_expires_at: dt.datetime | None = None
    scope: str | None = None


class McpOperatorAuthStatusResponse(BaseModel):
    associations: list[McpOperatorAuthStatus] = Field(default_factory=list)


class McpOperatorAuthStartResponse(BaseModel):
    server_id: str
    authorization_url: str
    expires_at: dt.datetime


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


class McpOperatorOAuthStoreProtocol(Protocol):
    def list_statuses(
        self, *, servers: list[McpServerEntry], operator_principal: str
    ) -> McpOperatorAuthStatusResponse: ...

    async def start_flow(
        self, *, server: McpServerEntry, operator_principal: str, public_base_url: str
    ) -> McpOperatorAuthStartResponse: ...

    async def complete_callback(self, *, state: str, code: str) -> McpOperatorAuthStatus: ...

    def disconnect(self, *, server_id: str, operator_principal: str) -> None: ...

    async def access_token_for(self, *, server: McpServerEntry, operator_principal: str) -> str | None: ...


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


class PostgresMcpOperatorOAuthStore:
    """Postgres-backed operator OAuth association store for connected MCP servers."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True)
        with self._engine.begin() as conn:
            run_migrations_for_connection(conn)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def list_statuses(self, *, servers: list[McpServerEntry], operator_principal: str) -> McpOperatorAuthStatusResponse:
        oauth_servers = [server for server in servers if _operator_oauth_enabled(server)]
        if not oauth_servers:
            return McpOperatorAuthStatusResponse()
        server_ids = [server.id for server in oauth_servers]
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(McpOperatorOAuthAssociation)
                .where(McpOperatorOAuthAssociation.operator_principal == operator_principal)
                .where(McpOperatorOAuthAssociation.server_id.in_(server_ids))
            ).all()
        by_server = {row.server_id: row for row in rows}
        return McpOperatorAuthStatusResponse(
            associations=[
                _oauth_status_from_row(server.id, operator_principal, by_server.get(server.id))
                for server in oauth_servers
            ]
        )

    async def start_flow(
        self, *, server: McpServerEntry, operator_principal: str, public_base_url: str
    ) -> McpOperatorAuthStartResponse:
        if not _operator_oauth_enabled(server):
            raise HTTPException(status_code=404, detail=f"MCP server {server.id} does not use operator OAuth")
        flow = await _build_operator_oauth_flow(server, operator_principal, public_base_url.rstrip("/"))
        with self._sessions.begin() as session:
            now = dt.datetime.now(dt.UTC)
            session.execute(delete(McpOperatorOAuthFlow).where(McpOperatorOAuthFlow.expires_at < now))
            session.execute(
                delete(McpOperatorOAuthFlow)
                .where(McpOperatorOAuthFlow.server_id == server.id)
                .where(McpOperatorOAuthFlow.operator_principal == operator_principal)
            )
            session.add(
                McpOperatorOAuthFlow(
                    state=flow.state,
                    server_id=server.id,
                    operator_principal=operator_principal,
                    created_at=now,
                    expires_at=flow.expires_at,
                    redirect_uri=flow.redirect_uri,
                    code_verifier=flow.code_verifier,
                    client_id=flow.client_info.client_id or "",
                    client_secret=flow.client_info.client_secret,
                    client_secret_expires_at=flow.client_info.client_secret_expires_at,
                    token_endpoint_auth_method=flow.client_info.token_endpoint_auth_method,
                    token_endpoint=flow.token_endpoint,
                    resource=flow.resource,
                    scope=flow.scope,
                )
            )
        return McpOperatorAuthStartResponse(
            server_id=server.id, authorization_url=flow.authorization_url, expires_at=flow.expires_at
        )

    async def complete_callback(self, *, state: str, code: str) -> McpOperatorAuthStatus:
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthFlow, state)
            if row is None:
                raise HTTPException(status_code=404, detail="OAuth flow not found or already used")
            session.delete(row)
            flow = _oauth_flow_snapshot(row)
        now = dt.datetime.now(dt.UTC)
        if flow["expires_at"] < now:
            raise HTTPException(status_code=410, detail="OAuth flow expired; start connection again")
        token = await _exchange_operator_oauth_code(flow, code)
        token_expires_at = _token_expires_at(token, now)
        with self._sessions.begin() as session:
            existing = session.get(
                McpOperatorOAuthAssociation, (flow["server_id"], flow["operator_principal"]), with_for_update=True
            )
            if existing is None:
                existing = McpOperatorOAuthAssociation(
                    server_id=flow["server_id"],
                    operator_principal=flow["operator_principal"],
                    created_at=now,
                    updated_at=now,
                    client_id=flow["client_id"],
                    client_secret=flow["client_secret"],
                    client_secret_expires_at=flow["client_secret_expires_at"],
                    token_endpoint_auth_method=flow["token_endpoint_auth_method"],
                    token_endpoint=flow["token_endpoint"],
                    resource=flow["resource"],
                    access_token=token.access_token,
                    refresh_token=token.refresh_token,
                    token_type=token.token_type,
                    scope=token.scope or flow["scope"],
                    token_expires_at=token_expires_at,
                )
                session.add(existing)
            else:
                existing.updated_at = now
                existing.client_id = flow["client_id"]
                existing.client_secret = flow["client_secret"]
                existing.client_secret_expires_at = flow["client_secret_expires_at"]
                existing.token_endpoint_auth_method = flow["token_endpoint_auth_method"]
                existing.token_endpoint = flow["token_endpoint"]
                existing.resource = flow["resource"]
                existing.access_token = token.access_token
                existing.refresh_token = token.refresh_token
                existing.token_type = token.token_type
                existing.scope = token.scope or flow["scope"]
                existing.token_expires_at = token_expires_at
            return _oauth_status_from_row(flow["server_id"], flow["operator_principal"], existing)

    def disconnect(self, *, server_id: str, operator_principal: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server_id, operator_principal), with_for_update=True)
            if row is not None:
                session.delete(row)

    async def access_token_for(self, *, server: McpServerEntry, operator_principal: str) -> str | None:
        if not _operator_oauth_enabled(server):
            return None
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_principal), with_for_update=True)
            if row is None:
                return None
            now = dt.datetime.now(dt.UTC)
            if row.token_expires_at is None or row.token_expires_at > now + MCP_OPERATOR_AUTH_REFRESH_SKEW:
                return row.access_token
            if not row.refresh_token:
                return None
            snapshot = _oauth_association_snapshot(row)
        refreshed = await _refresh_operator_oauth_token(snapshot)
        token_expires_at = _token_expires_at(refreshed, dt.datetime.now(dt.UTC))
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_principal), with_for_update=True)
            if row is None:
                return None
            row.updated_at = dt.datetime.now(dt.UTC)
            row.access_token = refreshed.access_token
            row.refresh_token = refreshed.refresh_token or row.refresh_token
            row.token_type = refreshed.token_type
            row.scope = refreshed.scope or row.scope
            row.token_expires_at = token_expires_at
            return row.access_token


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
    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        async with Client(server.server_url, auth=auth_token) as client:
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


def _oauth_store(request: Request) -> McpOperatorOAuthStoreProtocol:
    store = request.app.state.mcp_operator_oauth_store
    if store is None:
        raise HTTPException(status_code=503, detail="MCP operator OAuth database is not configured")
    return cast(McpOperatorOAuthStoreProtocol, store)


LedgerDep = Annotated[ToolCallLedgerProtocol, Depends(_ledger)]
HubDep = Annotated[ToolCallEventHub, Depends(_hub)]
ExecutorDep = Annotated[McpToolExecutor, Depends(_executor)]
MetadataProviderDep = Annotated[McpMetadataProvider, Depends(_metadata_provider)]
OAuthStoreDep = Annotated[McpOperatorOAuthStoreProtocol, Depends(_oauth_store)]


class _BuiltOperatorOAuthFlow(BaseModel):
    state: str
    authorization_url: str
    expires_at: dt.datetime
    redirect_uri: str
    code_verifier: str
    client_info: OAuthClientInformationFull
    token_endpoint: str
    resource: str | None = None
    scope: str | None = None


def _operator_oauth_enabled(server: McpServerEntry) -> bool:
    return bool(server.operator_oauth and server.operator_oauth.enabled)


def _oauth_status_from_row(
    server_id: str, operator_principal: str, row: McpOperatorOAuthAssociation | None
) -> McpOperatorAuthStatus:
    if row is None:
        return McpOperatorAuthStatus(server_id=server_id, status="unconnected", operator_principal=operator_principal)
    return McpOperatorAuthStatus(
        server_id=server_id,
        status="connected",
        operator_principal=operator_principal,
        connected_at=row.created_at,
        token_expires_at=row.token_expires_at,
        scope=row.scope,
    )


def _token_expires_at(token: OAuthToken, now: dt.datetime) -> dt.datetime | None:
    if token.expires_in is None:
        return None
    return now + dt.timedelta(seconds=token.expires_in)


def _oauth_flow_snapshot(row: McpOperatorOAuthFlow) -> dict[str, Any]:
    return {
        "state": row.state,
        "server_id": row.server_id,
        "operator_principal": row.operator_principal,
        "expires_at": row.expires_at,
        "redirect_uri": row.redirect_uri,
        "code_verifier": row.code_verifier,
        "client_id": row.client_id,
        "client_secret": row.client_secret,
        "client_secret_expires_at": row.client_secret_expires_at,
        "token_endpoint_auth_method": row.token_endpoint_auth_method,
        "token_endpoint": row.token_endpoint,
        "resource": row.resource,
        "scope": row.scope,
    }


def _oauth_association_snapshot(row: McpOperatorOAuthAssociation) -> dict[str, Any]:
    return {
        "server_id": row.server_id,
        "operator_principal": row.operator_principal,
        "client_id": row.client_id,
        "client_secret": row.client_secret,
        "token_endpoint_auth_method": row.token_endpoint_auth_method,
        "token_endpoint": row.token_endpoint,
        "resource": row.resource,
        "refresh_token": row.refresh_token,
    }


def _metadata_request_headers() -> dict[str, str]:
    return {MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION}


async def _discover_protected_resource(
    client: httpx.AsyncClient, server_url: str, auth_probe: httpx.Response
) -> ProtectedResourceMetadata | None:
    metadata_url = extract_resource_metadata_from_www_auth(auth_probe)
    for url in build_protected_resource_metadata_discovery_urls(metadata_url, server_url):
        response = await client.get(url, headers=_metadata_request_headers())
        metadata = await handle_protected_resource_response(response)
        if metadata:
            return metadata
    return None


async def _discover_oauth_metadata(
    client: httpx.AsyncClient, server_url: str, resource_metadata: ProtectedResourceMetadata | None
) -> OAuthMetadata | None:
    auth_server_url = str(resource_metadata.authorization_servers[0]) if resource_metadata else None
    for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url):
        response = await client.get(url, headers=_metadata_request_headers())
        ok, metadata = await handle_auth_metadata_response(response)
        if metadata:
            return metadata
        if not ok:
            break
    return None


def _resource_for_oauth(server_url: str, resource_metadata: ProtectedResourceMetadata | None) -> str | None:
    if resource_metadata is None:
        return None
    requested = resource_url_from_server_url(server_url)
    configured = str(resource_metadata.resource)
    if check_resource_allowed(requested_resource=requested, configured_resource=configured):
        return configured
    return requested


async def _register_oauth_client(
    client: httpx.AsyncClient,
    server_url: str,
    oauth_metadata: OAuthMetadata | None,
    client_metadata: OAuthClientMetadata,
) -> OAuthClientInformationFull:
    if oauth_metadata and oauth_metadata.registration_endpoint:
        registration_url = str(oauth_metadata.registration_endpoint)
    else:
        registration_url = urljoin(_authorization_base_url(server_url), "/register")
    response = await client.post(
        registration_url,
        json=client_metadata.model_dump(by_alias=True, mode="json", exclude_none=True),
        headers={"Content-Type": "application/json"},
    )
    return await handle_registration_response(response)


def _authorization_base_url(server_url: str) -> str:
    parsed = httpx.URL(server_url)
    return f"{parsed.scheme}://{parsed.host}{f':{parsed.port}' if parsed.port else ''}"


async def _build_operator_oauth_flow(
    server: McpServerEntry, operator_principal: str, public_base_url: str
) -> _BuiltOperatorOAuthFlow:
    assert server.operator_oauth is not None
    redirect_uri = f"{public_base_url}{MCP_OPERATOR_AUTH_CALLBACK_PATH}"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            auth_probe = await client.get(server.server_url, headers=_metadata_request_headers())
            resource_metadata = await _discover_protected_resource(client, server.server_url, auth_probe)
            oauth_metadata = await _discover_oauth_metadata(client, server.server_url, resource_metadata)
            scope = (
                " ".join(server.operator_oauth.scopes)
                if server.operator_oauth.scopes is not None
                else get_client_metadata_scopes(
                    extract_scope_from_www_auth(auth_probe), resource_metadata, oauth_metadata
                )
            )
            client_metadata = OAuthClientMetadata(
                client_name=server.operator_oauth.client_name,
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=scope,
            )
            client_info = await _register_oauth_client(client, server.server_url, oauth_metadata, client_metadata)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to start MCP OAuth flow for {server.id}: {e}") from e

    if not client_info.client_id:
        raise HTTPException(status_code=502, detail=f"MCP OAuth registration for {server.id} did not return client_id")
    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    if oauth_metadata and oauth_metadata.authorization_endpoint:
        auth_endpoint = str(oauth_metadata.authorization_endpoint)
    else:
        auth_endpoint = urljoin(_authorization_base_url(server.server_url), "/authorize")
    if oauth_metadata and oauth_metadata.token_endpoint:
        token_endpoint = str(oauth_metadata.token_endpoint)
    else:
        token_endpoint = urljoin(_authorization_base_url(server.server_url), "/token")
    resource = _resource_for_oauth(server.server_url, resource_metadata)
    params = {
        "response_type": "code",
        "client_id": client_info.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = resource
    if scope:
        params["scope"] = scope
    return _BuiltOperatorOAuthFlow(
        state=state,
        authorization_url=f"{auth_endpoint}?{urlencode(params)}",
        expires_at=dt.datetime.now(dt.UTC) + MCP_OPERATOR_AUTH_FLOW_TTL,
        redirect_uri=redirect_uri,
        code_verifier=pkce.code_verifier,
        client_info=client_info,
        token_endpoint=token_endpoint,
        resource=resource,
        scope=scope,
    )


def _token_request_auth(
    data: dict[str, str], *, client_id: str, client_secret: str | None, token_endpoint_auth_method: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token_endpoint_auth_method == "client_secret_basic" and client_secret:
        encoded_id = quote(client_id, safe="")
        encoded_secret = quote(client_secret, safe="")
        credentials = base64.b64encode(f"{encoded_id}:{encoded_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    elif token_endpoint_auth_method == "client_secret_post" and client_secret:
        data["client_secret"] = client_secret
    return data, headers


async def _exchange_operator_oauth_code(flow: dict[str, Any], code: str) -> OAuthToken:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "client_id": flow["client_id"],
        "code_verifier": flow["code_verifier"],
    }
    if flow["resource"]:
        data["resource"] = flow["resource"]
    data, headers = _token_request_auth(
        data,
        client_id=flow["client_id"],
        client_secret=flow["client_secret"],
        token_endpoint_auth_method=flow["token_endpoint_auth_method"],
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(flow["token_endpoint"], data=data, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"MCP OAuth token exchange failed: {response.status_code}")
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise HTTPException(status_code=502, detail=f"MCP OAuth token response was invalid: {e}") from e


async def _refresh_operator_oauth_token(association: dict[str, Any]) -> OAuthToken:
    refresh_token = association["refresh_token"]
    if not refresh_token:
        raise RuntimeError("MCP OAuth association has no refresh token; reconnect in the console")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": association["client_id"],
    }
    if association["resource"]:
        data["resource"] = association["resource"]
    data, headers = _token_request_auth(
        data,
        client_id=association["client_id"],
        client_secret=association["client_secret"],
        token_endpoint_auth_method=association["token_endpoint_auth_method"],
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(association["token_endpoint"], data=data, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"MCP OAuth token refresh failed: {response.status_code}")
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise RuntimeError(f"MCP OAuth refresh response was invalid: {e}") from e


def _oauth_callback_response(ok: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "MCP account connected" if ok else "MCP account connection failed"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    payload = "mcpAuthChanged" if ok else "mcpAuthFailed"
    return HTMLResponse(
        status_code=status_code,
        content=f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{safe_title}</title>
    <style>
      body {{
        color-scheme: light dark;
        font: 16px system-ui, sans-serif;
        margin: 3rem auto;
        max-width: 36rem;
        line-height: 1.4;
      }}
      code {{
        overflow-wrap: anywhere;
      }}
    </style>
  </head>
  <body>
    <h1>{safe_title}</h1>
    <p><code>{safe_message}</code></p>
    <script>
      try {{
        new BroadcastChannel("haku-console-mcp-auth").postMessage({{ type: "{payload}" }});
      }} catch (_) {{}}
    </script>
  </body>
</html>""",
    )


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


def _operator_principal(request: Request) -> str:
    return request.headers.get("x-authentik-username") or "operator"


def _public_base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()
    host = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
        .split(",", 1)[0]
        .strip()
    )
    return f"{proto}://{host}"


async def _execution_auth(
    server: McpServerEntry, operator_principal: str, oauth_store: McpOperatorOAuthStoreProtocol
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


async def _maybe_execute(
    record: ToolCallRecord,
    server: McpServerEntry,
    ledger: ToolCallLedgerProtocol,
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
    oauth_store: OAuthStoreDep,
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
        auth_token = await _execution_auth(server, caller, oauth_store)
        record = await _maybe_execute(record, server, ledger, hub, executor, auth_token)
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


@router.get("/api/mcp/operator-auth")
async def mcp_operator_auth_statuses(
    request: Request, settings: SettingsDep, oauth_store: OAuthStoreDep
) -> McpOperatorAuthStatusResponse:
    return oauth_store.list_statuses(servers=_load_servers(settings), operator_principal=_operator_principal(request))


@router.post("/api/mcp/operator-auth/{server_id}/start")
async def start_mcp_operator_auth(
    server_id: str, request: Request, csrf_protect: Csrf, settings: SettingsDep, oauth_store: OAuthStoreDep
) -> McpOperatorAuthStartResponse:
    await csrf_protect.validate_csrf(request)
    server = _server_entry(settings, server_id)
    return await oauth_store.start_flow(
        server=server,
        operator_principal=_operator_principal(request),
        public_base_url=_public_base_url(request, settings),
    )


@router.delete("/api/mcp/operator-auth/{server_id}")
async def disconnect_mcp_operator_auth(
    server_id: str, request: Request, csrf_protect: Csrf, oauth_store: OAuthStoreDep
) -> McpOperatorAuthStatus:
    await csrf_protect.validate_csrf(request)
    operator_principal = _operator_principal(request)
    oauth_store.disconnect(server_id=server_id, operator_principal=operator_principal)
    return McpOperatorAuthStatus(server_id=server_id, status="unconnected", operator_principal=operator_principal)


@router.get(MCP_OPERATOR_AUTH_CALLBACK_PATH)
async def mcp_operator_auth_callback(
    oauth_store: OAuthStoreDep, state: str | None = None, code: str | None = None, error: str | None = None
) -> HTMLResponse:
    if error:
        return _oauth_callback_response(False, f"MCP OAuth authorization failed: {error}", status_code=400)
    if not state or not code:
        return _oauth_callback_response(False, "MCP OAuth callback is missing state or code.", status_code=400)
    try:
        status = await oauth_store.complete_callback(state=state, code=code)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "MCP OAuth callback failed."
        return _oauth_callback_response(False, detail, status_code=e.status_code)
    return _oauth_callback_response(True, f"Connected {status.server_id} for {status.operator_principal}.")


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
