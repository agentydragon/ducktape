"""MCP presentation layer for airlock.

Thin wrapper over ActionCoordinator that exposes backend tools via MCP with
an approval envelope. For each backend tool T in server S, exposes a wrapped
version named ``{s}_{t}`` that:
  - Adds required `justification: str` and `session_key: str` fields
  - On call: delegates to coordinator.propose_action() which handles the full
    lifecycle (predicate, decision, execution)
  - Translates coordinator events into MCP resource-updated notifications

Resources:
  resource://sessions/{session_key}/actions/{action_seq}  — action state
  resource://sessions/{session_key}/log_hwm               — last log entry_id
  resource://sessions/{session_key}/log/{entry_id}         — log entry
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.server.auth import require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import FunctionTool
from mako.template import Template
from mcp import types as mcp_types
from mcp.server.session import ServerSession
from pydantic import TypeAdapter
from pydantic.networks import AnyUrl

from airlock.coordinator import ActionCoordinator, ActionCreatedEvent, CoordinatorEvent
from airlock.models import Action, ActionKey, ActionStatus, BlockingWait, ToolCall, WaitMode, YieldAfterMs
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.urls import parse_any_url

logger = logging.getLogger(__name__)

PROPOSE_SCOPE = "propose"
READ_SCOPE = "read"

_INSTRUCTIONS_TEMPLATE = Path(__file__).parent / "instructions.mako"


_WAIT_MODE_SCHEMA: dict[str, Any] = TypeAdapter(WaitMode).json_schema()


def _wrap_tool_schema(original_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap a backend tool's input schema in an approval envelope.

    Produces:
      { input: <original_schema>, justification: str, session_key: str, wait_mode?: WaitMode }

    The nested `input` property holds the backend's original schema unchanged,
    avoiding any risk of name collisions with the approval fields.
    """
    return {
        "type": "object",
        "properties": {
            "input": copy.deepcopy(original_schema),
            "justification": {
                "type": "string",
                "description": "Explain why you need to run this action. Shown to the operator.",
            },
            "session_key": {
                "type": "string",
                "description": "Session key for result notifications. Injected by plugin.",
            },
            "wait_mode": _WAIT_MODE_SCHEMA,
        },
        "required": ["input", "justification", "session_key"],
    }


def _wait_mode_to_timeout(wait_mode: WaitMode) -> float:
    """Convert a WaitMode to a timeout in seconds.

    Returns float('inf') = wait forever, N = bounded (0 = immediate).
    """
    match wait_mode:
        case BlockingWait():
            return float("inf")
        case YieldAfterMs(timeout_ms=ms):
            return max(0, ms / 1000)


class AirlockServer(EnhancedFastMCP):
    """MCP server that wraps backend tools with an approval layer.

    This is a thin presentation layer. The ActionCoordinator owns the full
    action lifecycle (backends, predicate, execution, storage, events).
    """

    def __init__(
        self, *, coordinator: ActionCoordinator, public_base_url: str, default_wait_mode: WaitMode, **kwargs: Any
    ) -> None:
        super().__init__("Airlock", lifespan=self._lifespan, instructions="Airlock — initialising…", **kwargs)
        self._public_base_url = public_base_url
        self._default_wait_mode = default_wait_mode
        self.coordinator = coordinator
        self._subscriptions: defaultdict[ServerSession, set[str]] = defaultdict(set)

    # ── Lifespan ──────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, app: FastMCP) -> AsyncGenerator[None]:
        self.coordinator.add_listener(self._on_coordinator_event)

        # Coordinator is already entered (backends connected, pending actions
        # rehydrated) by the Starlette lifespan. Register tools/resources from
        # the connected backends.
        info = self.coordinator.backend_info
        for namespace, tools in info.tools.items():
            for tool in tools:
                self._register_wrapped_tool(namespace, tool)

        self.instructions = Template(filename=str(_INSTRUCTIONS_TEMPLATE)).render(
            backend_instructions=info.instructions, public_base_url=self._public_base_url
        )

        self._register_resources()
        self._register_tools()
        yield

    async def _on_coordinator_event(self, event: CoordinatorEvent) -> None:
        """Translate coordinator events into MCP resource-updated notifications."""
        key = event.action.key
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/actions/{key.action_seq}")
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/log_hwm")
        if isinstance(event, ActionCreatedEvent):
            await self.broadcast_resource_list_changed()

    def _register_resources(self) -> None:
        """Register resource templates and subscription tracking handlers."""

        @self.resource("resource://sessions/{session_key}/actions/{action_seq}")
        async def action_resource(session_key: str, action_seq: int) -> str:
            """Current state of a deferred action."""
            key = ActionKey(session_key=session_key, action_seq=action_seq)
            action = await self.coordinator.get_action(key)
            if action is None:
                raise ValueError(f"Action not found: {key}")
            return action.model_dump_json()

        @self.resource("resource://sessions/{session_key}/log_hwm")
        async def log_hwm_resource(session_key: str) -> str:
            """The entry_id of the last log entry for this session."""
            return str(await self.coordinator.get_log_hwm(session_key))

        @self.resource("resource://sessions/{session_key}/log/{entry_id}")
        async def log_entry_resource(session_key: str, entry_id: int) -> str:
            """A specific log entry."""
            entry = await self.coordinator.get_log_entry(session_key, entry_id)
            if entry is None:
                raise ValueError(f"Log entry not found: {session_key}/{entry_id}")
            return entry.model_dump_json()

        mcp_server = self._mcp_server

        @mcp_server.subscribe_resource()
        async def _handle_subscribe(uri: AnyUrl) -> None:
            self._subscriptions[mcp_server.request_context.session].add(str(uri))

        @mcp_server.unsubscribe_resource()
        async def _handle_unsubscribe(uri: AnyUrl) -> None:
            self._subscriptions[mcp_server.request_context.session].discard(str(uri))

    def _register_tools(self) -> None:
        """Register agent-facing MCP tools (propose + read only)."""

        @self.tool(auth=require_scopes(PROPOSE_SCOPE))
        async def list_actions(status: ActionStatus | None = None, limit: int = 100, offset: int = 0) -> list[Action]:
            """List queued/processed actions, optionally filtered by status."""
            return await self.coordinator.list_actions(status, limit=limit, offset=offset)

        @self.tool(auth=require_scopes(PROPOSE_SCOPE))
        async def withdraw_action(key: ActionKey) -> Action:
            """Withdraw a pending action before it is decided by an operator."""
            return await self.coordinator.withdraw(key)

    # ── Wrapped tool registration ─────────────────────────────────────────────

    def _register_wrapped_tool(self, namespace: MCPMountPrefix, backend_tool: mcp_types.Tool) -> None:
        """Register an approval-wrapped version of a backend tool under its namespace."""
        tool_name = backend_tool.name
        prefixed_name = build_mcp_function(namespace, tool_name)
        original_schema = backend_tool.inputSchema or {}
        wrapped_schema = _wrap_tool_schema(original_schema)

        description = backend_tool.description or ""

        async def _tool_handler(
            justification: str,
            session_key: str,
            input: dict[str, object] = {},  # noqa: B006
            wait_mode: WaitMode | None = None,
        ) -> Action:
            # Extract client identity from JWT claims.
            token = get_access_token()
            client_id: str | None = None
            subject: str | None = None
            if token is not None:
                client_id = token.claims.get("azp") or token.claims.get("client_id")
                subject = token.claims.get("sub")

            call = ToolCall(server_namespace=namespace, tool_name=tool_name, arguments=input)
            action, pipeline_task = await self.coordinator.propose_action(
                session_key=session_key, call=call, justification=justification, client_id=client_id, subject=subject
            )

            effective = wait_mode if wait_mode is not None else self._default_wait_mode
            effective_timeout = _wait_mode_to_timeout(effective)

            if effective_timeout > 0:
                with anyio.move_on_after(effective_timeout):
                    await asyncio.shield(pipeline_task)

            result = await self.coordinator.get_action(action.key)
            assert result is not None
            return result

        tool = FunctionTool(
            fn=_tool_handler,
            name=prefixed_name,
            description=description,
            parameters=wrapped_schema,
            auth=require_scopes(PROPOSE_SCOPE),
        )
        FastMCP.add_tool(self, tool)

    # ── MCP resource notifications ────────────────────────────────────────────

    async def _notify_subscribers(self, uri: str) -> None:
        """Send resource-updated notification only to sessions subscribed to the given URI."""
        uri_value = parse_any_url(uri)
        dead: list[ServerSession] = []
        for session, uris in self._subscriptions.items():
            if uri in uris:
                try:
                    await session.send_resource_updated(uri_value)
                except Exception:
                    logger.warning("send_resource_updated failed, removing session")
                    dead.append(session)
        for session in dead:
            del self._subscriptions[session]
