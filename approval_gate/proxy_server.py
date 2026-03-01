"""Core approval gate FastMCP server.

Wraps multiple backend MCP servers (connected via MCPServerTypes transport or
in-process FastMCP) with an approval layer. For each backend tool T in server S,
exposes a wrapped version named ``{s}_{t}`` that:
  - Adds required `justification: str` field
  - Adds optional `session_key: str | None` field (injected by plugin)
  - On call: checks predicate, stores pending action, returns action_id/URL
  - On operator approval: forwards original args to correct backend, updates state
  - Emits ResourceUpdated notifications so the OpenClaw plugin can inject results

Action resources are exposed at: resource://actions/{id}
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import AsyncGenerator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

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
    ActionState,
    ActionStatus,
    ApproveDecision,
    DenyDecision,
    DoneState,
    ExecutingState,
    OperatorDecision,
    PendingState,
    RejectedState,
    ToolCall,
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
      { input: <original_schema>, justification: str, session_key: str|null }

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
                "type": ["string", "null"],
                "description": "OpenClaw session key for result notifications. Injected by plugin.",
                "default": None,
            },
        },
        "required": ["input", "justification"],
    }


def _require_action(action: Action | None, action_id: uuid.UUID) -> Action:
    if action is None:
        raise ValueError(f"Action not found: {action_id}")
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
        # Instructions are set after backend connection in lifespan; placeholder here.
        # Pass self._lifespan explicitly so FastMCP stores our bound method as the instance
        # attribute rather than default_lifespan — no del hack needed.
        super().__init__(
            "Approval Gate", lifespan=self._lifespan, instructions="Approval gate — initialising…", **kwargs
        )
        self._backend_specs = backends
        self._db_path = db_path
        self._storage: ActionStorage | None = None
        self._predicate = predicate
        self._public_base_url = public_base_url
        # Populated in lifespan
        self._backend_clients: dict[MCPMountPrefix, Client] = {}
        # Holds references to fire-and-forget background tasks to prevent GC cancellation.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ── Lifespan: connect backends, register wrapped tools + resources ────────

    @asynccontextmanager
    async def _lifespan(self, app: FastMCP) -> AsyncGenerator[None]:
        # Initialise storage here so the server can be constructed synchronously.
        self._storage = await ActionStorage.initialize(self._db_path)

        async with AsyncExitStack() as stack:
            backend_instructions: dict[str, str | None] = {}

            for namespace, spec in self._backend_specs.items():
                # mypy can't match to_transport()'s union return against Client's overloaded __init__
                client = Client(spec) if isinstance(spec, FastMCP) else Client(spec.to_transport())  # type: ignore[arg-type]

                logger.info("[_lifespan] connecting to backend %s: %s", namespace, spec)
                await stack.enter_async_context(client)
                logger.info("[_lifespan] backend %s connected", namespace)
                self._backend_clients[namespace] = client

                init = client.initialize_result
                backend_instructions[namespace] = init.instructions if init else None

                # Discover and register wrapped tools from this backend
                backend_tools = await client.list_tools()
                for tool in backend_tools:
                    self._register_wrapped_tool(namespace, tool)

            # Render instructions using Mako template
            tmpl = Template(filename=str(_INSTRUCTIONS_TEMPLATE))
            rendered_instructions = tmpl.render(
                backend_instructions=backend_instructions, public_base_url=self._public_base_url
            )
            # FastMCP.instructions is a property that writes through to _mcp_server.instructions
            self.instructions = rendered_instructions

            # Register a resource template for individual action state
            @self.resource("resource://actions/{action_id}")
            async def action_resource(action_id: uuid.UUID) -> str:
                """Current state of a deferred action."""
                action = await self._req_storage.get(action_id)
                if action is None:
                    raise ValueError(f"Action not found: {action_id}")
                return action.model_dump_json()

            # ── Operator MCP tools ────────────────────────────────────────────
            # Gated by require_scopes("operator"); bypassed for in-process clients
            # (stdio/memory transport), which allows tests to call these freely.

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def list_actions(status: ActionStatus | None = None, limit: int = 100) -> list[Action]:
                """List queued/processed actions, optionally filtered by status."""
                return await self._req_storage.list_actions(status, limit=limit)

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def approve_action(action_id: uuid.UUID) -> Action:
                """Approve a pending action, executing it against the backend."""
                return await self.decide(action_id, ApproveDecision())

            @self.tool(auth=require_scopes(OPERATOR_SCOPE))
            async def reject_action(action_id: uuid.UUID, reason: str | None = None) -> Action:
                """Reject a pending action without executing it."""
                return await self.decide(action_id, DenyDecision(reason=reason))

            @self.tool(auth=require_scopes(AGENT_SCOPE))
            async def withdraw_action(action_id: uuid.UUID) -> Action:
                """Withdraw a pending action before it is decided by an operator."""
                return await self.withdraw(action_id)

            yield

            # Drain in-flight background tasks (e.g. _execute_and_finish) before
            # disconnecting from the backend so they don't outlive the backend connection
            # and leak into the next test or request cycle.
            if self._background_tasks:
                logger.info("[_lifespan] draining %d background task(s)", len(self._background_tasks))
                await asyncio.gather(*self._background_tasks, return_exceptions=True)

        self._backend_clients.clear()

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
            input: dict[str, object] = {},  # noqa: B006
            session_key: str | None = None,
        ) -> uuid.UUID:
            call = ToolCall(server_namespace=namespace, tool_name=tool_name, arguments=input)
            action_id = uuid.uuid4()
            await self._req_storage.create(
                action_id=action_id, call=call, justification=justification, session_key=session_key
            )
            await self.broadcast_resource_list_changed()
            self._spawn(self._apply_predicate(action_id, namespace, tool_name, input))
            return action_id

        # Construct a FunctionTool with the wrapped schema (bypasses schema inference
        # from type hints — we provide the exact JSON schema from the backend tool).
        # Call FastMCP.add_tool directly to skip OpenAIStrictModeMixin validation:
        # proxy tools embed arbitrary backend schemas that may not satisfy OpenAI strict
        # mode (e.g. missing additionalProperties:false, optional fields not in required).
        tool = FunctionTool(
            fn=_tool_handler,
            name=prefixed_name,
            description=description,
            parameters=wrapped_schema,
            auth=require_scopes(AGENT_SCOPE),
        )
        FastMCP.add_tool(self, tool)

    async def _apply_predicate(
        self, action_id: uuid.UUID, namespace: MCPMountPrefix, tool_name: str, input: dict[str, object]
    ) -> None:
        """Evaluate the predicate and auto-decide if not NeedsHumanDecision."""
        decision = call_predicate(self._predicate, namespace, tool_name, input)
        match decision:
            case Approved():
                await self.decide(action_id, ApproveDecision())
            case Denied(reason=reason):
                await self.decide(action_id, DenyDecision(reason=reason or "automatically denied"))
            case NeedsHumanDecision():
                logger.info("queued action id=%s server=%s tool=%s", action_id, namespace, tool_name)

    # ── Operator / agent decisions ────────────────────────────────────────────

    async def decide(self, action_id: uuid.UUID, decision: OperatorDecision) -> Action:
        """Apply an operator decision (approve or deny) to a pending action.

        Raises ValueError if the action does not exist or is not pending.
        """
        action = _require_action(await self._req_storage.get(action_id), action_id)
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {action_id} is not pending ({action.state.status=})")

        match decision:
            case ApproveDecision():
                action = await self._update_and_notify(action_id, ExecutingState())
                self._spawn(self._execute_and_finish(action_id, action))
                return action
            case DenyDecision(reason=reason):
                return await self._update_and_notify(action_id, RejectedState(reason=reason))

    async def withdraw(self, action_id: uuid.UUID) -> Action:
        """Agent-initiated withdrawal of a pending action.

        Raises ValueError if the action does not exist or is not pending.
        """
        action = _require_action(await self._req_storage.get(action_id), action_id)
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {action_id} is not pending ({action.state.status=})")
        return await self._update_and_notify(action_id, WithdrawnState())

    async def _execute_and_finish(self, action_id: uuid.UUID, action: Action) -> None:
        """Execute the backend call and update state to done."""
        outcome = await self._execute_backend_call(action)
        await self._update_and_notify(action_id, DoneState(outcome=outcome))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a coroutine as a background task, keeping a reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _update_and_notify(self, action_id: uuid.UUID, new_state: ActionState) -> Action:
        """Update action state in storage and broadcast a resource-updated notification."""
        action = _require_action(await self._req_storage.update_state(action_id, new_state), action_id)
        await self.broadcast_resource_updated(f"resource://actions/{action_id}")
        return action

    # ── Internal backend call ─────────────────────────────────────────────────

    async def _execute_backend_call(self, action: Action) -> mcp_types.CallToolResult:
        """Forward the tool call to the correct backend and return the raw MCP CallToolResult."""
        namespace = MCPMountPrefix(action.call.server_namespace)
        client = self._backend_clients.get(namespace)
        if client is None:
            raise RuntimeError(f"backend client not connected for namespace: {namespace!r}")
        return await client.call_tool_mcp(action.call.tool_name, action.call.arguments)
