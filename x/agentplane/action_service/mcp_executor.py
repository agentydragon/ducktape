"""The first concrete Executor wiring gate adapter: one ActionGroup backed by one MCP server.

Owns the current FastMCP connection to the configured MCP server, mirrors its `tools/list` into
the bound `ActionGroup`'s catalog, and initiates `tools/call` itself — the Agent/harness never
gets a direct MCP client. OAuth-backed groups may be dormant until their shared linkage authority
has a token. See x/agentplane/action_service/README.md § "Action catalog" and § "MCP executor
transports".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import httpx
import jsonschema
import mcp.types
from fastmcp.client import Client, ClientTransport
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, field_validator
from tenacity import RetryCallState, Retrying, wait_random_exponential

from x.agentplane.action_service.catalog import (
    ActionDefinition,
    ActionGroup,
    Key,
    McpExecutorBinding,
    McpHealth,
    McpLifecycle,
    McpUnavailableReason,
)
from x.agentplane.action_service.mcp_linkage import McpLinkageAuthority, McpLinkageStatus
from x.agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState
from x.agentplane.action_service.service import ExecutionOutcomeUnknownError

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_REFRESH_INTERVAL = timedelta(minutes=5)
LIFECYCLE_TIMEOUT = timedelta(seconds=15)
CLEANUP_TIMEOUT = timedelta(seconds=5)
STABLE_SUCCESS = timedelta(seconds=30)
_KEY_ADAPTER = TypeAdapter(Key)


class McpStdioServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class _McpHttpServerConfigBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["streamable-http"]
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def validate_endpoint(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username is not None or url.password is not None or url.fragment is not None or url.query is not None:
            raise ValueError("MCP endpoint must not contain userinfo, a query, or a fragment")
        return url


class McpHttpNoAuthServerConfig(_McpHttpServerConfigBase):
    auth: Literal["none"] = "none"
    server_id: None = None
    bearer_file: None = None


class McpHttpOAuthServerConfig(_McpHttpServerConfigBase):
    auth: Literal["oauth"] = "oauth"
    server_id: Key
    bearer_file: None = None


class McpHttpStaticBearerServerConfig(_McpHttpServerConfigBase):
    auth: Literal["static_bearer"] = "static_bearer"
    server_id: None = None
    bearer_file: Path


McpHttpServerConfigValue = McpHttpNoAuthServerConfig | McpHttpOAuthServerConfig | McpHttpStaticBearerServerConfig
McpHttpServerConfig = Annotated[McpHttpServerConfigValue, Field(discriminator="auth")]
McpServerConfig = Annotated[McpStdioServerConfig | McpHttpServerConfig, Field(discriminator="transport")]
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


def _http_auth(config: McpHttpServerConfigValue) -> str | None:
    """Resolve transport credentials by HTTP config variant, not optional fields."""
    if isinstance(config, McpHttpStaticBearerServerConfig):
        try:
            token = config.bearer_file.read_text().strip()
        except OSError:
            raise ValueError("configured MCP static bearer file is unavailable") from None
        if not token:
            raise ValueError("configured MCP static bearer file is empty")
        return token
    if isinstance(config, McpHttpNoAuthServerConfig):
        return None
    raise ValueError("OAuth MCP config requires the linkage-aware executor")


@dataclass(eq=False)
class _Connection:
    client: Client[Any]
    stack: AsyncExitStack
    inflight: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    failed: bool = False

    def __post_init__(self) -> None:
        self.idle.set()


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
        lifecycle_timeout: timedelta = LIFECYCLE_TIMEOUT,
    ) -> None:
        if execution_timeout <= timedelta(0):
            raise ValueError("execution_timeout must be positive")
        if lifecycle_timeout <= timedelta(0) or catalog_refresh_interval <= timedelta(0):
            raise ValueError("lifecycle and refresh timeouts must be positive")
        self._lifecycle_timeout = lifecycle_timeout
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
        self._connection: _Connection | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._retirements: dict[asyncio.Task[None], _Connection] = {}
        self._draining = False
        self._health = McpHealth()
        self._group.health = self._health
        self._available_since: float | None = None
        self._backoff = wait_random_exponential(multiplier=1, min=0.1, max=30)
        self._retry = RetryCallState(Retrying(), None, (), {})
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
        if isinstance(config, McpHttpOAuthServerConfig):
            raise ValueError("OAuth MCP config requires the linkage-aware executor")
        if isinstance(config, McpStdioServerConfig):

            def transport_factory() -> ClientTransport:
                return StdioTransport(config.command, config.args, env=config.env or None, cwd=config.cwd)
        else:

            def transport_factory() -> ClientTransport:
                return StreamableHttpTransport(config.url, auth=_http_auth(config))

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
        if not isinstance(config, McpHttpOAuthServerConfig):
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
        if self._supervisor is not None or self._draining:
            raise RuntimeError("MCP executor already started or stopped")
        self._mark_unavailable()
        self._supervisor = asyncio.create_task(self._supervise(), name=f"mcp-executor-{self._group_key}")
        self._supervisor.add_done_callback(self._supervisor_done)

    def _supervisor_done(self, task: asyncio.Task[None]) -> None:
        if not self._draining:
            self._mark_unavailable(McpUnavailableReason.SUPERVISOR_STOPPED)
            self._health.state = McpLifecycle.STOPPED
            logger.error("MCP supervisor stopped unexpectedly for %s", self._group_key)
        if not task.cancelled():
            task.exception()  # Retrieve without logging secret-bearing exception text.

    def begin_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self._mark_unavailable()
        self._health.state = McpLifecycle.DRAINING
        self._health.retry_at = None
        self._tool_list_changed.set()
        if self._supervisor is not None:
            # The supervisor does not own the lifetime of published connections.
            self._supervisor.cancel()

    async def close(self) -> None:
        self.begin_drain()
        if self._supervisor is not None:
            await asyncio.gather(self._supervisor, return_exceptions=True)
        connection, self._connection = self._connection, None
        if connection is not None:
            await self._close_connection(connection)
        retirements = dict(self._retirements)
        for task in retirements:
            task.cancel()
        await asyncio.gather(*retirements, return_exceptions=True)
        for retired in retirements.values():
            await self._close_connection(retired)
        if self._linkage is not None and self._linkage_server_id is not None and self._linkage_changed is not None:
            self._linkage.unsubscribe_changes(self._linkage_server_id, self._linkage_changed)
            self._linkage_changed = None
        self._health.state = McpLifecycle.STOPPED

    async def _close_connection(self, connection: _Connection) -> None:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT.total_seconds()):
                await connection.stack.aclose()
        except Exception:
            logger.warning("MCP connection cleanup failed for %s", self._group_key)

    async def _retire(self, connection: _Connection) -> None:
        try:
            async with asyncio.timeout(self._execution_timeout.total_seconds()):
                await connection.idle.wait()
        except TimeoutError:
            logger.warning("MCP connection retirement deadline reached for %s", self._group_key)
        finally:
            await self._close_connection(connection)

    def _retire_current(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            task = asyncio.create_task(self._retire(connection), name=f"mcp-retire-{self._group_key}")
            self._retirements[task] = connection
            task.add_done_callback(self._retired)

    def _retired(self, task: asyncio.Task[None]) -> None:
        self._retirements.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("MCP retirement failed for %s", self._group_key)

    async def _connect_client(self) -> None:
        self._health.state = McpLifecycle.CONNECTING
        client = Client[Any](
            self._transport_factory(),
            init_timeout=self._lifecycle_timeout,
            message_handler=_ToolListChangeHandler(self._tool_list_changed),
        )
        connection = _Connection(client, AsyncExitStack())
        # Failed __aenter__ is not registered by AsyncExitStack; close the SDK client too.
        connection.stack.push_async_callback(client.close)
        try:
            async with asyncio.timeout(self._lifecycle_timeout.total_seconds()):
                await connection.stack.enter_async_context(client)
        except BaseException:
            await self._close_connection(connection)
            raise
        self._connection = connection

    async def _supervise(self) -> None:
        while not self._draining:
            self._clear_wakeup()
            self._health.retry_at = None
            try:
                async with asyncio.timeout(self._lifecycle_timeout.total_seconds()):
                    ready = not self._requires_linkage or await self._linkage_is_ready()
                if not ready:
                    self._mark_unavailable(McpUnavailableReason.LINKAGE_UNAVAILABLE)
                    self._retire_current()
                else:
                    if self._connection is not None and self._connection.failed:
                        self._retire_current()
                    if self._connection is None:
                        await self._connect_client()
                    await self.refresh_catalog()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._mark_unavailable(McpUnavailableReason.CONNECT_FAILED)
                self._retire_current()
            if self._group.available:
                await self._wait_for_wakeup(self._catalog_refresh_interval)
            else:
                self._health.failures += 1
                self._retry.attempt_number = self._health.failures
                delay = timedelta(seconds=self._backoff(self._retry))
                self._health.retry_at = datetime.now(UTC) + delay
                logger.warning("MCP group %s unavailable: %s; retry scheduled", self._group_key, self._health.reason)
                await self._wait_for_wakeup(delay)

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
        waiters = [asyncio.create_task(event.wait(), name=f"mcp-wakeup-{self._group_key}") for event in events]
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay.total_seconds()):
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _clear_wakeup(self) -> None:
        self._tool_list_changed.clear()
        if self._linkage_changed is not None:
            self._linkage_changed.clear()

    async def _discover_catalog(self, client: Client[Any]) -> dict[str, ActionDefinition]:
        return self._catalog_from_tools(await client.list_tools())

    def _catalog_from_tools(self, tools: list[mcp.types.Tool]) -> dict[str, ActionDefinition]:
        actions: dict[str, ActionDefinition] = {}
        for tool in tools:
            try:
                key = _KEY_ADAPTER.validate_python(tool.name)
            except ValidationError:
                logger.warning("MCP tool name does not fit the catalog key pattern; skipping")
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
        if self._draining:
            return
        self._group.actions = actions
        self._group.available = True
        self._health.state = McpLifecycle.AVAILABLE
        self._health.reason = None
        self._health.retry_at = None
        self._health.last_discovery_at = datetime.now(UTC)
        now = asyncio.get_running_loop().time()
        if self._available_since is None:
            self._available_since = now
        if now - self._available_since >= STABLE_SUCCESS.total_seconds():
            self._health.failures = 0

    def _mark_unavailable(self, reason: McpUnavailableReason | None = None) -> None:
        if (
            self._available_since is not None
            and asyncio.get_running_loop().time() - self._available_since >= STABLE_SUCCESS.total_seconds()
        ):
            self._health.failures = 0
        self._group.available = False
        self._group.actions = {}
        self._health.state = McpLifecycle.DISCONNECTED
        self._health.reason = reason
        self._available_since = None

    def _session_failed(self, connection: _Connection) -> None:
        connection.failed = True
        if self._connection is connection:
            self._mark_unavailable(McpUnavailableReason.SESSION_FAILED)
            if asyncio.current_task() is not self._supervisor:
                self._tool_list_changed.set()

    async def refresh_catalog(self) -> None:
        connection = self._connection
        if connection is None or self._draining:
            return
        self._health.state = McpLifecycle.DISCOVERING
        try:
            async with asyncio.timeout(self._lifecycle_timeout.total_seconds()):
                actions = await self._discover_catalog(connection.client)
        except _InvalidMcpCatalogError:
            self._mark_unavailable(McpUnavailableReason.INVALID_CATALOG)
        except Exception:
            self._session_failed(connection)
            self._health.reason = McpUnavailableReason.DISCOVERY_FAILED
        else:
            if self._connection is connection and not connection.failed:
                self._publish_catalog(actions)

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        connection = self._connection
        if connection is None or connection.failed or not self._group.available or self._draining:
            return self._unavailable_result()
        # Pin before the first await; neither refresh nor reconnection may switch this execution.
        connection.inflight += 1
        connection.idle.clear()
        try:
            return await self._execute_with_lease(request, lease, connection)
        finally:
            connection.inflight -= 1
            if connection.inflight == 0:
                connection.idle.set()

    async def _execute_with_lease(
        self, request: ExecutionRequest, lease: ExecutionLease, connection: _Connection
    ) -> ExecutionResult:
        # Ownership/liveness, not evidence of backend progress. Bound the whole exchange so
        # an unresponsive remote call cannot be kept running indefinitely by our renewals.
        await self._renew(lease)
        execution = asyncio.create_task(self._execute(request, lease, connection), name="mcp-execution")
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

    @staticmethod
    def _unavailable_result() -> ExecutionResult:
        return ExecutionResult(
            state=ExecutionState.FAILED,
            error={"kind": "mcp_unavailable", "message": "could not verify the current tool schema"},
        )

    async def _execute(
        self, request: ExecutionRequest, lease: ExecutionLease, connection: _Connection
    ) -> ExecutionResult:
        group_key, name = request.action.group, request.action.name
        if group_key != self._group_key:
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "unknown_action", "message": "action is not owned by this group"},
            )

        client = connection.client
        try:
            async with asyncio.timeout(self._lifecycle_timeout.total_seconds()):
                tools = await client.list_tools()
        except Exception:
            self._session_failed(connection)
            return self._unavailable_result()

        try:
            actions = self._catalog_from_tools(tools)
        except _InvalidMcpCatalogError:
            if self._connection is connection:
                self._mark_unavailable(McpUnavailableReason.INVALID_CATALOG)
                self._tool_list_changed.set()
            return ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": "mcp_invalid_schema", "message": "backend tool schema is invalid"},
            )
        tool = actions.get(name)
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

        await self._renew(lease)
        if connection.failed or self._connection is not connection:
            return self._unavailable_result()
        try:
            result = await client.call_tool(name, request.arguments, raise_on_error=False)
        except Exception:
            self._session_failed(connection)
            raise ExecutionOutcomeUnknownError(f"MCP tools/call transport failure for {name}") from None

        if result.is_error and _mcp_error_kind(result) == "execution_unknown":
            raise ExecutionOutcomeUnknownError("MCP backend reported an unknown execution outcome")
        # A tool's error output is one of its two valid answers, not an execution failure: the
        # backend ran the call and replied. Only transport and outcome uncertainty fail here.
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result=_safe_result(result))


def _safe_result(result: Any) -> JsonValue:
    if result.is_error:
        payload: dict[str, JsonValue] = {"is_error": True, "content": _texts(result)}
        if result.structured_content is not None:
            payload["structured_content"] = cast(JsonValue, result.structured_content)
        return payload
    if result.structured_content is not None:
        return cast(JsonValue, result.structured_content)
    return {"content": _texts(result)}


def _texts(result: Any) -> list[JsonValue]:
    return [block.text for block in result.content if isinstance(block, mcp.types.TextContent)]


def _mcp_error_kind(result: Any) -> str | None:
    """Read only the bounded machine-readable kind used for backend unknown outcomes."""
    for block in result.content:
        if not isinstance(block, mcp.types.TextContent):
            continue
        try:
            payload = json.loads(block.text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            kind = cast(object, payload.get("kind"))
            if isinstance(kind, str):
                return kind
    return None
