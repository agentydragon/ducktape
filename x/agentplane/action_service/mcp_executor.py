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
from typing import Any, cast

import jsonschema
import mcp.types
from fastmcp.client import Client, ClientTransport
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from x.agentplane.action_service.catalog import ActionDefinition, ActionGroup, Key
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState
from x.agentplane.action_service.service import ExecutionOutcomeUnknownError

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_REFRESH_INTERVAL = timedelta(minutes=5)
_KEY_ADAPTER = TypeAdapter(Key)


class McpServerConfig(BaseModel):
    """Stdio MCP server launch config, parsed from `ExecutorBinding.config` for an `mcp`-kind group."""

    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


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
        config = McpServerConfig.model_validate(group.executor.config)
        transport = StdioTransport(config.command, config.args, env=config.env or None, cwd=config.cwd)
        return cls(group_key, group, transport, catalog_refresh_interval=catalog_refresh_interval)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(f"{self._group_key}.{name}" for name in self._group.actions)

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
                description=tool.description or f"MCP tool {tool.name}", input_schema=tool.inputSchema
            )
        self._group.actions = actions
        self._group.available = True

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        prefix = f"{self._group_key}."
        if not request.capability.startswith(prefix):
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "unknown_action", "message": "capability is not owned by this executor"},
            )
        name = request.capability.removeprefix(prefix)

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
            jsonschema.validate(request.arguments, tool.inputSchema)
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
