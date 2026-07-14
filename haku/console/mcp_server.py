"""haku-console's own MCP server: the connected-server tools, re-exposed to a Claude agent.

An interactive MCP client (the claude.ai custom connector or the ``claude`` CLI) connects here
and calls the console's connected-server tools directly. Every call is **submitted to the existing
approval queue rather than executed inline** (`mcp_approval.submit_and_wait`): it auto-approves +
runs when the reviewed policy allows, otherwise it returns a **promise** (a pending
``tool_call_id`` plus an operator-facing deep link) that the agent resolves later via
``get_tool_call``.

The tool surface has two buckets (v1: uniform across agents, driven by the global auto-approval
policy):

Every proxied tool is named ``<server>_<tool>`` (one uniform format — operator decision
2026-07-13; bare upstream names hid which server a tool belonged to):

- **Pass-through** — tools the policy unconditionally auto-approves (gmail reads): the upstream
  schema and description unchanged, so they behave like the real tool and return the real result.
- **Request** — everything else: an envelope schema (``input`` + ``rationale`` + optional
  ``title``/``wait_for_approval_ms``) and a promise-semantics preamble in the description;
  returns the real result *or* a promise.

Both buckets, and ``POST /api/tool-calls``, run through the single ``submit_and_wait`` path.
"""

from __future__ import annotations

import copy
import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import Tool, ToolResult
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from haku.console.auto_approval import is_unconditionally_auto_approved
from haku.console.config import Settings
from haku.console.console_events import ConsoleEventHub
from haku.console.mcp_approval import (
    AgentToolCallScope,
    DegradedServerMetadata,
    McpMetadataProvider,
    McpToolExecutor,
    PostgresToolCallLedger,
    ServerMetadata,
    ToolCallListResponse,
    resolve_mcp_agent,
    submit_and_wait,
)
from haku.console.mcp_config import (
    InProcessServers,
    McpServerEntry,
    ResolvedStaticAgent,
    _credential_token,
    _load_servers,
    _operator_oauth_enabled,
    static_agent_client_id,
)
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.tool_calls import SubmitToolCallRequest, ToolCallRecord, ToolCallStatus
from haku.console.tools.gmail_client import GmailToolsClient
from mcp_infra.authentik_auth.auth import OnClientAuthorized, build_authentik_auth
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

logger = logging.getLogger(__name__)

SERVER_NAME = "haku-console"
# Synchronous hold budget (ms) before a call returns a promise; overridable per envelope call.
DEFAULT_WAIT_MS = 5000
MAX_WAIT_MS = 60_000

INSTRUCTIONS = (
    "haku-console tool proxy. Every proxied tool is named `<server>_<tool>`. Tools whose schema "
    "wraps the real arguments in an `input` + `rationale` envelope submit a request to the "
    "operator's approval queue rather than running immediately: they return the tool's result if "
    "it is approved within a few seconds, otherwise a promise (a pending `tool_call_id` and a "
    "link the operator can approve at). Poll `get_tool_call(tool_call_id)` to resolve a promise. "
    "Tools with the upstream tool's own schema auto-approve and return their result directly."
)

_REQUEST_PREAMBLE = (
    "Submits a request to run `{tool}` on `{server}` through haku-console's operator-approval "
    "queue — it does NOT run it directly. Put the real tool arguments under `input`. Returns the "
    "result if approved within `wait_for_approval_ms`, otherwise a promise (a pending "
    "`tool_call_id` plus an approval `url`) — poll `get_tool_call(tool_call_id)` to resolve it."
)


@dataclass(frozen=True)
class ConsoleMcpContext:
    """The app-state singletons the MCP tools need — the same objects the HTTP router uses."""

    settings: Settings
    static_agents: list[ResolvedStaticAgent]
    ledger: PostgresToolCallLedger
    hub: ConsoleEventHub
    executor: McpToolExecutor
    oauth_store: PostgresMcpOperatorOAuthStore
    metadata_provider: McpMetadataProvider
    in_process_servers: InProcessServers
    gmail_client: GmailToolsClient | None


class ToolCallPromise(BaseModel):
    """Returned by an approval-envelope tool when the call is not yet terminal."""

    status: ToolCallStatus
    tool_call_id: str
    url: str | None = Field(default=None, description="Operator-facing link to approve this call.")
    message: str


class ToolCallView(BaseModel):
    """A tool-call record plus its operator-facing deep link, for the read tools."""

    call: ToolCallRecord
    url: str | None = None


class ApprovalRequestEnvelope(BaseModel):
    """The approval-request envelope. One model drives both the generated input schema
    (`_envelope_schema`) and parsing the incoming call (`ProxyTool.run`)."""

    input: dict[str, Any] = Field(description="The real arguments for the upstream tool.")
    rationale: str = Field(description="Why you are requesting this call. Shown to the operator.")
    title: str | None = Field(default=None, description="Short human-facing title for the operator's approval queue.")
    wait_for_approval_ms: int | None = Field(
        default=None,
        description=(
            "How long to wait synchronously for approval before returning a promise "
            f"(default {DEFAULT_WAIT_MS}, max {MAX_WAIT_MS})."
        ),
    )


def _agent_id() -> str:
    """The calling agent's identity: an OAuth DCR client_id, or a static agent's configured id (the
    static bearer verifier maps each agent token to its `client_id`). Every `/mcp` caller is
    authenticated, so a missing token is a bug, not an anonymous call."""
    token = get_access_token()
    if token is None or not token.client_id:
        raise RuntimeError("no authenticated agent on the MCP request")
    return token.client_id


def _sanitize_prefix(server_id: str) -> str:
    """Server id → a tool-name-safe prefix (``kubectl-passthrough-mcp`` → ``kubectl_passthrough_mcp``)."""
    return re.sub(r"[^a-z0-9]+", "_", server_id.lower()).strip("_")


def _tool_call_url(settings: Settings, tool_call_id: str) -> str | None:
    if settings.ui_base_url is None:
        return None
    return f"{settings.ui_base_url.rstrip('/')}/tool-calls/{tool_call_id}"


def _envelope_schema(original_schema: dict[str, Any]) -> dict[str, Any]:
    """The approval-request envelope schema: `ApprovalRequestEnvelope`'s generated schema with the
    ``input`` property replaced by the upstream tool's own schema (nested unchanged, so its fields
    can't collide with the envelope's ``rationale``/``title``/``wait_for_approval_ms``)."""
    schema = ApprovalRequestEnvelope.model_json_schema()
    schema["properties"]["input"] = copy.deepcopy(original_schema)
    schema.pop("title", None)  # the model class title; the object schema itself needs none
    return schema


def _record_to_result(record: ToolCallRecord, settings: Settings) -> ToolResult:
    """Map a (possibly non-terminal) tool-call record to an MCP tool result.

    Terminal ok → the upstream result; error/denied → ``ToolError``; still pending/running → a
    promise (pending id + approval url).
    """
    if record.status == ToolCallStatus.OK:
        upstream = mcp_types.CallToolResult.model_validate(record.result or {"content": []})
        content: list[mcp_types.ContentBlock] = list(upstream.content) or [
            mcp_types.TextContent(type="text", text="(tool returned no content)")
        ]
        return ToolResult(content=content, structured_content=upstream.structuredContent)
    if record.status == ToolCallStatus.ERROR:
        raise ToolError(record.error or "tool call failed")
    if record.status == ToolCallStatus.DENIED:
        raise ToolError(f"denied: {record.denial_reason or 'no reason given'}")
    url = _tool_call_url(settings, record.tool_call_id)
    approve = f" Open {url} to approve." if url else ""
    promise = ToolCallPromise(
        status=record.status,
        tool_call_id=record.tool_call_id,
        url=url,
        message=(
            f"Sent for approval; pending as {record.tool_call_id}.{approve} "
            f"Poll get_tool_call('{record.tool_call_id}') for the result."
        ),
    )
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=promise.message)],
        structured_content=promise.model_dump(mode="json"),
    )


class ProxyTool(Tool):
    """A connected-server tool re-exposed through the approval queue.

    ``passthrough`` tools advertise the upstream schema and take the raw args; envelope tools
    advertise the envelope and read the call args from ``input``. Both route through
    ``submit_and_wait``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: ConsoleMcpContext
    server_id: str
    upstream_tool_name: str
    passthrough: bool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.passthrough:
            call_args, rationale, title, wait_ms = arguments, "", None, DEFAULT_WAIT_MS
        else:
            env = ApprovalRequestEnvelope.model_validate(arguments)
            call_args, rationale, title = env.input, env.rationale, env.title
            wait_ms = env.wait_for_approval_ms or DEFAULT_WAIT_MS
        ctx = self.context
        req = SubmitToolCallRequest(
            server_id=self.server_id,
            tool_name=self.upstream_tool_name,
            arguments=call_args,
            rationale=rationale,
            title=title,
            wait_for_ms=max(0, min(int(wait_ms), MAX_WAIT_MS)),
        )
        client_id = _agent_id()
        caller = resolve_mcp_agent(client_id, ctx.static_agents, ctx.oauth_store)
        if caller is None:
            raise RuntimeError(f"agent {client_id} has no linked operator subject")
        record = await submit_and_wait(
            req=req,
            caller_principal=caller.principal,
            operator_subject=caller.operator_subject,
            caller_is_agent=True,
            settings=ctx.settings,
            ledger=ctx.ledger,
            hub=ctx.hub,
            executor=ctx.executor,
            oauth_store=ctx.oauth_store,
            in_process_servers=ctx.in_process_servers,
            gmail_client=ctx.gmail_client,
        )
        return _record_to_result(record, ctx.settings)


def _build_proxy_tool(context: ConsoleMcpContext, server_id: str, tool: Any, *, passthrough: bool) -> ProxyTool:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) and tool.input_schema else {"type": "object"}
    # One uniform name format for both buckets — approval semantics live in the schema and
    # description, never in the name (operator decision 2026-07-13).
    name = build_mcp_function(MCPMountPrefix(_sanitize_prefix(server_id)), tool.name)
    if passthrough:
        parameters = schema
        description = tool.description or ""
    else:
        parameters = _envelope_schema(schema)
        preamble = _REQUEST_PREAMBLE.format(tool=tool.name, server=server_id)
        description = f"{preamble}\n\n{tool.description}" if tool.description else preamble
    return ProxyTool(
        name=name,
        description=description,
        parameters=parameters,
        context=context,
        server_id=server_id,
        upstream_tool_name=tool.name,
        passthrough=passthrough,
    )


async def _reflect_server(context: ConsoleMcpContext, server: McpServerEntry) -> ServerMetadata:
    """Reflect one connected server's tools, mirroring ``mcp_approval._metadata_for_request``.

    in-process / static-bearer servers use their configured credential; operator_oauth servers reflect
    as one of the console's static-agent operators (``tools/list`` is operator-independent) — the first
    whose operator subject has a connected token — and degrade if none is connected.
    """
    if _operator_oauth_enabled(server):
        for agent in context.static_agents:
            token = await context.oauth_store.access_token_for(server=server, operator_subject=agent.operator_subject)
            if token:
                return await context.metadata_provider.metadata(server, token)
        return DegradedServerMetadata(
            server_id=server.id, title=server.id, tools=[], degraded_reason="no connected reflection operator"
        )
    try:
        token = _credential_token(server)
    except Exception as e:
        return DegradedServerMetadata(server_id=server.id, title=server.id, tools=[], degraded_reason=str(e))
    return await context.metadata_provider.metadata(server, token)


async def register_proxy_tools(mcp: FastMCP, context: ConsoleMcpContext) -> None:
    """Reflect every connected server and register its tools into the two buckets.

    Uniform (v1) surface: pass-through for unconditionally auto-approved tools, the approval envelope
    for everything else. Degraded servers are logged and skipped.
    """
    for server in _load_servers(context.settings):
        meta = await _reflect_server(context, server)
        if isinstance(meta, DegradedServerMetadata):
            logger.warning("mcp_server: skipping degraded server %s: %s", server.id, meta.degraded_reason)
            continue
        for tool in meta.tools:
            passthrough = is_unconditionally_auto_approved(server.id, tool.name)
            mcp.add_tool(_build_proxy_tool(context, server.id, tool, passthrough=passthrough))
        logger.info("mcp_server: registered %d tools from %s", len(meta.tools), server.id)


def _static_bearer_verifier(static_agents: list[ResolvedStaticAgent]) -> StaticTokenVerifier | None:
    """The configured static machine static_agents' fixed bearers, as FastMCP's stock ``StaticTokenVerifier``.

    Each agent's token maps to its own ``client_id`` (the agent id), so a machine call gets the stable
    identity operator resolution keys on; ``None`` when no static static_agents are configured.
    """
    if not static_agents:
        return None
    return StaticTokenVerifier(
        tokens={
            agent.token.get_secret_value(): {"client_id": static_agent_client_id(agent.agent), "scopes": []}
            for agent in static_agents
        }
    )


def build_auth(
    settings: Settings,
    static_agents: list[ResolvedStaticAgent],
    client_storage: Any,
    on_client_authorized: OnClientAuthorized | None = None,
) -> AuthProvider:
    """Compose the MCP server's auth — the credentials `/mcp` accepts.

    An Authentik-backed OIDCProxy (DCR + PKCE for claude.ai / the ``claude`` CLI) when
    ``settings.mcp_oauth`` is configured, composed with the static agent bearers via MultiAuth
    (`build_authentik_auth`'s ``extra_verifiers``); the static bearers alone when no OAuth is
    configured. Raises when neither is set — a `/mcp` server no one can authenticate to is a
    misconfiguration, not a mode to run in. ``on_client_authorized`` is invoked when an OAuth client
    completes the authorization-code exchange (haku-console uses it to record the agent→operator link).
    """
    static = _static_bearer_verifier(static_agents)
    if settings.mcp_oauth is not None:
        return build_authentik_auth(
            settings.mcp_oauth.as_authentik_auth_config(public_base_url=settings.public_base_url),
            client_storage=client_storage,
            extra_verifiers=[static] if static is not None else None,
            on_client_authorized=on_client_authorized,
        )
    if static is None:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    return static


def build_console_mcp(context: ConsoleMcpContext, auth: AuthProvider | None = None) -> FastMCP:
    """Build the console MCP server with the read tools + auth.

    ``auth`` is the composed auth provider (see ``build_auth``); when omitted it defaults to the
    static agent bearer. Call ``register_proxy_tools`` (async) afterwards to add the
    connected-server proxy tools.
    """
    mcp: FastMCP = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)
    if auth is None:
        auth = _static_bearer_verifier(context.static_agents)
    mcp.auth = auth

    def current_agent_scope() -> AgentToolCallScope:
        client_id = _agent_id()
        caller = resolve_mcp_agent(client_id, context.static_agents, context.oauth_store)
        if caller is None:
            raise ToolError(f"agent {client_id} has no linked operator subject")
        return AgentToolCallScope(operator_subject=caller.operator_subject, caller_principal=caller.principal)

    @mcp.tool
    async def get_tool_call(tool_call_id: str) -> ToolCallView:
        """Read one tool call (resolve a promise): status, result/error, and its approval link."""
        try:
            record = context.ledger.get(tool_call_id, scope=current_agent_scope())
        except HTTPException as e:
            raise ToolError(str(e.detail))
        return ToolCallView(call=record, url=_tool_call_url(context.settings, tool_call_id))

    @mcp.tool
    async def list_tool_calls(
        status: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list[ToolCallView]:
        """List recent tool calls (newest first by default), optionally filtered by status/since."""
        resp: ToolCallListResponse = context.ledger.list(
            scope=current_agent_scope(), statuses=status, since=since, limit=limit, newest_first=newest_first
        )
        return [ToolCallView(call=r, url=_tool_call_url(context.settings, r.tool_call_id)) for r in resp.tool_calls]

    return mcp
