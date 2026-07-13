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
import datetime
import logging
import secrets
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from mcp import types as mcp_types
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console import operator_auth
from haku.console.auto_approval import auto_approve_tool_call
from haku.console.config import Settings
from haku.console.console_events import ConsoleEventHub, ConsoleEventHubDep
from haku.console.database_schema import McpToolCall, McpToolCallEvent
from haku.console.deps import SettingsDep
from haku.console.mcp_config import (
    InProcessServers,
    McpServerEntry,
    ResolvedStaticAgent,
    _credential_token,
    _load_servers,
    _operator_oauth_enabled,
    _server_entry,
    _transport,
)
from haku.console.mcp_operator_oauth import (
    OAuthStoreDep,
    PostgresMcpOperatorOAuthStore,
    _oauth_store,
    _operator_subject,
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

# Operator-only routes (reflection, approvals, decisions). app.py guards this router with
# `require_operator`.
router = APIRouter(tags=["mcp-approval"])
# The agent-facing tool-call routes (submit + read/sweep results). app.py guards this router with
# `require_operator_or_static_agent`, so a static agent's bearer — not just an operator session —
# reaches them, and nothing else.
agent_router = APIRouter(tags=["mcp-approval"])
Csrf = Annotated[CsrfProtect, Depends()]


TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}
DEV_OPERATOR_SUBJECT = "development-operator"


class ToolMetadataBase(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


def _operator_event_subject(request: Request, settings: Settings) -> str:
    """Audience for operator-facing events.

    Production reads the authenticated OIDC ``sub``. The OIDC-unset local/test mode has no browser
    identity at all, so use one explicit development audience rather than weakening the production
    path or making approval execution depend on an unavailable session.
    """
    subject = operator_auth.operator_subject(request)
    if subject is not None:
        return subject
    if settings.operator_oidc is None:
        return DEV_OPERATOR_SUBJECT
    raise HTTPException(status_code=401, detail="no authenticated operator subject on the request")


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
        # Migrations are applied once at startup (haku.console.database_migrate.apply_migrations), not
        # here — constructing a ledger neither connects nor mutates schema.
        self._engine = create_engine(database_url, pool_pre_ping=True)
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
    return cast(PostgresToolCallLedger, request.app.state.tool_call_ledger)


def _static_agents(request: Request) -> list[ResolvedStaticAgent]:
    return cast("list[ResolvedStaticAgent]", request.app.state.static_agents)


def _executor(request: Request) -> McpToolExecutor:
    return cast(McpToolExecutor, request.app.state.tool_call_executor)


def _metadata_provider(request: Request) -> McpMetadataProvider:
    return cast(McpMetadataProvider, request.app.state.tool_call_metadata_provider)


LedgerDep = Annotated[PostgresToolCallLedger, Depends(_ledger)]
ExecutorDep = Annotated[McpToolExecutor, Depends(_executor)]
MetadataProviderDep = Annotated[McpMetadataProvider, Depends(_metadata_provider)]
StaticAgentsDep = Annotated[list[ResolvedStaticAgent], Depends(_static_agents)]


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


def _caller_principal(request: Request, static_agents: StaticAgentsDep) -> str:
    """The audit identity for an `/api/tool-calls` caller: a configured static agent (matched by its
    bearer), else the operator session, else `operator`. A POST with a configured agent present but no
    valid credential is a 401 rather than a silent operator fallback."""
    agent = operator_auth.authenticated_static_agent(request, static_agents)
    if agent is not None:
        return agent.agent
    operator = operator_auth.operator_username(request)
    if operator:
        return operator
    if static_agents and request.method == "POST" and request.url.path == "/api/tool-calls":
        raise HTTPException(status_code=401, detail="missing or invalid tool-call API token")
    return "operator"


CallerPrincipalDep = Annotated[str, Depends(_caller_principal)]


def operator_subject_for_agent(
    caller_principal: str, static_agents: list[ResolvedStaticAgent], oauth_store: PostgresMcpOperatorOAuthStore
) -> str | None:
    """The operator subject bound to an authenticated agent.

    A configured static agent → its explicit `operator_subject` binding; any other authenticated agent
    is an OAuth DCR client → the operator it linked at connect (`None` if unlinked, which fails closed
    into the 409 "connect your account" path for operator-backed execution)."""
    for agent in static_agents:
        if agent.agent == caller_principal:
            return agent.operator_subject
    return oauth_store.agent_operator(agent_dcr_client_id=caller_principal)


async def _execution_auth(
    server: McpServerEntry, operator_subject: str, oauth_store: PostgresMcpOperatorOAuthStore
) -> str | None:
    if _operator_oauth_enabled(server):
        token = await oauth_store.access_token_for(server=server, operator_subject=operator_subject)
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
    # The operator subject is only consulted for operator_oauth servers; for others the credential
    # token is used, so don't require an operator subject on the request there.
    operator_subject = _operator_subject(request) if _operator_oauth_enabled(server) else ""
    auth_token = await _execution_auth(server, operator_subject, oauth_store)
    return Client(_transport(server, {}), auth=auth_token)


async def _maybe_execute(
    record: ToolCallRecord,
    server: McpServerEntry,
    ledger: PostgresToolCallLedger,
    hub: ConsoleEventHub,
    executor: McpToolExecutor,
    auth_token: str | None,
    operator_subject: str,
) -> ToolCallRecord:
    if record.status != ToolCallStatus.RUNNING:
        return record
    try:
        result = await executor.execute(server, record.tool_name, record.arguments, auth_token)
    except Exception as e:
        updated, event = ledger.finish(record.tool_call_id, result=None, error=str(e))
    else:
        updated, event = ledger.finish(record.tool_call_id, result=result, error=None)
    await hub.broadcast(operator_subject, [event])
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
        oauth_store = _oauth_store(request)
        auth_token = await oauth_store.access_token_for(server=server, operator_subject=_operator_subject(request))
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


async def submit_and_wait(
    *,
    req: SubmitToolCallRequest,
    caller_principal: str,
    operator_subject: str,
    caller_is_agent: bool,
    static_agents: list[ResolvedStaticAgent],
    settings: Settings,
    ledger: PostgresToolCallLedger,
    hub: ConsoleEventHub,
    executor: McpToolExecutor,
    oauth_store: PostgresMcpOperatorOAuthStore,
    in_process_servers: InProcessServers,
    gmail_client: GmailToolsClient | None,
) -> ToolCallRecord:
    """Submit a tool call, auto-approve + execute if policy allows, then wait up to
    ``req.wait_for_ms`` for a terminal status. The single execution/approval path shared by the
    HTTP endpoint (``POST /api/tool-calls``) and the console MCP server (``mcp_server``).
    ``caller_is_agent`` gates the auto-approval policy (static_agents, not interactive operators)."""
    server = _server_entry(settings, req.server_id)
    auto_approval_policy_id, auto_approval_evaluation = await auto_approve_tool_call(
        caller_is_agent=caller_is_agent,
        server_id=server.id,
        tool_name=req.tool_name,
        arguments=req.arguments,
        label_prefix=settings.gmail_auto_approve_label_prefix,
        gmail=gmail_client,
        mcp=in_process_servers.get(server.id),
    )
    record, events, created = ledger.submit(
        server=server,
        req=req,
        caller_principal=caller_principal,
        auto_approval_policy_id=auto_approval_policy_id,
        auto_approval_evaluation=auto_approval_evaluation,
    )
    await hub.broadcast(operator_subject, events)
    if created:
        logger.info(
            "tool call %s submitted status=%s server=%s tool=%s caller=%s approval_policy=%s auto_approval=%s",
            record.tool_call_id,
            record.status,
            record.server_id,
            record.tool_name,
            caller_principal,
            record.approval_policy_id,
            record.auto_approval_evaluation,
        )
    if record.status == ToolCallStatus.RUNNING:
        # An auto-approved operator_oauth call executes as an operator, not the agent (an agent has no
        # operator token of its own): a static agent uses its configured `operator_subject`, an OAuth
        # agent the operator it linked at connect. Everything else (in-process / static-credential
        # servers) uses the caller's own resolved credential.
        execution_principal = caller_principal
        if _operator_oauth_enabled(server):
            execution_principal = (
                operator_subject_for_agent(caller_principal, static_agents, oauth_store) or caller_principal
            )
        auth_token = await _execution_auth(server, execution_principal, oauth_store)
        record = await _maybe_execute(record, server, ledger, hub, executor, auth_token, operator_subject)
    return await _wait_terminal(ledger, record.tool_call_id, req.wait_for_ms)


@agent_router.post("/api/tool-calls")
async def submit_tool_call(
    body: SubmitToolCallRequest,
    request: Request,
    settings: SettingsDep,
    ledger: LedgerDep,
    hub: ConsoleEventHubDep,
    executor: ExecutorDep,
    oauth_store: OAuthStoreDep,
    static_agents: StaticAgentsDep,
    caller: CallerPrincipalDep,
) -> ToolCallRecord:
    operator_subject = (
        operator_auth.operator_subject(request)
        or operator_subject_for_agent(caller, static_agents, oauth_store)
        or caller
    )
    return await submit_and_wait(
        req=body,
        caller_principal=caller,
        operator_subject=operator_subject,
        caller_is_agent=any(caller == agent.agent for agent in static_agents),
        static_agents=static_agents,
        settings=settings,
        ledger=ledger,
        hub=hub,
        executor=executor,
        oauth_store=oauth_store,
        in_process_servers=cast(InProcessServers, request.app.state.in_process_servers),
        gmail_client=cast(GmailToolsClient | None, request.app.state.gmail_client),
    )


@agent_router.get("/api/tool-calls")
async def list_tool_calls(
    ledger: LedgerDep,
    status: Annotated[list[ToolCallStatus] | None, Query()] = None,
    since: datetime.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    newest_first: bool = False,
) -> ToolCallListResponse:
    return ledger.list(statuses=status, since=since, limit=limit, newest_first=newest_first)


@agent_router.get("/api/tool-calls/{tool_call_id}")
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
    hub: ConsoleEventHubDep,
    executor: ExecutorDep,
    oauth_store: OAuthStoreDep,
) -> ApprovalDecisionResponse:
    await csrf_protect.validate_csrf(request)
    event_operator_subject = _operator_event_subject(request, settings)
    if body.decision == "deny":
        record, event = ledger.deny(tool_call_id, body.reason)
        await hub.broadcast(event_operator_subject, [event])
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
    # Only operator_oauth execution runs as the approving operator; other servers use their configured
    # credential, so an operator subject isn't required to approve a call there.
    execution_subject = event_operator_subject if _operator_oauth_enabled(server) else ""
    auth_token = await _execution_auth(server, execution_subject, oauth_store)
    running, running_event = ledger.mark_running(tool_call_id)
    await hub.broadcast(event_operator_subject, [running_event])
    finished = await _maybe_execute(running, server, ledger, hub, executor, auth_token, event_operator_subject)
    return ApprovalDecisionResponse(tool_call=finished)
