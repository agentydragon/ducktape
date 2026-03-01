"""Core approval gate FastMCP server.

Wraps multiple backend MCP servers (connected via MCPServerTypes transport or
in-process FastMCP) with an approval layer. For each backend tool T in server S,
exposes a wrapped version named ``{s}_{t}`` that:
  - Adds required `justification: str` and `session_key: str` fields
  - On call: checks predicate, stores pending action, returns ActionKey
  - On operator approval: forwards original args to correct backend, updates state
  - Appends to per-session append-only event log and broadcasts HWM updates

Resources:
  resource://sessions/{session_key}/actions/{action_seq}  — action state
  resource://sessions/{session_key}/log_hwm               — last log entry_id
  resource://sessions/{session_key}/log/{entry_id}         — log entry
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.mcp_config import MCPServerTypes
from fastmcp.server.auth import require_scopes
from fastmcp.tools.tool import FunctionTool
from mako.template import Template
from mcp import types as mcp_types

from approval_gate.mcp_auth import AGENT_SCOPE, OPERATOR_SCOPE
from approval_gate.models import (
    Action,
    ActionKey,
    ActionReceivedDetail,
    ActionState,
    ActionStatus,
    ApproveDecision,
    DeniedDetail,
    DenyDecision,
    DoneState,
    ExecutingState,
    ExecutionFinishedDetail,
    ExecutionStartedDetail,
    LogEventDetail,
    OperatorDecision,
    PendingState,
    RejectedState,
    ToolCall,
    WithdrawnDetail,
    WithdrawnState,
)
from approval_gate.predicates import Approved, Denied, NeedsHumanDecision, PredicateFn, call_predicate
from approval_gate.storage import ActionStorage
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

logger = logging.getLogger(__name__)

_INSTRUCTIONS_TEMPLATE = Path(__file__).parent / "instructions.mako"


def _wrap_tool_schema(original_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap a backend tool's input schema in an approval envelope.

    Produces:
      { input: <original_schema>, justification: str, session_key: str }

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
        },
        "required": ["input", "justification", "session_key"],
    }


def _require_action(action: Action | None, key: ActionKey) -> Action:
    if action is None:
        raise ValueError(f"Action not found: {key.session_key}/{key.action_seq}")
    return action


def _require_pending(action: Action) -> Action:
    if not isinstance(action.state, PendingState):
        raise ValueError(
            f"Action {action.key.session_key}/{action.key.action_seq} is not pending ({action.state.status=})"
        )
    return action


class ApprovalGateServer(EnhancedFastMCP):
    """MCP server that wraps multiple backend MCP servers with an approval layer."""

    def __init__(
        self,
        *,
        backends: dict[MCPMountPrefix, MCPServerTypes | FastMCP],
        db_path: Path,
        predicate: PredicateFn,
        public_base_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            "Approval Gate", lifespan=self._lifespan, instructions="Approval gate — initialising…", **kwargs
        )
        self._backend_specs = backends
        self._db_path = db_path
        self._storage: ActionStorage | None = None
        self._predicate = predicate
        self._public_base_url = public_base_url
        self._backend_clients: dict[MCPMountPrefix, Client] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ── Lifespan: connect backends, register wrapped tools + resources ────────

    @asynccontextmanager
    async def _lifespan(self, app: FastMCP) -> AsyncGenerator[None]:
        self._storage = await ActionStorage.initialize(self._db_path)

        # Manual stack management so cleanup runs inside the shielded scope below.
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            backend_instructions: dict[str, str | None] = {}

            for namespace, spec in self._backend_specs.items():
                client = Client(spec) if isinstance(spec, FastMCP) else Client(spec.to_transport())  # type: ignore[arg-type]

                logger.info("[_lifespan] connecting to backend %s: %s", namespace, spec)
                await stack.enter_async_context(client)
                logger.info("[_lifespan] backend %s connected", namespace)
                self._backend_clients[namespace] = client

                init = client.initialize_result
                backend_instructions[namespace] = init.instructions if init else None

                backend_tools = await client.list_tools()
                for tool in backend_tools:
                    self._register_wrapped_tool(namespace, tool)

            tmpl = Template(filename=str(_INSTRUCTIONS_TEMPLATE))
            rendered_instructions = tmpl.render(
                backend_instructions=backend_instructions, public_base_url=self._public_base_url
            )
            self.instructions = rendered_instructions

            # ── Resource templates ────────────────────────────────────────────

            @self.resource("resource://sessions/{session_key}/actions/{action_seq}")
            async def action_resource(session_key: str, action_seq: int) -> str:
                """Current state of a deferred action."""
                key = ActionKey(session_key=session_key, action_seq=action_seq)
                action = await self._req_storage.get_action(key)
                if action is None:
                    raise ValueError(f"Action not found: {session_key}/{action_seq}")
                return action.model_dump_json()

            @self.resource("resource://sessions/{session_key}/log_hwm")
            async def log_hwm_resource(session_key: str) -> str:
                """The entry_id of the last log entry for this session."""
                hwm = await self._req_storage.get_log_hwm(session_key)
                return str(hwm)

            @self.resource("resource://sessions/{session_key}/log/{entry_id}")
            async def log_entry_resource(session_key: str, entry_id: int) -> str:
                """A specific log entry."""
                entry = await self._req_storage.get_log_entry(session_key, entry_id)
                if entry is None:
                    raise ValueError(f"Log entry not found: {session_key}/{entry_id}")
                return entry.model_dump_json()

            # Enable resource subscriptions
            @self._mcp_server.subscribe_resource()
            async def _handle_subscribe(_uri: str) -> None:
                return None

            @self._mcp_server.unsubscribe_resource()
            async def _handle_unsubscribe(_uri: str) -> None:
                return None

            # ── Operator MCP tools ────────────────────────────────────────────

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def list_actions(status: ActionStatus | None = None, limit: int = 100) -> list[Action]:
                """List queued/processed actions, optionally filtered by status."""
                return await self._req_storage.list_actions(status, limit=limit)

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def approve_action(session_key: str, action_seq: int) -> Action:
                """Approve a pending action, executing it against the backend."""
                return await self.decide(ActionKey(session_key=session_key, action_seq=action_seq), ApproveDecision())

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def reject_action(session_key: str, action_seq: int, reason: str | None = None) -> Action:
                """Reject a pending action without executing it."""
                return await self.decide(
                    ActionKey(session_key=session_key, action_seq=action_seq), DenyDecision(reason=reason)
                )

            @self.tool(auth=require_scopes(AGENT_SCOPE))
            async def withdraw_action(session_key: str, action_seq: int) -> Action:
                """Withdraw a pending action before it is decided by an operator."""
                return await self.withdraw(ActionKey(session_key=session_key, action_seq=action_seq))

            yield
        finally:
            # Shield cleanup from FastMCP memory transport's cancel scope.
            # FastMCPTransport.connect_session() calls tg.cancel_scope.cancel()
            # on client disconnect, which would cancel our async cleanup
            # (backend client close, storage dispose), leaving orphaned resources.
            with anyio.CancelScope(shield=True):
                if self._background_tasks:
                    logger.info("[_lifespan] draining %d background task(s)", len(self._background_tasks))
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
                await stack.aclose()
                self._backend_clients.clear()
                if self._storage is not None:
                    await self._storage.close()

    @property
    def _req_storage(self) -> ActionStorage:
        if self._storage is None:
            raise RuntimeError("storage not initialised — gate not started")
        return self._storage

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
        ) -> ActionKey:
            call = ToolCall(server_namespace=namespace, tool_name=tool_name, arguments=input)
            action_seq = await self._req_storage.next_action_seq(session_key)
            key = ActionKey(session_key=session_key, action_seq=action_seq)
            await self._req_storage.create_action(key=key, call=call, justification=justification)
            await self._append_log_and_notify(key, ActionReceivedDetail())
            await self.broadcast_resource_list_changed()
            self._spawn(self._apply_predicate(key, namespace, tool_name, input))
            return key

        tool = FunctionTool(
            fn=_tool_handler,
            name=prefixed_name,
            description=description,
            parameters=wrapped_schema,
            auth=require_scopes(AGENT_SCOPE),
        )
        FastMCP.add_tool(self, tool)

    async def _apply_predicate(
        self, key: ActionKey, namespace: MCPMountPrefix, tool_name: str, input: dict[str, object]
    ) -> None:
        """Evaluate the predicate and auto-decide if not NeedsHumanDecision."""
        decision = call_predicate(self._predicate, namespace, tool_name, input)
        match decision:
            case Approved():
                await self.decide(key, ApproveDecision())
            case Denied(reason=reason):
                await self.decide(key, DenyDecision(reason=reason or "automatically denied"))
            case NeedsHumanDecision():
                logger.info(
                    "queued action %s/%d server=%s tool=%s", key.session_key, key.action_seq, namespace, tool_name
                )

    # ── Event log helpers ────────────────────────────────────────────────────

    async def _append_log_and_notify(self, key: ActionKey, detail: LogEventDetail) -> None:
        """Append a log entry and broadcast action + HWM resource updates."""
        await self._req_storage.append_log_entry(session_key=key.session_key, action_seq=key.action_seq, detail=detail)
        await self.broadcast_resource_updated(f"resource://sessions/{key.session_key}/actions/{key.action_seq}")
        await self.broadcast_resource_updated(f"resource://sessions/{key.session_key}/log_hwm")

    # ── Operator / agent decisions ────────────────────────────────────────────

    async def _get_pending_action(self, key: ActionKey) -> Action:
        """Fetch an action and verify it exists and is pending."""
        return _require_pending(_require_action(await self._req_storage.get_action(key), key))

    async def decide(self, key: ActionKey, decision: OperatorDecision) -> Action:
        """Apply an operator decision (approve or deny) to a pending action.

        Raises ValueError if the action does not exist or is not pending.
        """
        await self._get_pending_action(key)
        match decision:
            case ApproveDecision():
                action = await self._update_and_notify(key, ExecutingState(), ExecutionStartedDetail())
                self._spawn(self._execute_and_finish(key, action))
                return action
            case DenyDecision(reason=reason):
                return await self._update_and_notify(key, RejectedState(reason=reason), DeniedDetail(reason=reason))

    async def withdraw(self, key: ActionKey) -> Action:
        """Agent-initiated withdrawal of a pending action.

        Raises ValueError if the action does not exist or is not pending.
        """
        await self._get_pending_action(key)
        return await self._update_and_notify(key, WithdrawnState(), WithdrawnDetail())

    async def _execute_and_finish(self, key: ActionKey, action: Action) -> None:
        """Execute the backend call and update state to done."""
        outcome = await self._execute_backend_call(action)
        await self._update_and_notify(key, DoneState(outcome=outcome), ExecutionFinishedDetail(outcome=outcome))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a coroutine as a background task, keeping a reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _update_and_notify(self, key: ActionKey, new_state: ActionState, detail: LogEventDetail) -> Action:
        """Update action state in storage, append log entry, and broadcast notifications."""
        action = _require_action(await self._req_storage.update_state(key, new_state), key)
        await self._append_log_and_notify(key, detail)
        return action

    # ── Internal backend call ─────────────────────────────────────────────────

    async def _execute_backend_call(self, action: Action) -> mcp_types.CallToolResult:
        """Forward the tool call to the correct backend and return the raw MCP CallToolResult."""
        namespace = MCPMountPrefix(action.call.server_namespace)
        client = self._backend_clients.get(namespace)
        if client is None:
            raise RuntimeError(f"backend client not connected for namespace: {namespace!r}")
        return await client.call_tool_mcp(action.call.tool_name, action.call.arguments)
