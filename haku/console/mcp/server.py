"""haku-console's own MCP server: the connected-server tools, re-exposed to a Claude agent.

An interactive Agent MCP client (the claude.ai custom connector or the ``claude`` CLI) connects here
and calls the console's connected-server tools through the approval lifecycle. The trusted console
frontend uses this same endpoint with its Operator browser session; those calls execute directly and
do not create tool-call rows, approval events, or non-terminal stubs.

Each request exposes only servers connected by that principal's canonical Operator. Within that
Operator-specific surface, the authenticated Agent's auto-approval policy divides tools into two
buckets:

Every proxied tool is named ``<server>__<tool>``, so a bare upstream name never hides which
server a tool belongs to. An upstream tool's human-readable ``annotations.title`` is likewise
re-prefixed with the server id into the proxy's own display ``title``, which takes precedence over
``annotations.title`` for clients:

- **Pass-through** — tools the policy unconditionally auto-approves (gmail reads): the upstream
  schema and description unchanged, so they behave like the real tool and return the real result.
- **Request** — everything else: an envelope schema (``input`` + ``rationale`` + optional
  ``title``/``wait_for_result_ms``) and a stub-semantics preamble in the description;
  returns the real result *or* a non-terminal stub.

For Agents both buckets run through ``submit_and_wait``. Operators bypass that lifecycle only after
the transport has established a DB-revalidated, exact-Origin-gated browser principal.
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    StrictInt,
    ValidationError,
    create_model,
    model_serializer,
)

from haku.console.auto_approval.registry import AutoApprovalPolicyRegistry, ToolAutoApprovalMode
from haku.console.config import Settings, tool_call_console_url
from haku.console.hostexecd.service import DaemonStatusResponse, Service
from haku.console.identity.fastmcp_adapter import HakuMcpActorResolver
from haku.console.mcp.approval import (
    DegradedReflection,
    DegradedServerState,
    McpServerDispatcher,
    ServerMetadata,
    ServerReflection,
    ToolMetadata,
    metadata_for_operator,
    server_metadata_response,
)
from haku.console.mcp.catalog_reconciler import OperatorCatalogReconciler
from haku.console.mcp.guidance import SERVER_INSTRUCTIONS, approval_request_preamble
from haku.console.mcp.operator_oauth import McpOperatorAuthStatus, PostgresMcpOperatorOAuthStore
from haku.console.mcp.tool_call_service import (
    AgentActorRequiredError,
    BackendAccountNotConnectedError,
    ToolCallApplicationService,
    ToolCallNotFoundError,
    ToolCallStateConflictError,
)
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
    load_console_config,
    server_tool_prefix,
)
from haku.console.oauth.provider_connection import PostgresProviderConnectionStore, ProviderConnectionStatus
from haku.console.tool_call_actor import OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    MCP_TOOL_CALL_META_KEY,
    MCP_TOOL_META_KEY,
    ApprovalMode,
    McpProxyToolMetadata,
    McpToolCallMetadata,
    SubmitToolCallRequest,
    ToolCallPayloadField,
    ToolCallRecord,
    ToolCallStatus,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "haku-console"
DEFAULT_WAIT_MS = 5000
TOOL_NAME_SEPARATOR = "__"
_DEFAULT_GET_TOOL_CALL_FIELDS = [ToolCallPayloadField.RESULT]
_DEFAULT_LIST_TOOL_CALL_FIELDS: list[ToolCallPayloadField] = []

# Console-native read tools: they read only the console's own persisted catalog/ledger (closed
# world — never a downstream MCP/provider lookup) and mutate nothing, so advertise both axes.
# Clients like claude.ai key off readOnlyHint to group these as read-only and skip approvals.
_READ_ONLY_META = mcp_types.ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# Console-native mutation of the console's own ledger: closed world (never a downstream MCP or
# provider call), and non-destructive — it retracts a request the agent can simply resubmit, and
# destroys no data. Spelled out because an unannotated tool reads to clients as an open-world
# destructive write. No idempotentHint: a second withdrawal is a conflict, not a no-op, and
# claiming idempotency would invite client retry loops.
_LEDGER_MUTATION_META = mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

# The generic dispatch fallback reaches any tool on any configured server, so it is neither
# read-only nor closed-world. destructiveHint is deliberately left unset: MCP's default for a
# non-read-only tool is "destructive", which is the only safe assumption when the target tool is a
# parameter rather than a property of this tool.
_GENERIC_DISPATCH_META = mcp_types.ToolAnnotations(readOnlyHint=False, openWorldHint=True)


@dataclass(frozen=True)
class ConsoleMcpContext:
    """The application service and MCP-specific adapters needed by the FastMCP transport."""

    settings: Settings
    tool_calls: ToolCallApplicationService
    oauth_store: PostgresMcpOperatorOAuthStore
    provider_store: PostgresProviderConnectionStore
    dispatcher: McpServerDispatcher
    catalogs: OperatorCatalogReconciler
    node_daemons: Service | None = None


class ToolCallStub(BaseModel):
    """Returned by an approval-envelope tool when the synchronous wait ends before terminal state."""

    status: ToolCallStatus
    tool_call_id: str
    url: str = Field(description="Operator-facing console link that opens this call's approval or status view.")
    message: str


class ToolCallView(BaseModel):
    """A tool-call record plus its operator-facing deep link, for the read tools."""

    call: ToolCallRecord
    url: str


class McpToolCallResponse(ToolCallRecord):
    """A compact MCP edge rendering of the shared domain record with its deep link."""

    url: str
    _selected_payload_fields: frozenset[ToolCallPayloadField] = PrivateAttr()

    @model_serializer(mode="wrap")
    def _serialize(self, serializer: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = serializer(self)
        for field in ToolCallPayloadField:
            if field not in self._selected_payload_fields:
                data.pop(field.value, None)
        return data


def _mcp_tool_call_response(record: ToolCallRecord, settings: Settings) -> McpToolCallResponse:
    response = McpToolCallResponse.model_validate(
        record.model_dump() | {"url": _tool_call_url(settings, record.tool_call_id)}
    )
    response._selected_payload_fields = frozenset(
        field for field in ToolCallPayloadField if field.value in record.model_fields_set
    )
    return response


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


class _ApprovalRequestEnvelopeBase(BaseModel):
    """The fields shared by dynamically bounded approval-request envelopes."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(description="The real arguments for the upstream tool.")
    rationale: str = Field(description="Why you are requesting this call. Shown to the operator.")
    title: str | None = Field(default=None, description="Short human-facing title for the operator's approval queue.")


def _approval_request_envelope_model(
    *, max_wait_ms: int, default_wait_ms: int = DEFAULT_WAIT_MS, min_wait_ms: int = 0
) -> type[Any]:
    """Build the envelope model with the bounds used by this console instance.

    FastMCP snapshots a tool's JSON schema when it reflects the proxy.  Constructing this model at
    the point where the schema is requested (and again when a call is parsed) keeps that schema and
    runtime validation tied to the same default and bounds, rather than leaving a stale class-level
    ``None`` default that silently turns into the default or a runtime clamp that hides bad input.
    """
    if not min_wait_ms <= default_wait_ms <= max_wait_ms:
        raise ValueError("approval wait bounds must satisfy min <= default <= max")
    return create_model(
        "ApprovalRequestEnvelope",
        __base__=_ApprovalRequestEnvelopeBase,
        wait_for_result_ms=(
            Annotated[
                StrictInt,
                Field(
                    ge=min_wait_ms,
                    le=max_wait_ms,
                    description=(
                        "How long to wait synchronously for approval and execution before returning a non-terminal "
                        "stub. Returning a stub does not cancel or expire the queued call."
                    ),
                ),
            ],
            default_wait_ms,
        ),
    )


def _tool_call_url(settings: Settings, tool_call_id: str) -> str:
    """The console link handed to an agent whose call returned a non-terminal stub.

    Shares one definition with the push notification's deep link so an operator following either
    lands in the same place — the approvals drawer with this call expanded.
    """
    return tool_call_console_url(settings.public_base_url, tool_call_id)


async def _passive_server_connection_statuses(
    context: ConsoleMcpContext, actor: RuntimeActor
) -> McpServerConnectionStatusResponse:
    """Read connection rows without refreshing tokens or contacting an MCP/provider endpoint."""
    servers = _load_servers(context.settings)
    oauth_statuses = {
        status.server_id: status
        for status in (
            await context.oauth_store.list_statuses(servers=servers, operator_id=actor.operator_id, username="operator")
        ).associations
    }
    provider_statuses = {
        status.connection: status
        for status in (await context.provider_store.list_statuses(operator_id=actor.operator_id)).connections
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


def _is_passthrough(policies: AutoApprovalPolicyRegistry, actor: RuntimeActor, server_id: str, tool_name: str) -> bool:
    return policies.tool_mode(actor, server_id, tool_name) is ToolAutoApprovalMode.ALWAYS_AUTO_APPROVED


def _exposed_metadata(
    metadata: ServerMetadata,
    *,
    policies: AutoApprovalPolicyRegistry,
    actor: RuntimeActor,
    include_schemas: bool,
    max_wait_ms: int,
) -> ServerMetadata:
    """Report each tool as *this proxy* exposes it to this caller, not as the upstream declares it.

    ``input_schema`` is therefore the schema a caller actually sends — enveloped where the policy
    requires approval — and ``approval_mode`` names which of the two shapes that is. The upstream
    schema is not reported separately: for an enveloped tool it is already nested under ``input``,
    so returning both would be the same schema twice.

    This is what makes `call_mcp_tool` usable. Its whole reason to exist is a caller whose tool list
    lacks the generated proxy, and such a caller has no other way to learn whether the tool it wants
    takes raw arguments or an envelope.
    """
    if isinstance(metadata.state, DegradedServerState):
        return metadata

    def exposed(tool: ToolMetadata) -> ToolMetadata:
        passthrough = _is_passthrough(policies, actor, metadata.server_id, tool.name)
        # Mirror `_build_proxy_tool`'s treatment of a missing/degenerate upstream schema, so the
        # reported envelope is exactly the one the generated tool would advertise.
        schema = (
            tool.input_schema
            if passthrough
            else _envelope_schema(tool.input_schema or {"type": "object"}, max_wait_ms=max_wait_ms)
        )
        return tool.model_copy(
            update={
                "approval_mode": ApprovalMode.PASSTHROUGH if passthrough else ApprovalMode.APPROVAL_REQUIRED,
                "input_schema": schema if include_schemas else None,
                "output_schema": tool.output_schema if include_schemas else None,
            }
        )

    tools = [exposed(tool) for tool in metadata.state.tools]
    return metadata.model_copy(update={"state": metadata.state.model_copy(update={"tools": tools})})


def _envelope_schema(original_schema: dict[str, Any], *, max_wait_ms: int) -> dict[str, Any]:
    """The approval-request envelope schema: `ApprovalRequestEnvelope`'s generated schema with the
    ``input`` property replaced by the upstream tool's own schema (nested unchanged, so its fields
    can't collide with the envelope's ``rationale``/``title``/``wait_for_result_ms``)."""
    schema: dict[str, Any] = _approval_request_envelope_model(max_wait_ms=max_wait_ms).model_json_schema()
    schema["properties"]["input"] = copy.deepcopy(original_schema)
    schema.pop("title", None)  # the model class title; the object schema itself needs none
    return schema


def _failed_result(text: str, meta: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[mcp_types.TextContent(type="text", text=text)], meta=meta, is_error=True)


def _record_to_result(record: ToolCallRecord, settings: Settings) -> ToolResult:
    """Map a (possibly non-terminal) tool-call record to an MCP tool result.

    Terminal ok → the upstream result; error/denied/withdrawn → an MCP tool error; still
    pending/running → a non-terminal stub (pending id + approval url). Every outcome carries the canonical
    tool-call id in MCP result metadata so non-interactive clients can resolve the audit record
    without a second admission protocol.

    Exhaustive `match` on purpose: the stub is one named arm rather than a fallback, so adding a
    status is a mypy error here instead of a terminal record silently reported as still pending.
    """
    result_meta = {
        MCP_TOOL_CALL_META_KEY: McpToolCallMetadata(tool_call_id=record.tool_call_id).model_dump(mode="json")
    }
    match record.status:
        case ToolCallStatus.OK:
            upstream = mcp_types.CallToolResult.model_validate(record.result or {"content": []})
            content: list[mcp_types.ContentBlock] = list(upstream.content) or [
                mcp_types.TextContent(type="text", text="(tool returned no content)")
            ]
            return ToolResult(content=content, structured_content=upstream.structuredContent, meta=result_meta)
        case ToolCallStatus.ERROR:
            return _failed_result(record.error or "tool call failed", result_meta)
        case ToolCallStatus.DENIED:
            return _failed_result(f"denied: {record.denial_reason or 'no reason given'}", result_meta)
        case ToolCallStatus.WITHDRAWN:
            return _failed_result(f"withdrawn: {record.withdrawal_reason or 'no reason given'}", result_meta)
        case ToolCallStatus.PENDING_APPROVAL:
            url = _tool_call_url(settings, record.tool_call_id)
            stub = ToolCallStub(
                status=record.status,
                tool_call_id=record.tool_call_id,
                url=url,
                message=(
                    "The operator did not approve or deny before the synchronous wait ended, so this is a "
                    f"non-terminal pending stub for {record.tool_call_id}, not a timeout or cancellation. "
                    "The request remains queued and the operator may approve or deny it later; if approved, "
                    f"the tool call will execute. Open {url} to decide. "
                    f"Poll get_tool_call('{record.tool_call_id}') for the result, or "
                    f"withdraw_tool_call('{record.tool_call_id}', reason) if you no longer want it."
                ),
            )
            return ToolResult(
                content=[mcp_types.TextContent(type="text", text=stub.message)],
                structured_content=stub.model_dump(mode="json"),
                meta=result_meta,
            )
        case ToolCallStatus.RUNNING:
            url = _tool_call_url(settings, record.tool_call_id)
            stub = ToolCallStub(
                status=record.status,
                tool_call_id=record.tool_call_id,
                url=url,
                message=(
                    f"The operator approved {record.tool_call_id}, but execution did not finish before the "
                    "synchronous wait ended, so this is a non-terminal running stub, not a timeout. "
                    f"Execution continues in the background. Poll get_tool_call('{record.tool_call_id}') "
                    "for the result."
                ),
            )
            return ToolResult(
                content=[mcp_types.TextContent(type="text", text=stub.message)],
                structured_content=stub.model_dump(mode="json"),
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


async def _dispatch(
    context: ConsoleMcpContext,
    *,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    passthrough: bool,
    actor: RuntimeActor,
) -> ToolResult:
    """Read one call payload in the shape its tool advertises, then run it through the lifecycle its
    principal is entitled to.

    Shared by the generated ``<server>__<tool>`` proxies and the generic ``call_mcp_tool`` fallback,
    which is why the payload parsing lives here rather than in either caller: the two must accept
    byte-identical arguments, and they must reach the queue through exactly one path. A second
    parse or a second dispatch is how an approval bypass gets built by accident, since the policy
    decision lives inside ``submit_and_wait``.
    """
    if passthrough:
        call_args, rationale, title, wait_ms = arguments, "", None, DEFAULT_WAIT_MS
    else:
        env = _approval_request_envelope_model(max_wait_ms=context.settings.max_wait_for_result_ms).model_validate(
            arguments
        )
        call_args, rationale, title = env.input, env.rationale, env.title
        wait_ms = env.wait_for_result_ms
    req = SubmitToolCallRequest(
        server_id=server_id,
        tool_name=tool_name,
        arguments=call_args,
        rationale=rationale,
        title=title,
        wait_for_ms=wait_ms,
    )
    try:
        if isinstance(actor, OperatorActor):
            return _direct_to_result(await context.tool_calls.execute_direct(req=req, actor=actor))
        record = await context.tool_calls.submit_and_wait(req=req, actor=actor)
    except (
        BackendAccountNotConnectedError,
        McpServerNotFoundError,
        ToolCallNotFoundError,
        ToolCallStateConflictError,
    ) as error:
        raise ToolError(str(error)) from error
    return _record_to_result(record, context.settings)


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
    actor: RuntimeActor

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return await _dispatch(
            self.context,
            server_id=self.server_id,
            tool_name=self.upstream_tool_name,
            arguments=arguments,
            passthrough=self.passthrough,
            actor=self.actor,
        )


def _build_proxy_tool(
    context: ConsoleMcpContext, server_id: str, tool: mcp_types.Tool, *, passthrough: bool, actor: RuntimeActor
) -> ProxyTool:
    # `inputSchema` is a required field on the real upstream type, but treat a degenerate empty
    # dict the same as "no schema" — an empty object schema is a worse minimal schema than the
    # canonical one.
    schema = tool.inputSchema or {"type": "object"}
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
        parameters = _envelope_schema(schema, max_wait_ms=context.settings.max_wait_for_result_ms)
        preamble = approval_request_preamble(tool=tool.name, server=server_id)
        description = f"{preamble}\n\n{tool.description}" if tool.description else preamble
    return ProxyTool(
        name=name,
        title=title,
        description=description,
        parameters=parameters,
        # TODO: proxied tools intentionally declare no output schema. A call can return either the
        # upstream result or, when approval/execution outlasts `wait_for_result_ms`, a `ToolCallStub`;
        # modeling that union as a top-level `oneOf` is rejected by claude.ai, which requires
        # `outputSchema.type == "object"` (anthropics/claude-ai-mcp#400). outputSchema is optional
        # in MCP, so omitting it is conformant; the stub behavior is described in the tool
        # description. Restore a single conformant object shape if a client needs structured typing.
        output_schema=None,
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
                approval_mode=ApprovalMode.PASSTHROUGH if passthrough else ApprovalMode.APPROVAL_REQUIRED,
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

    async def metadata(self, server: McpServerEntry, actor: RuntimeActor) -> ServerReflection:
        return self._context.catalogs.metadata(operator_id=actor.operator_id, server=server)


def _unavailable_server_message(server_id: str, reason: str) -> str:
    return (
        f"MCP server {server_id!r} is unavailable: {reason.rstrip().rstrip('.') or 'unknown availability error'}. "
        f"Use get_mcp_server_status(server_id={server_id!r}) to check it; "
        "reconnect the server in the console if its OAuth connection has expired or been revoked."
    )


class OperatorToolProvider(Provider):
    """Reflect the connected-server catalog for the current principal's Operator.

    FastMCP providers are consulted for both ``tools/list`` and ``tools/call``.
    Discovery reads a generation already published by the background reconciler. Tool execution
    independently re-resolves authorization, so a retained schema is never execution authority.
    """

    def __init__(
        self,
        context: ConsoleMcpContext,
        actor_resolver: HakuMcpActorResolver,
        catalog: OperatorServerCatalog | None = None,
        policies: AutoApprovalPolicyRegistry | None = None,
    ) -> None:
        super().__init__()
        self._context = context
        self._actor_resolver = actor_resolver
        self._catalog = catalog or OperatorServerCatalog(context)
        self._auto_approval_policies = policies or AutoApprovalPolicyRegistry(
            load_console_config(context.settings.config_file)
        )

    def _is_passthrough(self, actor: RuntimeActor, server_id: str, tool_name: str) -> bool:
        return _is_passthrough(self._auto_approval_policies, actor, server_id, tool_name)

    async def _server_tools(self, server: McpServerEntry, actor: RuntimeActor) -> list[Tool]:
        try:
            meta = await self._catalog.metadata(server, actor)
        except Exception:
            # Discovery is an aggregate availability surface: an unexpected failure in one server
            # must remain visible in logs without erasing every unrelated server's tools.
            logger.exception("mcp.server: failed to reflect server %s for Operator %s", server.id, actor.operator_id)
            return []
        if isinstance(meta, DegradedReflection):
            logger.info(
                "mcp.server: hiding unavailable server %s from Operator %s: %s",
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
                passthrough=self._is_passthrough(actor, server.id, tool.name),
                actor=actor,
            )
            for tool in meta.tools
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        actor = await self._actor_resolver.resolve()
        # Snapshot reads are async only because FastMCP's Provider contract is async. They perform
        # no downstream or OAuth I/O; gather preserves configured server order.
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
        if isinstance(meta, DegradedReflection):
            return None
        for upstream_tool in meta.tools:
            tool = _build_proxy_tool(
                self._context,
                server.id,
                upstream_tool,
                passthrough=self._is_passthrough(actor, server.id, upstream_tool.name),
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
            if isinstance(meta, DegradedReflection):
                raise ToolError(_unavailable_server_message(server.id, meta.degraded_reason)) from error
            raise


def build_console_mcp(
    context: ConsoleMcpContext, *, auth: AuthProvider, actor_resolver: HakuMcpActorResolver
) -> FastMCP:
    """Build the console MCP server with request-local proxy tools + auth.

    Authentication is composed by :mod:`haku.console.identity.mcp_agent_auth`. The
    provider reflects connected-server tools for the authenticated principal's
    canonical Operator on each discovery and dispatch request.
    """
    mcp: FastMCP = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    mcp.auth = auth
    catalog = OperatorServerCatalog(context)
    # One registry for both the generated proxies and `call_mcp_tool`, so the two can never disagree
    # about which payload shape a tool takes.
    policies = AutoApprovalPolicyRegistry(load_console_config(context.settings.config_file))
    mcp.add_provider(OperatorToolProvider(context, actor_resolver, catalog, policies))
    mcp.add_middleware(OperatorToolAvailabilityMiddleware(catalog, actor_resolver))

    current_actor_dependency = Depends(actor_resolver.resolve)

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_mcp_servers(actor: RuntimeActor = current_actor_dependency) -> McpServerConnectionStatusResponse:
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
        return await _passive_server_connection_statuses(context, actor)

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_node_daemons(actor: RuntimeActor = current_actor_dependency) -> DaemonStatusResponse:
        """List configured node daemons and their current persisted heartbeat/lease status.

        Use this to check whether approved node work can currently be dispatched; do not use it to
        submit or alter work. Each result includes the daemon's derived presence state, last
        heartbeat, advertised backends/version, and active execution when one exists. This is a
        read-only console-state view and does not contact a daemon or renew its lease.
        """
        _ = actor
        return (
            await context.node_daemons.statuses()
            if context.node_daemons is not None
            else DaemonStatusResponse(daemons=[])
        )

    @mcp.tool(annotations=_READ_ONLY_META)
    async def get_mcp_server_status(
        server_id: str, include_tool_schemas: bool = False, actor: RuntimeActor = current_actor_dependency
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
            for status in (await _passive_server_connection_statuses(context, actor)).servers
            if status.server_id == server_id
        )
        reflection = await metadata_for_operator(
            operator_id=actor.operator_id,
            server=server,
            dispatcher=context.dispatcher,
            oauth_store=context.oauth_store,
            provider_store=context.provider_store,
        )
        return McpServerProbeResponse(
            connection=connection,
            server=_exposed_metadata(
                server_metadata_response(server_id, reflection),
                policies=policies,
                actor=actor,
                include_schemas=include_tool_schemas,
                max_wait_ms=context.settings.max_wait_for_result_ms,
            ),
        )

    @mcp.tool(annotations=_GENERIC_DISPATCH_META)
    async def call_mcp_tool(
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        actor: RuntimeActor = current_actor_dependency,
    ) -> ToolResult:
        """Call a configured tool by server/tool name when it is missing from the tool list.

        Read `get_mcp_server_status(server_id, include_tool_schemas=True)` first and pass the
        exposed `input_schema` shape verbatim: raw arguments for pass-through tools, or the
        approval envelope for approval-required tools.
        """
        servers = _load_servers(context.settings)
        if not any(server.id == server_id for server in servers):
            known = ", ".join(sorted(server.id for server in servers)) or "(none configured)"
            raise ToolError(
                f"unknown configured MCP server {server_id!r}; configured servers: {known}. "
                "Use list_mcp_servers to inspect them."
            )
        passthrough = _is_passthrough(policies, actor, server_id, tool_name)
        try:
            return await _dispatch(
                context,
                server_id=server_id,
                tool_name=tool_name,
                arguments=arguments or {},
                passthrough=passthrough,
                actor=actor,
            )
        except ValidationError as error:
            # The named tool advertises its shape in its own schema, so a client cannot get this
            # wrong; a caller naming the tool by hand can, and the bare pydantic error does not say
            # which of the two shapes was expected.
            raise ToolError(
                f"{tool_name!r} on {server_id!r} requires operator approval, so `arguments` must be the "
                "envelope {input: <the real arguments>, rationale: <shown to the operator>, title?, "
                f"wait_for_result_ms?}}, not the arguments themselves. Details: {error}"
            ) from error

    @mcp.tool(annotations=_READ_ONLY_META)
    async def get_tool_call(
        tool_call_id: str,
        fields: Annotated[
            list[ToolCallPayloadField],
            Field(
                description="Whole payloads to include. Defaults to [`result`]; pass [] for a compact status poll.",
                json_schema_extra={"default": [ToolCallPayloadField.RESULT]},
            ),
        ] = _DEFAULT_GET_TOOL_CALL_FIELDS,
        actor: RuntimeActor = current_actor_dependency,
    ) -> McpToolCallResponse:
        """Read one tool call: status, selected payloads, terminal reason, and approval link."""
        try:
            record = await context.tool_calls.get(tool_call_id, actor=actor, fields=frozenset(fields))
        except (ToolCallNotFoundError, ToolCallStateConflictError) as error:
            raise ToolError(str(error)) from error
        return _mcp_tool_call_response(record, context.settings)

    @mcp.tool(annotations=_LEDGER_MUTATION_META)
    async def withdraw_tool_call(
        tool_call_id: str, reason: str | None = None, actor: RuntimeActor = current_actor_dependency
    ) -> ToolCallView:
        """Retract your own tool call while it is still `pending_approval`.

        Withdrawal removes it from the operator queue but never stops an approved call; use
        `get_tool_call` to read that call's outcome. `reason` is shown to the operator.
        """
        try:
            record = await context.tool_calls.withdraw(tool_call_id=tool_call_id, reason=reason, actor=actor)
        except (ToolCallNotFoundError, ToolCallStateConflictError, AgentActorRequiredError) as error:
            raise ToolError(str(error)) from error
        return ToolCallView(call=record, url=_tool_call_url(context.settings, tool_call_id))

    @mcp.tool(annotations=_READ_ONLY_META)
    async def list_tool_calls(
        *,
        status: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        auto_approved: bool | None = None,
        fields: Annotated[
            list[ToolCallPayloadField],
            Field(
                description="Whole payloads to include. Defaults to [] for compact status summaries.",
                json_schema_extra={"default": []},
            ),
        ] = _DEFAULT_LIST_TOOL_CALL_FIELDS,
        limit: int = 100,
        newest_first: bool = True,
        actor: RuntimeActor = current_actor_dependency,
    ) -> list[McpToolCallResponse]:
        """List recent tool calls (newest first by default), optionally filtered by status/since/
        auto_approved (true: only calls the reviewed policy auto-approved; false: only calls that
        went through manual or no approval; omitted: no filter).
        """
        records = await context.tool_calls.list_tool_calls(
            actor=actor,
            fields=frozenset(fields),
            statuses=status,
            since=since,
            auto_approved=auto_approved,
            limit=limit,
            newest_first=newest_first,
        )
        return [_mcp_tool_call_response(record, context.settings) for record in records]

    return mcp
