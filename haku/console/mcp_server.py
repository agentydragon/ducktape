"""haku-console's own MCP server: the connected-server tools, re-exposed to a Claude agent.

An interactive MCP client (the claude.ai custom connector or the ``claude`` CLI) connects here
and calls the console's connected-server tools directly. Every call is **submitted through the
shared application service rather than executed inline** (`ToolCallApplicationService.submit_and_wait`):
it auto-approves and runs when the reviewed policy allows, otherwise it returns a **promise** (a pending
``tool_call_id`` plus an operator-facing deep link) that the agent resolves later via
``get_tool_call``.

Each request exposes only servers connected by that Agent's canonical Operator. Within that
Operator-specific surface, the global auto-approval policy divides tools into two buckets:

Every proxied tool is named ``<server>_<tool>`` (one uniform format — operator decision
2026-07-13; bare upstream names hid which server a tool belonged to):

- **Pass-through** — tools the policy unconditionally auto-approves (gmail reads): the upstream
  schema and description unchanged, so they behave like the real tool and return the real result.
- **Request** — everything else: an envelope schema (``input`` + ``rationale`` + optional
  ``title``/``wait_for_approval_ms``) and a promise-semantics preamble in the description;
  returns the real result *or* a promise.

Both buckets run through the single ``submit_and_wait`` path.
"""

from __future__ import annotations

import copy
import datetime
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from haku.console.auto_approval import is_unconditionally_auto_approved
from haku.console.config import Settings
from haku.console.mcp_approval import DegradedServerMetadata, McpMetadataProvider, metadata_for_operator
from haku.console.mcp_auth.fastmcp_adapter import get_agent_actor
from haku.console.mcp_config import McpServerNotFoundError, _load_servers
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.tool_call_actor import AgentActor
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
    """The application service and MCP-specific adapters needed by the FastMCP transport."""

    settings: Settings
    tool_calls: ToolCallApplicationService
    oauth_store: PostgresMcpOperatorOAuthStore
    metadata_provider: McpMetadataProvider


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


class ProxyTool(Tool):
    """A connected-server tool re-exposed through the shared application service.

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
        actor = get_agent_actor()
        try:
            record = await ctx.tool_calls.submit_and_wait(req=req, actor=actor)
        except (
            BackendAccountNotConnectedError,
            McpServerNotFoundError,
            ToolCallNotFoundError,
            ToolCallStateConflictError,
        ) as error:
            raise ToolError(str(error)) from error
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
        meta={
            MCP_TOOL_META_KEY: McpProxyToolMetadata(
                server_id=server_id,
                upstream_tool_name=tool.name,
                approval_mode="passthrough" if passthrough else "approval_required",
            ).model_dump(mode="json")
        },
    )


class OperatorToolProvider(Provider):
    """Reflect the connected-server catalog for the current Agent's Operator.

    FastMCP providers are consulted for both ``tools/list`` and ``tools/call``.
    Keeping reflection here makes discovery request-local and also fails closed
    if a client calls a tool after its Operator disconnects that server.
    """

    def __init__(self, context: ConsoleMcpContext) -> None:
        super().__init__()
        self._context = context

    async def _list_tools(self) -> Sequence[Tool]:
        actor = get_agent_actor()
        tools: list[Tool] = []
        for server in _load_servers(self._context.settings):
            meta = await metadata_for_operator(
                operator_id=actor.operator_id,
                server=server,
                metadata_provider=self._context.metadata_provider,
                oauth_store=self._context.oauth_store,
            )
            if isinstance(meta, DegradedServerMetadata):
                logger.info(
                    "mcp_server: hiding unavailable server %s from Operator %s: %s",
                    server.id,
                    actor.operator_id,
                    meta.degraded_reason,
                )
                continue
            tools.extend(
                _build_proxy_tool(
                    self._context, server.id, tool, passthrough=is_unconditionally_auto_approved(server.id, tool.name)
                )
                for tool in meta.tools
            )
        return tools

    async def get_tasks(self) -> Sequence[Tool]:
        # Proxy tools forbid background tasks, and startup has no request actor.
        return []


def build_console_mcp(context: ConsoleMcpContext, *, auth: AuthProvider) -> FastMCP:
    """Build the console MCP server with request-local proxy tools + auth.

    Authentication is composed by :mod:`haku.console.mcp_agent_auth`. The
    provider reflects connected-server tools for the authenticated Agent's
    canonical Operator on each discovery and dispatch request.
    """
    mcp: FastMCP = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)
    mcp.auth = auth
    mcp.add_provider(OperatorToolProvider(context))

    current_agent_dependency = Depends(get_agent_actor)

    @mcp.tool
    async def get_tool_call(tool_call_id: str, actor: AgentActor = current_agent_dependency) -> ToolCallView:
        """Read one tool call (resolve a promise): status, result/error, and its approval link."""
        try:
            record = context.tool_calls.get(tool_call_id, actor=actor)
        except (ToolCallNotFoundError, ToolCallStateConflictError) as error:
            raise ToolError(str(error)) from error
        return ToolCallView(call=record, url=_tool_call_url(context.settings, tool_call_id))

    @mcp.tool
    async def list_tool_calls(
        status: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = True,
        actor: AgentActor = current_agent_dependency,
    ) -> list[ToolCallView]:
        """List recent tool calls (newest first by default), optionally filtered by status/since."""
        records = context.tool_calls.list_tool_calls(
            actor=actor, statuses=status, since=since, limit=limit, newest_first=newest_first
        )
        return [ToolCallView(call=r, url=_tool_call_url(context.settings, r.tool_call_id)) for r in records]

    return mcp
