"""The first concrete Executor wiring gate adapter: one ActionGroup backed by one MCP server.

Owns a persistent connection to the configured MCP server, mirrors its `tools/list` into the
bound `ActionGroup`'s catalog, and initiates `tools/call` itself — the Agent/harness never gets
a direct MCP client. See plans/operations_and_access.md § "Action groups, MCP discovery, and
backend ownership".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Literal, cast

import jsonschema
import mcp.types
from fastmcp.client import Client, ClientTransport
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, field_validator

from x.agentplane.action_service.catalog import ActionDefinition, ActionGroup, Key, McpExecutorBinding
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState
from x.agentplane.action_service.service import ExecutionOutcomeUnknownError

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_REFRESH_INTERVAL = timedelta(minutes=5)
_KEY_ADAPTER = TypeAdapter(Key)


class McpStdioServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class McpHttpServerConfig(BaseModel):
    """Credentialless endpoint; authentication and header forwarding are not configurable here."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["streamable-http"]
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def validate_endpoint(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username is not None or url.password is not None or url.fragment is not None:
            raise ValueError("MCP endpoint must not contain userinfo or a fragment")
        return url


McpServerConfig = McpStdioServerConfig | McpHttpServerConfig
_SERVER_CONFIG_ADAPTER: TypeAdapter[McpServerConfig] = TypeAdapter(McpServerConfig)


class _ToolListChangeHandler(MessageHandler):
    def __init__(self, changed: asyncio.Event) -> None:
        self._changed = changed

    async def on_tool_list_changed(self, message: mcp.types.ToolListChangedNotification) -> None:
        del message
        self._changed.set()


class McpActionGroupExecutor:
    """Implements `Executor` for exactly one `ActionGroup` backed by one MCP server connection."""

    def __init__(
        self,
        group_key: str,
        group: ActionGroup,
        transport: ClientTransport | Any,
        *,
        catalog_refresh_interval: timedelta = DEFAULT_CATALOG_REFRESH_INTERVAL,
    ) -> None:
        self._group_key = group_key
        self._group = group
        self._catalog_refresh_interval = catalog_refresh_interval
        self._tool_list_changed = asyncio.Event()
        self._client = Client(transport, message_handler=_ToolListChangeHandler(self._tool_list_changed))
        self._stack = AsyncExitStack()
        self._refresh_task: asyncio.Task[None] | None = None

    @classmethod
    def from_group(
        cls,
        group_key: str,
        group: ActionGroup,
        *,
        catalog_refresh_interval: timedelta = DEFAULT_CATALOG_REFRESH_INTERVAL,
    ) -> McpActionGroupExecutor:
        if not isinstance(group.executor, McpExecutorBinding):
            raise ValueError("unsupported executor binding; expected MCP")
        config = _SERVER_CONFIG_ADAPTER.validate_python(group.executor.config)
        transport: ClientTransport
        if isinstance(config, McpStdioServerConfig):
            transport = StdioTransport(config.command, config.args, env=config.env or None, cwd=config.cwd)
        else:
            transport = StreamableHttpTransport(config.url, auth=None)
        return cls(group_key, group, transport, catalog_refresh_interval=catalog_refresh_interval)

    async def start(self) -> None:
        await self._stack.enter_async_context(self._client)
        await self.refresh_catalog()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name=f"mcp-executor-refresh-{self._group_key}")

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        await self._stack.aclose()

    async def _refresh_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._tool_list_changed.wait(), timeout=self._catalog_refresh_interval.total_seconds()
                )
            self._tool_list_changed.clear()
            try:
                await self.refresh_catalog()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("periodic MCP catalog refresh failed; will retry", exc_info=True)

    async def refresh_catalog(self) -> None:
        try:
            tools = await self._client.list_tools()
        except Exception:
            logger.warning("MCP tools/list failed; %s marked unavailable", self._group_key, exc_info=True)
            self._group.available = False
            self._group.actions = {}
            return
        actions: dict[str, ActionDefinition] = {}
        for tool in tools:
            try:
                key = _KEY_ADAPTER.validate_python(tool.name)
            except ValidationError:
                logger.warning("MCP tool name %r does not fit the catalog key pattern; skipping", tool.name)
                continue
            actions[key] = ActionDefinition(
                description=tool.description or f"MCP tool {tool.name}", input_schema=tool.input_schema
            )
        self._group.actions = actions
        self._group.available = True

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        group_key, name = request.action.group, request.action.name
        if group_key != self._group_key:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "unknown_action", "message": "action is not owned by this group"},
            )

        try:
            tools = await self._client.list_tools()
        except Exception:
            logger.warning("MCP tools/list failed before dispatch; refusing without calling the backend", exc_info=True)
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "mcp_unavailable", "message": "could not verify the current tool schema"},
            )

        tool = next((tool for tool in tools if tool.name == name), None)
        if tool is None:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "unknown_action", "message": "action is no longer offered by the backend"},
            )

        try:
            jsonschema.validate(request.arguments, tool.input_schema)
        except jsonschema.ValidationError:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={
                    "kind": "incompatible_action_schema",
                    "message": "arguments no longer match the current tool schema",
                },
            )

        try:
            result = await self._client.call_tool(name, request.arguments, raise_on_error=False)
        except Exception as error:
            raise ExecutionOutcomeUnknownError(f"MCP tools/call transport failure for {name}") from error

        if result.is_error:
            return ExecutionResult(
                state=ExecutionState.FAILED, error={"kind": "mcp_tool_error", "message": "MCP tool reported an error"}
            )
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result=_safe_result(result))


def _safe_result(result: Any) -> JsonValue:
    if result.structured_content is not None:
        return cast(JsonValue, result.structured_content)
    texts: list[JsonValue] = [block.text for block in result.content if isinstance(block, mcp.types.TextContent)]
    return {"content": texts}
