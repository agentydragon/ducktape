"""haku-console's own MCP server: the connected-server tools, re-exposed to a Claude agent.

An interactive Agent MCP client (the claude.ai custom connector or the ``claude`` CLI) connects here
and calls the console's connected-server tools through the approval lifecycle. The trusted console
frontend uses this same endpoint with its Operator browser session; those calls execute directly and
do not create tool-call rows, approval events, or promises.

Each request exposes only servers connected by that principal's canonical Operator. Within that
Operator-specific surface, the global auto-approval policy divides Agent-visible tools into two
buckets:

Every proxied tool is named ``<server>__<tool>`` (one uniform format — operator decision
2026-07-13; bare upstream names hid which server a tool belonged to). An upstream tool's
human-readable ``annotations.title`` is likewise re-prefixed with the server id (operator decision
2026-07-20; same rationale, same fix) into the proxy's own display ``title``, which takes
precedence over ``annotations.title`` for clients:

- **Pass-through** — tools the policy unconditionally auto-approves (gmail reads): the upstream
  schema and description unchanged, so they behave like the real tool and return the real result.
- **Request** — everything else: an envelope schema (``input`` + ``rationale`` + optional
  ``title``/``wait_for_approval_ms``) and a promise-semantics preamble in the description;
  returns the real result *or* a promise.

For Agents both buckets run through ``submit_and_wait``. Operators bypass that lifecycle only after
the transport has established a DB-revalidated, same-origin, CSRF-gated browser principal.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from haku.console.auto_approval import is_unconditionally_auto_approved
from haku.console.config import Settings
from haku.console.mcp_approval import DegradedServerMetadata, McpServerClient, ServerMetadata, metadata_for_operator
from haku.console.mcp_auth.fastmcp_adapter import HakuMcpActorResolver
from haku.console.mcp_config import (
    InProcessBackend,
    InProcessCredential,
    McpServerEntry,
    McpServerNotFoundError,
    NoCredential,
    OperatorConnectionCredential,
    RemoteMcpBackend,
    RemoteServerOAuthAuth,
    StaticBearerAuth,
    _load_servers,
    server_tool_prefix,
)
from haku.console.mcp_operator_oauth import McpOperatorAuthStatus, PostgresMcpOperatorOAuthStore
from haku.console.node_daemons import DaemonStatusResponse, NodeDaemonService
from haku.console.provider_connection import PostgresProviderConnectionStore, ProviderConnectionStatus
from haku.console.tool_call_actor import OperatorActor, ToolCallActor
from haku.console.tool_call_service import (
    BackendAccountNotConnectedError,
    ToolCallApplicationService,
    ToolCallNotFoundError,
    ToolCallStateConflictError,
)
from haku.console.tool_calls import (
    MCP_TOOL_CALL_META_KEY,
    MCP_TOOL_META_KEY,
    McpProxyToolMetadata,
    McpToolCallMetadata,
    SubmitToolCallRequest,
    ToolCallRecord,
    ToolCallStatus,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "haku-console"
# Synchronous hold budget (ms) before a call returns a promise; overridable per envelope call.
DEFAULT_WAIT_MS = 5000
MAX_WAIT_MS = 60_000
TOOL_NAME_SEPARATOR = "__"

INSTRUCTIONS = (
    "haku-console tool proxy. Every proxied tool is named `<server>__<tool>`. Tools whose schema "
    "wraps the real arguments in an `input` + `rationale` envelope submit Agent calls to the "
    "operator's approval queue: they return the result if approved within a few seconds, otherwise "
    "a promise (a pending `tool_call_id` and approval link). Poll `get_tool_call(tool_call_id)` to "
    "resolve a promise. Tools with the upstream schema auto-approve Agent calls. Calls authenticated "
    "by the console Operator's browser session execute directly and create no approval record. Call "
    "`list_mcp_servers` to passively inspect persisted connection state without refreshing credentials."
)

_REQUEST_PREAMBLE = (
    "For Agent callers, submits `{tool}` on `{server}` through haku-console's operator-approval "
    "queue. Put the real tool arguments under `input`. Returns the result if approved within "
    "`wait_for_approval_ms`, otherwise a promise (a pending `tool_call_id` plus approval `url`) — "
    "poll `get_tool_call(tool_call_id)` to resolve it. An authenticated console Operator call "
    "executes directly and creates no approval record."
)

# Console-native read tools: they read only the console's own persisted catalog/ledger (closed
# world — never a downstream MCP/provider lookup) and mutate nothing, so advertise both axes.
# Clients like claude.ai key off readOnlyHint to group these as read-only and skip approvals.
_READ_ONLY_META = mcp_types.ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@dataclass(frozen=True)
class ConsoleMcpContext:
    """The application service and MCP-specific adapters needed by the FastMCP transport."""

    settings: Settings
    tool_calls: ToolCallApplicationService
    oauth_store: PostgresMcpOperatorOAuthStore
    provider_store: PostgresProviderConnectionStore
    metadata_provider: McpServerClient
    node_daemons: NodeDaemonService | None = None


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


class StaticBearerAuthStatus(BaseModel):
    """Safe reflection of static-bearer auth: the secret's environment reference is omitted."""

    kind: Literal["static_bearer"] = "static_bearer"


type RemoteMcpAuthStatus = Annotated[
    RemoteServerOAuthAuth | StaticBearerAuthStatus | NoCredential, Field(discriminator="kind")
]


class RemoteMcpBackendStatus(BaseModel):
    kind: Literal["remote_mcp"] = "remote_mcp"
    url: str
    auth: RemoteMcpAuthStatus


class InProcessBackendStatus(BaseModel):
    kind: Literal["in_process"] = "in_process"
    credential: InProcessCredential


type McpBackendStatus = Annotated[RemoteMcpBackendStatus | InProcessBackendStatus, Field(discriminator="kind")]


class McpServerConnectionStatus(BaseModel):
    """Persisted connection state for one configured MCP server.

    This deliberately says nothing about current reachability or upstream tools: answering either
    question would require network I/O and, for OAuth-backed servers, could rotate credentials.
    """

    server_id: str
    backend: McpBackendStatus
    connection: McpOperatorAuthStatus | ProviderConnectionStatus | None = Field(
        description=(
            "The persisted, non-secret operator connection status. This uses the same safe status "
            "shape as the console's OAuth/provider APIs, including connection and token-expiry times. "
            "It is null when this authentication kind has no separately linked operator connection."
        )
    )


class McpServerConnectionStatusResponse(BaseModel):
    servers: list[McpServerConnectionStatus]


class McpServerProbeResponse(BaseModel):
    """Persisted linkage plus the current reflection result for one configured server."""

    connection: McpServerConnectionStatus
    server: ServerMetadata


def _backend_status(backend: RemoteMcpBackend | InProcessBackend) -> McpBackendStatus:
    match backend:
        case RemoteMcpBackend(auth=StaticBearerAuth()):
            return RemoteMcpBackendStatus(url=backend.url, auth=StaticBearerAuthStatus())
        case RemoteMcpBackend():
            return RemoteMcpBackendStatus(url=backend.url, auth=backend.auth)
        case InProcessBackend():
            return InProcessBackendStatus(credential=backend.credential)


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


def _tool_call_url(settings: Settings, tool_call_id: str) -> str | None:
    if settings.ui_base_url is None:
        return None
    return f"{settings.ui_base_url.rstrip('/')}/tool-calls/{tool_call_id}"


def _passive_server_connection_statuses(
    context: ConsoleMcpContext, actor: ToolCallActor
) -> McpServerConnectionStatusResponse:
    """Read connection rows without refreshing tokens or contacting an MCP/provider endpoint."""
    servers = _load_servers(context.settings)
    oauth_statuses = {
        status.server_id: status
        for status in context.oauth_store.list_statuses(
            servers=servers, operator_id=actor.operator_id, username="operator"
        ).associations
    }
    provider_statuses = {
        status.connection: status
        for status in context.provider_store.list_statuses(operator_id=actor.operator_id).connections
    }
    result: list[McpServerConnectionStatus] = []
    for server in servers:
        match server.backend:
            case RemoteMcpBackend(auth=RemoteServerOAuthAuth()):
                oauth_status = oauth_statuses.get(server.id)
                result.append(
                    McpServerConnectionStatus(
                        server_id=server.id, backend=_backend_status(server.backend), connection=oauth_status
                    )
                )
            case InProcessBackend(credential=OperatorConnectionCredential(connection=connection)):
                provider_status = provider_statuses.get(connection)
                result.append(
                    McpServerConnectionStatus(
                        server_id=server.id, backend=_backend_status(server.backend), connection=provider_status
                    )
                )
            case RemoteMcpBackend():
                result.append(
                    McpServerConnectionStatus(
                        server_id=server.id, backend=_backend_status(server.backend), connection=None
                    )
                )
            case InProcessBackend():
                result.append(
                    McpServerConnectionStatus(
                        server_id=server.id, backend=_backend_status(server.backend), connection=None
                    )
                )
    return McpServerConnectionStatusResponse(servers=result)


def _without_tool_schemas(metadata: ServerMetadata) -> ServerMetadata:
    """Keep the reflected catalog useful while omitting its potentially large schemas."""
    if isinstance(metadata, DegradedServerMetadata):
        return metadata
    return metadata.model_copy(
        update={
            "tools": [tool.model_copy(update={"input_schema": None, "output_schema": None}) for tool in metadata.tools]
        }
    )


def _output_schema(upstream_output_schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """A truthful output schema: any proxied call — passthrough or approval-request alike — can
    return either the upstream tool's own result or, when execution outlasts
    ``wait_for_approval_ms``, the pending-approval promise (`ToolCallPromise`, see
    `_record_to_result`). Only meaningful when the upstream declares its own output schema;
    otherwise there is nothing to narrow beyond "anything", so declaring none is more honest than
    inventing a promise-only schema that would wrongly reject the upstream shape.

    ``$ref``s are JSON pointers resolved against the document root, so each branch's own
    ``$defs`` has to be hoisted to the combined schema's root rather than left nested under
    ``oneOf`` (where the pointer would dangle). ``ToolCallPromise`` is the only source of
    defs here; an upstream schema defining a same-named def is an unreviewed-but-unlikely
    collision, not something this display-only schema needs to guard against.
    """
    if upstream_output_schema is None:
        return None
    upstream = copy.deepcopy(upstream_output_schema)
    promise = ToolCallPromise.model_json_schema()
    defs = {**upstream.pop("$defs", {}), **promise.pop("$defs", {})}
    combined: dict[str, Any] = {"oneOf": [upstream, promise]}
    if defs:
        combined["$defs"] = defs
    return combined


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

    Terminal ok → the upstream result; error/denied → an MCP tool error; still pending/running →
    a promise (pending id + approval url). Every outcome carries the canonical tool-call id in MCP
    result metadata so non-interactive clients can resolve the audit record without a second
    admission protocol.
    """
    result_meta = {
        MCP_TOOL_CALL_META_KEY: McpToolCallMetadata(tool_call_id=record.tool_call_id).model_dump(mode="json")
    }
    if record.status == ToolCallStatus.OK:
        upstream = mcp_types.CallToolResult.model_validate(record.result or {"content": []})
        content: list[mcp_types.ContentBlock] = list(upstream.content) or [
            mcp_types.TextContent(type="text", text="(tool returned no content)")
        ]
        return ToolResult(content=content, structured_content=upstream.structuredContent, meta=result_meta)
    if record.status == ToolCallStatus.ERROR:
        return ToolResult(
            content=[mcp_types.TextContent(type="text", text=record.error or "tool call failed")],
            meta=result_meta,
            is_error=True,
        )
    if record.status == ToolCallStatus.DENIED:
        return ToolResult(
            content=[mcp_types.TextContent(type="text", text=f"denied: {record.denial_reason or 'no reason given'}")],
            meta=result_meta,
            is_error=True,
        )
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
        meta=result_meta,
    )


def _direct_to_result(result: dict[str, Any]) -> ToolResult:
    """Preserve the upstream MCP result without adding Haku tool-call metadata."""

    upstream = mcp_types.CallToolResult.model_validate(result)
    return ToolResult(
        content=list(upstream.content),
        structured_content=upstream.structuredContent,
        meta=upstream.meta,
        is_error=upstream.isError,
    )


class ProxyTool(Tool):
    """A connected-server tool re-exposed through the shared application service.

    ``passthrough`` tools advertise the upstream schema and take the raw args; envelope tools
    advertise the envelope and read the call args from ``input``. Agent calls route through
    ``submit_and_wait``; authenticated Operator calls execute directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: ConsoleMcpContext
    server_id: str
    upstream_tool_name: str
    passthrough: bool
    actor: ToolCallActor

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.passthrough:
            call_args, rationale, title, wait_ms = arguments, "", None, DEFAULT_WAIT_MS
        else:
            env = ApprovalRequestEnvelope.model_validate(arguments)
            call_args, rationale, title = env.input, env.rationale, env.title
            wait_ms = DEFAULT_WAIT_MS if env.wait_for_approval_ms is None else env.wait_for_approval_ms
        ctx = self.context
        req = SubmitToolCallRequest(
            server_id=self.server_id,
            tool_name=self.upstream_tool_name,
            arguments=call_args,
            rationale=rationale,
            title=title,
            wait_for_ms=max(0, min(int(wait_ms), MAX_WAIT_MS)),
        )
        actor = self.actor
        try:
            if isinstance(actor, OperatorActor):
                return _direct_to_result(await ctx.tool_calls.execute_direct(req=req, actor=actor))
            record = await ctx.tool_calls.submit_and_wait(req=req, actor=actor)
        except (
            BackendAccountNotConnectedError,
            McpServerNotFoundError,
            ToolCallNotFoundError,
            ToolCallStateConflictError,
        ) as error:
            raise ToolError(str(error)) from error
        return _record_to_result(record, ctx.settings)


def _build_proxy_tool(
    context: ConsoleMcpContext, server_id: str, tool: Any, *, passthrough: bool, actor: ToolCallActor
) -> ProxyTool:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) and tool.input_schema else {"type": "object"}
    # One uniform name format for both buckets — approval semantics live in the schema and
    # description, never in the name (operator decision 2026-07-13).
    name = f"{server_tool_prefix(server_id)}{TOOL_NAME_SEPARATOR}{tool.name}"
    # `title` is the spec-preferred display name; `annotations.title` is the legacy fallback FastMCP
    # still honors when `title` is unset (mirrors `Tool.to_mcp_tool`'s own precedence).
    upstream_title = tool.title or (tool.annotations.title if tool.annotations else None)
    # The upstream human-readable title hides which server a tool belongs to just like the bare
    # name did, so prefix it the same way (operator decision 2026-07-20). This is a display-only
    # `title`, which FastMCP's MCP conversion prefers over `annotations.title` for clients.
    title = f"{server_id}: {upstream_title}" if upstream_title else None
    if passthrough:
        parameters = schema
        description = tool.description or ""
    else:
        parameters = _envelope_schema(schema)
        preamble = _REQUEST_PREAMBLE.format(tool=tool.name, server=server_id)
        description = f"{preamble}\n\n{tool.description}" if tool.description else preamble
    return ProxyTool(
        name=name,
        title=title,
        description=description,
        parameters=parameters,
        output_schema=_output_schema(tool.output_schema),
        # Icons are opaque display assets (URLs/data URIs) — no server identity to prefix, so they
        # propagate unchanged, unlike the textual name/title above.
        icons=tool.icons,
        # Propagate the upstream server's self-declared hints unchanged. They are advisory (the
        # spec forbids trusting them for security), and the console's own approval policy is
        # enforced server-side regardless — this only sets client-facing UX grouping.
        annotations=tool.annotations,
        context=context,
        server_id=server_id,
        upstream_tool_name=tool.name,
        passthrough=passthrough,
        actor=actor,
        meta={
            MCP_TOOL_META_KEY: McpProxyToolMetadata(
                server_id=server_id,
                upstream_tool_name=tool.name,
                approval_mode="passthrough" if passthrough else "approval_required",
            ).model_dump(mode="json")
        },
    )


class OperatorServerCatalog:
    """Resolve one configured connected server for the current Operator."""

    def __init__(self, context: ConsoleMcpContext) -> None:
        self._context = context

    def server_for_tool_name(self, name: str) -> McpServerEntry | None:
        candidates = [
            server
            for server in _load_servers(self._context.settings)
            if name.startswith(f"{server_tool_prefix(server.id)}{TOOL_NAME_SEPARATOR}")
        ]
        if not candidates:
            return None
        # A longer namespace is the more specific match (for example, ``google_calendar``
        # rather than ``google``). Startup validation guarantees exact namespace uniqueness.
        return max(candidates, key=lambda candidate: len(server_tool_prefix(candidate.id)))

    async def metadata(self, server: McpServerEntry, actor: ToolCallActor) -> ServerMetadata:
        return await metadata_for_operator(
            operator_id=actor.operator_id,
            server=server,
            metadata_provider=self._context.metadata_provider,
            oauth_store=self._context.oauth_store,
            provider_store=self._context.provider_store,
        )


def _unavailable_server_message(server_id: str, reason: str) -> str:
    return (
        f"MCP server {server_id!r} is unavailable: {reason.rstrip().rstrip('.') or 'unknown availability error'}. "
        f"Use get_mcp_server_status(server_id={server_id!r}) to check it; "
        "reconnect the server in the console if its OAuth connection has expired or been revoked."
    )


class OperatorToolProvider(Provider):
    """Reflect the connected-server catalog for the current principal's Operator.

    FastMCP providers are consulted for both ``tools/list`` and ``tools/call``.
    Keeping reflection here makes discovery request-local and also fails closed
    if a client calls a tool after its Operator disconnects that server.
    """

    def __init__(
        self,
        context: ConsoleMcpContext,
        actor_resolver: HakuMcpActorResolver,
        catalog: OperatorServerCatalog | None = None,
    ) -> None:
        super().__init__()
        self._context = context
        self._actor_resolver = actor_resolver
        self._catalog = catalog or OperatorServerCatalog(context)

    async def _server_tools(self, server: McpServerEntry, actor: ToolCallActor) -> list[Tool]:
        try:
            meta = await self._catalog.metadata(server, actor)
        except Exception:
            # Discovery is an aggregate availability surface: an unexpected failure in one server
            # must remain visible in logs without erasing every unrelated server's tools.
            logger.exception("mcp_server: failed to reflect server %s for Operator %s", server.id, actor.operator_id)
            return []
        if isinstance(meta, DegradedServerMetadata):
            logger.info(
                "mcp_server: hiding unavailable server %s from Operator %s: %s",
                server.id,
                actor.operator_id,
                meta.degraded_reason,
            )
            return []
        return [
            _build_proxy_tool(
                self._context,
                server.id,
                tool,
                passthrough=is_unconditionally_auto_approved(server.id, tool.name),
                actor=actor,
            )
            for tool in meta.tools
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        actor = await self._actor_resolver.resolve()
        # gather preserves input order even when servers finish reflection out of order.
        reflected = await asyncio.gather(
            *(self._server_tools(server, actor) for server in _load_servers(self._context.settings))
        )
        return [tool for server_tools in reflected for tool in server_tools]

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        actor = await self._actor_resolver.resolve()
        server = self._catalog.server_for_tool_name(name)
        if server is None:
            return None
        meta = await self._catalog.metadata(server, actor)
        if isinstance(meta, DegradedServerMetadata):
            return None
        for upstream_tool in meta.tools:
            tool = _build_proxy_tool(
                self._context,
                server.id,
                upstream_tool,
                passthrough=is_unconditionally_auto_approved(server.id, upstream_tool.name),
                actor=actor,
            )
            if tool.name == name and (version is None or version.matches(tool.version)):
                return tool
        return None

    async def get_tasks(self) -> Sequence[Tool]:
        # Proxy tools forbid background tasks, and startup has no request actor.
        return []


class OperatorToolAvailabilityMiddleware(Middleware):
    """Make a known server's reflection failure visible after FastMCP lookup misses."""

    def __init__(self, catalog: OperatorServerCatalog, actor_resolver: HakuMcpActorResolver) -> None:
        self._catalog = catalog
        self._actor_resolver = actor_resolver

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except NotFoundError as error:
            server = self._catalog.server_for_tool_name(context.message.name)
            if server is None:
                raise
            actor = await self._actor_resolver.resolve()
            meta = await self._catalog.metadata(server, actor)
            if isinstance(meta, DegradedServerMetadata):
                raise ToolError(_unavailable_server_message(server.id, meta.degraded_reason)) from error
            raise


def build_console_mcp(
    context: ConsoleMcpContext, *, auth: AuthProvider, actor_resolver: HakuMcpActorResolver
) -> FastMCP:
    """Build the console MCP server with request-local proxy tools + auth.

    Authentication is composed by :mod:`haku.console.mcp_agent_auth`. The
    provider reflects connected-server tools for the authenticated principal's
    canonical Operator on each discovery and dispatch request.
    """
    mcp: FastMCP = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)
    mcp.auth = auth
    catalog = OperatorServerCatalog(context)
    mcp.add_provider(OperatorToolProvider(context, actor_resolver, catalog))
    mcp.add_middleware(OperatorToolAvailabilityMiddleware(catalog, actor_resolver))

    current_actor_dependency = Depends(actor_resolver.resolve)

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_mcp_servers(actor: ToolCallActor = current_actor_dependency) -> McpServerConnectionStatusResponse:
        """List configured MCP servers and their persisted connection state.

        This is a passive status read: it never refreshes a token, contacts an authorization server,
        or calls a downstream MCP server. OAuth/provider connection objects mirror the console's
        persisted non-secret status structures, including connection and token-expiry times. The
        nested backend object mirrors the safe server configuration shape so callers can distinguish
        remote MCP transports from in-process implementations; static bearer secret references are
        omitted. A real
        discovery or execution attempt may refresh an expired token or prove that reconnect is needed.
        Cataloged provider accounts whose OAuth client is absent remain visible as ``unprovisioned``.
        """
        return _passive_server_connection_statuses(context, actor)

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_node_daemons(actor: ToolCallActor = current_actor_dependency) -> DaemonStatusResponse:
        """List configured node daemons and their current persisted heartbeat/lease status.

        Use this to check whether approved node work can currently be dispatched; do not use it to
        submit or alter work. Each result includes the daemon's derived presence state, last
        heartbeat, advertised backends/version, and active execution when one exists. This is a
        read-only console-state view and does not contact a daemon or renew its lease.
        """
        _ = actor
        return context.node_daemons.statuses() if context.node_daemons is not None else DaemonStatusResponse(daemons=[])

    @mcp.tool(annotations=_READ_ONLY_META)
    async def get_mcp_server_status(
        server_id: str, include_tool_schemas: bool = False, actor: ToolCallActor = current_actor_dependency
    ) -> McpServerProbeResponse:
        """Actively reflect one configured MCP server's current tool availability.

        Unlike ``list_mcp_servers``, this may refresh the operator's linked OAuth token and contact
        the remote MCP server. It returns persisted linkage plus a structured degraded result when
        that cannot succeed, so agents can distinguish credential failures from downstream tool
        discovery failures. Tool names, titles, descriptions, icons, and annotations are returned by
        default; set ``include_tool_schemas`` to include the potentially large input/output schemas.
        """
        server = next((candidate for candidate in _load_servers(context.settings) if candidate.id == server_id), None)
        if server is None:
            raise ToolError(f"unknown configured MCP server {server_id!r}")
        connection = next(
            status
            for status in _passive_server_connection_statuses(context, actor).servers
            if status.server_id == server_id
        )
        metadata = await metadata_for_operator(
            operator_id=actor.operator_id,
            server=server,
            metadata_provider=context.metadata_provider,
            oauth_store=context.oauth_store,
            provider_store=context.provider_store,
        )
        return McpServerProbeResponse(
            connection=connection, server=metadata if include_tool_schemas else _without_tool_schemas(metadata)
        )

    @mcp.tool(annotations=_READ_ONLY_META)
    async def get_tool_call(tool_call_id: str, actor: ToolCallActor = current_actor_dependency) -> ToolCallView:
        """Read one tool call (resolve a promise): status, result/error, and its approval link."""
        try:
            record = context.tool_calls.get(tool_call_id, actor=actor)
        except (ToolCallNotFoundError, ToolCallStateConflictError) as error:
            raise ToolError(str(error)) from error
        return ToolCallView(call=record, url=_tool_call_url(context.settings, tool_call_id))

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_tool_calls(
        status: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = True,
        actor: ToolCallActor = current_actor_dependency,
    ) -> list[ToolCallView]:
        """List recent tool calls (newest first by default), optionally filtered by status/since."""
        records = context.tool_calls.list_tool_calls(
            actor=actor, statuses=status, since=since, limit=limit, newest_first=newest_first
        )
        return [ToolCallView(call=r, url=_tool_call_url(context.settings, r.tool_call_id)) for r in records]

    return mcp
