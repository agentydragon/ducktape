"""The first concrete Executor wiring gate adapter: one ActionGroup backed by one MCP server.

Owns the current FastMCP connection to the configured MCP server, mirrors its `tools/list` into
the bound `ActionGroup`'s catalog, and initiates `tools/call` itself — the Agent/harness never
gets a direct MCP client. OAuth-backed groups may be dormant until their shared linkage authority
has a token. See plans/operations_and_access.md § "Action groups, MCP discovery, and backend
ownership".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Literal, cast

import httpx
import jsonschema
import mcp.types
from fastmcp.client import Client, ClientTransport
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from x.agentplane.action_service.catalog import ActionDefinition, ActionGroup, Key, McpExecutorBinding
from x.agentplane.action_service.mcp_linkage import McpLinkageAuthority, McpLinkageStatus
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState
from x.agentplane.action_service.service import ExecutionOutcomeUnknownError

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_REFRESH_INTERVAL = timedelta(minutes=5)
LINKAGE_POLL_INTERVAL = timedelta(seconds=5)
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
    server_id: Key | None = None
    auth: Literal["none", "oauth"] = "none"

    @model_validator(mode="after")
    def require_linkage_server(self) -> McpHttpServerConfig:
        if self.auth == "oauth" and self.server_id is None:
            raise ValueError("OAuth MCP HTTP config requires server_id")
        return self

    @field_validator("url")
    @classmethod
    def validate_endpoint(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username is not None or url.password is not None or url.fragment is not None or url.query is not None:
            raise ValueError("MCP endpoint must not contain userinfo, a query, or a fragment")
        return url


McpServerConfig = McpStdioServerConfig | McpHttpServerConfig
_SERVER_CONFIG_ADAPTER: TypeAdapter[McpServerConfig] = TypeAdapter(McpServerConfig)


class _ToolListChangeHandler(MessageHandler):
    def __init__(self, changed: asyncio.Event) -> None:
        self._changed = changed

    async def on_tool_list_changed(self, message: mcp.types.ToolListChangedNotification) -> None:
        del message
        self._changed.set()


class _LinkageBearerAuth(httpx.Auth):
    requires_response_body = False

    def __init__(self, linkage: McpLinkageAuthority, server_id: str) -> None:
        self._linkage = linkage
        self._server_id = server_id

    async def async_auth_flow(self, request: httpx.Request):
        token = await self._linkage.access_token_for_execution(self._server_id)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class _InvalidMcpCatalogError(Exception):
    pass


class McpActionGroupExecutor:
    """Implements `Executor` for exactly one `ActionGroup` backed by one MCP server connection."""

    def __init__(
        self,
        group_key: str,
        group: ActionGroup,
        transport: ClientTransport | Any | None = None,
        *,
        catalog_refresh_interval: timedelta = DEFAULT_CATALOG_REFRESH_INTERVAL,
        execution_timeout: timedelta = timedelta(minutes=10),
        transport_factory: Callable[[], ClientTransport | Any] | None = None,
    ) -> None:
        if execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive")
        self._execution_timeout = execution_timeout
        self._group_key = group_key
        self._group = group
        self._catalog_refresh_interval = catalog_refresh_interval
        self._tool_list_changed = asyncio.Event()
        if transport_factory is None:
            if transport is None:
                raise ValueError("an MCP transport or transport factory is required")

            def make_transport() -> ClientTransport | Any:
                return transport

            self._transport_factory = make_transport
        else:
            self._transport_factory = transport_factory
        self._client: Client[Any] | None = None
        self._stack: AsyncExitStack | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._linkage_task: asyncio.Task[None] | None = None
        self._linkage: McpLinkageAuthority | None = None
        self._linkage_server_id: str | None = None
        self._linkage_changed: asyncio.Event | None = None
        self._requires_linkage = False

    @property
    def requires_linkage(self) -> bool:
        return self._requires_linkage

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
        if isinstance(config, McpStdioServerConfig):

            def transport_factory() -> ClientTransport:
                return StdioTransport(config.command, config.args, env=config.env or None, cwd=config.cwd)
        else:

            def transport_factory() -> ClientTransport:
                return StreamableHttpTransport(config.url, auth=None)

        return cls(
            group_key, group, catalog_refresh_interval=catalog_refresh_interval, transport_factory=transport_factory
        )

    @classmethod
    def from_group_with_linkage(
        cls,
        group_key: str,
        group: ActionGroup,
        linkage: McpLinkageAuthority,
        *,
        catalog_refresh_interval: timedelta = DEFAULT_CATALOG_REFRESH_INTERVAL,
    ) -> McpActionGroupExecutor:
        if not isinstance(group.executor, McpExecutorBinding):
            raise ValueError("unsupported executor binding; expected MCP")
        config = _SERVER_CONFIG_ADAPTER.validate_python(group.executor.config)
        if not isinstance(config, McpHttpServerConfig) or config.auth != "oauth" or config.server_id is None:
            return cls.from_group(group_key, group, catalog_refresh_interval=catalog_refresh_interval)
        server_id = config.server_id

        def transport_factory() -> ClientTransport:
            return StreamableHttpTransport(config.url, auth=_LinkageBearerAuth(linkage, server_id))

        executor = cls(
            group_key, group, catalog_refresh_interval=catalog_refresh_interval, transport_factory=transport_factory
        )
        executor._linkage = linkage
        executor._linkage_server_id = server_id
        executor._linkage_changed = linkage.subscribe_changes(server_id)
        executor._requires_linkage = True
        return executor

    async def start(self) -> None:
        self._group.available = False
        self._group.actions = {}
        if self._requires_linkage:
            self._linkage_task = asyncio.create_task(
                self._linkage_loop(), name=f"mcp-executor-linkage-{self._group_key}"
            )
            return
        await self._connect_client()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name=f"mcp-executor-refresh-{self._group_key}")

    async def close(self) -> None:
        for task_name in ("_refresh_task", "_linkage_task"):
            task = getattr(self, task_name)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, task_name, None)
        await self._disconnect_client()
        if self._linkage is not None and self._linkage_server_id is not None and self._linkage_changed is not None:
            self._linkage.unsubscribe_changes(self._linkage_server_id, self._linkage_changed)
            self._linkage_changed = None

    async def _connect_client(self) -> None:
        client = Client[Any](self._transport_factory(), message_handler=_ToolListChangeHandler(self._tool_list_changed))
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(client)
            actions = await self._discover_catalog(client)
        except _InvalidMcpCatalogError:
            self._client = client
            self._stack = stack
            self._mark_unavailable()
            return
        except BaseException:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        self._client = client
        self._stack = stack
        self._publish_catalog(actions)

    async def _disconnect_client(self, *, suppress_errors: bool = False) -> None:
        stack = self._stack
        self._client = None
        self._stack = None
        if stack is None:
            return
        if suppress_errors:
            with contextlib.suppress(Exception):
                await stack.aclose()
        else:
            await stack.aclose()

    async def _linkage_loop(self) -> None:
        while True:
            if self._client is None:
                self._clear_wakeup()
                if not await self._linkage_is_ready():
                    self._mark_unavailable()
                    await self._wait_for_wakeup(LINKAGE_POLL_INTERVAL)
                    continue
                try:
                    await self._connect_client()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("MCP OAuth connection failed; %s remains unavailable", self._group_key)
                    self._mark_unavailable()
                    await self._wait_for_wakeup(LINKAGE_POLL_INTERVAL)
                continue

            await self._wait_for_wakeup(self._catalog_refresh_interval)
            self._clear_wakeup()
            client = self._client
            if client is None:
                continue
            if not await self._linkage_is_ready():
                self._mark_unavailable()
                await self._disconnect_client(suppress_errors=True)
                continue
            try:
                actions = await self._discover_catalog(client)
            except asyncio.CancelledError:
                raise
            except _InvalidMcpCatalogError:
                logger.warning("MCP catalog invalid; %s marked unavailable", self._group_key)
                self._mark_unavailable()
            except Exception:
                logger.warning("MCP OAuth session failed; reconnecting %s", self._group_key)
                self._mark_unavailable()
                await self._disconnect_client(suppress_errors=True)
            else:
                self._publish_catalog(actions)

    async def _linkage_is_ready(self) -> bool:
        if self._linkage is None or self._linkage_server_id is None:
            return False
        try:
            status = await self._linkage.status(self._linkage_server_id)
        except Exception:
            logger.warning("MCP OAuth linkage status failed; %s remains unavailable", self._group_key)
            return False
        return status.status is McpLinkageStatus.LINKED

    async def _wait_for_wakeup(self, delay: timedelta) -> None:
        events = [self._tool_list_changed]
        if self._linkage_changed is not None:
            events.append(self._linkage_changed)
        waiters = [asyncio.create_task(event.wait()) for event in events]
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay.total_seconds()):
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)

    def _clear_wakeup(self) -> None:
        self._tool_list_changed.clear()
        if self._linkage_changed is not None:
            self._linkage_changed.clear()

    async def _discover_catalog(self, client: Client[Any]) -> dict[str, ActionDefinition]:
        tools = await client.list_tools()
        actions: dict[str, ActionDefinition] = {}
        for tool in tools:
            try:
                key = _KEY_ADAPTER.validate_python(tool.name)
            except ValidationError:
                logger.warning("MCP tool name %r does not fit the catalog key pattern; skipping", tool.name)
                continue
            try:
                jsonschema.validators.validator_for(tool.inputSchema).check_schema(tool.inputSchema)
                if key in actions:
                    raise ValueError("duplicate MCP tool name")
                actions[key] = ActionDefinition(
                    description=tool.description or f"MCP tool {tool.name}", input_schema=tool.inputSchema
                )
            except (jsonschema.SchemaError, ValueError) as error:
                raise _InvalidMcpCatalogError from error
        return actions

    def _publish_catalog(self, actions: dict[str, ActionDefinition]) -> None:
        self._group.actions = actions
        self._group.available = True

    def _mark_unavailable(self) -> None:
        self._group.available = False
        self._group.actions = {}

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
                logger.warning("periodic MCP catalog refresh failed; will retry")

    async def refresh_catalog(self) -> None:
        client = self._client
        if client is None:
            self._mark_unavailable()
            return
        try:
            actions = await self._discover_catalog(client)
        except _InvalidMcpCatalogError:
            logger.warning("MCP catalog invalid; %s marked unavailable", self._group_key)
            self._mark_unavailable()
            return
        except Exception:
            logger.warning("MCP tools/list failed; %s marked unavailable", self._group_key)
            self._mark_unavailable()
            return
        self._publish_catalog(actions)

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        # Ownership/liveness, not evidence of backend progress. Bound the whole exchange so
        # an unresponsive remote call cannot be kept running indefinitely by our renewals.
        await self._renew(lease)
        execution = asyncio.create_task(self._execute(request, lease), name="mcp-execution")
        renewal = asyncio.create_task(self._renew_loop(lease), name="mcp-execution-renewal")
        try:
            async with asyncio.timeout(self._execution_timeout.total_seconds()):
                await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
                if execution.done():
                    return await execution
                await renewal
                raise AssertionError("lease renewal loop returned")
        except TimeoutError:
            raise ExecutionOutcomeUnknownError("MCP execution deadline exceeded") from None
        finally:
            execution.cancel()
            renewal.cancel()
            await asyncio.gather(execution, renewal, return_exceptions=True)

    async def _renew(self, lease: ExecutionLease) -> None:
        try:
            async with asyncio.timeout(lease.renewal_interval.total_seconds()):
                owned = await lease.heartbeat()
        except Exception:
            # Database/transport errors can contain credentials. Losing proof of ownership
            # stops local waiting, not the remote side effect, and never permits replay.
            raise ExecutionOutcomeUnknownError("MCP lease renewal failed") from None
        if not owned:
            raise ExecutionOutcomeUnknownError("MCP execution lease lost")

    async def _renew_loop(self, lease: ExecutionLease) -> None:
        while True:
            await asyncio.sleep(lease.renewal_interval.total_seconds())
            await self._renew(lease)

    async def _execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        group_key, name = request.action.group, request.action.name
        if group_key != self._group_key:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "unknown_action", "message": "action is not owned by this group"},
            )

        client = self._client
        if client is None:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "mcp_unavailable", "message": "could not verify the current tool schema"},
            )

        try:
            tools = await client.list_tools()
        except Exception:
            logger.warning("MCP tools/list failed before dispatch; refusing without calling the backend")
            if self._requires_linkage:
                self._tool_list_changed.set()
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
        except jsonschema.SchemaError:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "mcp_invalid_schema", "message": "backend tool schema is invalid"},
            )
        except jsonschema.ValidationError:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={
                    "kind": "incompatible_action_schema",
                    "message": "arguments no longer match the current tool schema",
                },
            )

        await self._renew(lease)
        try:
            result = await client.call_tool(name, request.arguments, raise_on_error=False)
        except Exception as error:
            if self._requires_linkage:
                self._tool_list_changed.set()
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
