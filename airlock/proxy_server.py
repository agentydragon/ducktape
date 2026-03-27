"""Core airlock FastMCP server.

Wraps multiple backend MCP servers (connected via MCPServerTypes transport or
in-process FastMCP) with an approval layer. For each backend tool T in server S,
exposes a wrapped version named ``{s}_{t}`` that:
  - Adds required `justification: str` and `session_key: str` fields
  - On call: checks predicate, stores pending action, returns ActionKey
  - On operator approval: forwards original args to correct backend, updates state
  - Appends to per-session append-only event log and notifies subscribed sessions

Resources:
  resource://sessions/{session_key}/actions/{action_seq}  — action state
  resource://sessions/{session_key}/log_hwm               — last log entry_id
  resource://sessions/{session_key}/log/{entry_id}         — log entry
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Coroutine, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
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
from mcp.server.session import ServerSession
from pydantic import TypeAdapter
from pydantic.networks import AnyUrl

from airlock.models import (
    Action,
    ActionKey,
    ActionReceivedDetail,
    ActionState,
    ActionStatus,
    ApproveDecision,
    BlockingWait,
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
    WaitMode,
    WithdrawnDetail,
    WithdrawnState,
    YieldAfterMs,
)
from airlock.predicates import Approved, Denied, NeedsHumanDecision, PredicateFn, call_predicate
from airlock.storage import ActionStorage
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.urls import parse_any_url

logger = logging.getLogger(__name__)

PROPOSE_SCOPE = "propose"
DECIDE_SCOPE = "decide"
READ_SCOPE = "read"

_INSTRUCTIONS_TEMPLATE = Path(__file__).parent / "instructions.mako"


_WAIT_MODE_SCHEMA: dict[str, Any] = TypeAdapter(WaitMode).json_schema()
_DEFAULT_WAIT_MODE = YieldAfterMs(timeout_ms=0)


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
    """MCP server that wraps multiple backend MCP servers with an approval layer."""

    def __init__(
        self,
        *,
        backends: Mapping[MCPMountPrefix, MCPServerTypes | FastMCP],
        db_path: Path,
        predicate: PredicateFn,
        public_base_url: str,
        default_wait_mode: WaitMode = _DEFAULT_WAIT_MODE,
        **kwargs: Any,
    ) -> None:
        super().__init__("Airlock", lifespan=self._lifespan, instructions="Airlock — initialising…", **kwargs)
        self._backend_specs = backends
        self._db_path = db_path
        self._storage: ActionStorage | None = None
        self._predicate = predicate
        self._public_base_url = public_base_url
        self._default_wait_mode = default_wait_mode
        self._backend_clients: dict[MCPMountPrefix, Client] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._subscriptions: defaultdict[ServerSession, set[str]] = defaultdict(set)
        # Keyed by ActionKey while action is parked awaiting a human decision.
        self._pending_decisions: dict[ActionKey, asyncio.Future[OperatorDecision]] = {}
        self._sse_listeners: set[asyncio.Queue[dict[str, Any]]] = set()

    # ── SSE listener management ───────────────────────────────────────────────

    def add_sse_listener(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._sse_listeners.add(queue)

    def remove_sse_listener(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._sse_listeners.discard(queue)

    def _broadcast_sse(self, event: dict[str, Any]) -> None:
        for q in list(self._sse_listeners):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    # ── Lifespan ──────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, app: FastMCP) -> AsyncGenerator[None]:
        self._storage = await ActionStorage.initialize(self._db_path)

        # Manual stack management so cleanup runs inside the shielded scope below.
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await self._connect_backends(stack)
            await self._rehydrate_pending_actions()
            self._register_resources()
            self._register_tools()
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

    async def _rehydrate_pending_actions(self) -> None:
        """Recreate in-memory decision futures for PENDING actions that survived a server restart.

        Without this, decide() would fail for any PENDING action from a prior run because
        _pending_decisions only lives in memory and is empty on startup.
        """
        pending = await self._req_storage.list_actions(ActionStatus.PENDING)
        for action in pending:
            namespace = MCPMountPrefix(action.call.server_namespace)
            self._spawn(self._await_human_decision(action.key, namespace, action.call.tool_name, action.call.arguments))

    async def _connect_backends(self, stack: AsyncExitStack) -> None:
        """Connect to all backend servers, register wrapped tools, and render instructions."""
        backend_instructions: dict[str, str | None] = {}
        for namespace, spec in self._backend_specs.items():
            client = Client(spec) if isinstance(spec, FastMCP) else Client(spec.to_transport())  # type: ignore[arg-type]

            logger.info("[_connect_backends] connecting to %s: %s", namespace, spec)
            await stack.enter_async_context(client)
            logger.info("[_connect_backends] %s connected", namespace)
            self._backend_clients[namespace] = client

            init = client.initialize_result
            backend_instructions[namespace] = init.instructions if init else None

            for tool in await client.list_tools():
                self._register_wrapped_tool(namespace, tool)

        self.instructions = Template(filename=str(_INSTRUCTIONS_TEMPLATE)).render(
            backend_instructions=backend_instructions, public_base_url=self._public_base_url
        )

    def _register_resources(self) -> None:
        """Register resource templates and subscription tracking handlers."""

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
            return str(await self._req_storage.get_log_hwm(session_key))

        @self.resource("resource://sessions/{session_key}/log/{entry_id}")
        async def log_entry_resource(session_key: str, entry_id: int) -> str:
            """A specific log entry."""
            entry = await self._req_storage.get_log_entry(session_key, entry_id)
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
        """Register operator and agent MCP tools."""

        @self.tool(auth=require_scopes(READ_SCOPE))
        async def list_actions(status: ActionStatus | None = None, limit: int = 100, offset: int = 0) -> list[Action]:
            """List queued/processed actions, optionally filtered by status."""
            return await self._req_storage.list_actions(status, limit=limit, offset=offset)

        @self.tool(auth=require_scopes(DECIDE_SCOPE))
        async def approve_action(key: ActionKey) -> None:
            """Approve a pending action, executing it against the backend."""
            await self.decide(key, ApproveDecision())

        @self.tool(auth=require_scopes(DECIDE_SCOPE))
        async def reject_action(key: ActionKey, reason: str | None = None) -> None:
            """Reject a pending action without executing it."""
            await self.decide(key, DenyDecision(reason=reason))

        @self.tool(auth=require_scopes(PROPOSE_SCOPE))
        async def withdraw_action(key: ActionKey) -> Action:
            """Withdraw a pending action before it is decided by an operator."""
            return await self.withdraw(key)

    @property
    def _req_storage(self) -> ActionStorage:
        if self._storage is None:
            raise RuntimeError("storage not initialised — gate not started")
        return self._storage

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
            call = ToolCall(server_namespace=namespace, tool_name=tool_name, arguments=input)
            action = await self._req_storage.create_action(
                session_key=session_key, call=call, justification=justification
            )
            key = action.key
            await self._append_log_and_notify(key, ActionReceivedDetail())
            await self.broadcast_resource_list_changed()
            self._broadcast_sse({"type": "actions_changed"})

            effective = wait_mode if wait_mode is not None else self._default_wait_mode
            effective_timeout = _wait_mode_to_timeout(effective)

            pipeline = self._spawn(self._run_action_pipeline(key, namespace, tool_name, input))

            if effective_timeout > 0:
                with anyio.move_on_after(effective_timeout):
                    await asyncio.shield(pipeline)

            result = await self._req_storage.get_action(key)
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

    # ── Action pipeline ───────────────────────────────────────────────────────

    async def _run_action_pipeline(
        self, key: ActionKey, namespace: MCPMountPrefix, tool_name: str, input: dict[str, object]
    ) -> None:
        """Walk one action through its full lifecycle: predicate → decision → execution."""
        match call_predicate(self._predicate, namespace, tool_name, input):
            case Approved():
                await self._apply_decision(key, namespace, tool_name, input, ApproveDecision())
            case Denied(reason=reason):
                await self._apply_decision(
                    key, namespace, tool_name, input, DenyDecision(reason=reason or "automatically denied")
                )
            case NeedsHumanDecision():
                logger.info(
                    "queued action %s/%d server=%s tool=%s", key.session_key, key.action_seq, namespace, tool_name
                )
                await self._await_human_decision(key, namespace, tool_name, input)

    async def _await_human_decision(
        self, key: ActionKey, namespace: MCPMountPrefix, tool_name: str, input: dict[str, object]
    ) -> None:
        """Park until a human decision arrives via decide(), then apply it."""
        fut: asyncio.Future[OperatorDecision] = asyncio.get_running_loop().create_future()
        self._pending_decisions[key] = fut
        try:
            decision = await fut
        except asyncio.CancelledError:
            return  # withdrawn externally; state already updated by withdraw()
        finally:
            self._pending_decisions.pop(key, None)
        await self._apply_decision(key, namespace, tool_name, input, decision)

    async def _apply_decision(
        self,
        key: ActionKey,
        namespace: MCPMountPrefix,
        tool_name: str,
        input: dict[str, object],
        decision: OperatorDecision,
    ) -> None:
        """Apply a decision: transition state and execute backend if approved."""
        match decision:
            case ApproveDecision():
                started_at = datetime.now(tz=UTC)
                await self._update_and_notify(key, ExecutingState(), ExecutionStartedDetail(started_at=started_at))
                client = self._backend_clients.get(namespace)
                if client is None:
                    raise RuntimeError(f"backend client not connected for namespace: {namespace!r}")
                outcome = await client.call_tool_mcp(tool_name, input)
                await self._update_and_notify(key, DoneState(outcome=outcome), ExecutionFinishedDetail(outcome=outcome))
            case DenyDecision(reason=reason):
                await self._update_and_notify(key, RejectedState(reason=reason), DeniedDetail(reason=reason))

    # ── Event log + notification helpers ──────────────────────────────────────

    async def _append_log_and_notify(self, key: ActionKey, detail: LogEventDetail) -> None:
        """Append a log entry and notify subscribed sessions of action + HWM changes."""
        await self._req_storage.append_log_entry(session_key=key.session_key, action_seq=key.action_seq, detail=detail)
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/actions/{key.action_seq}")
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/log_hwm")

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

    # ── Operator / agent decisions ────────────────────────────────────────────

    async def decide(self, key: ActionKey, decision: OperatorDecision) -> None:
        """Inject an operator decision for an action awaiting human input.

        Raises ValueError if the action does not exist, is not pending, or is not
        awaiting a human decision (e.g. still processing an auto-predicate).
        """
        action = await self._req_storage.get_action(key)
        if action is None:
            raise ValueError(f"Action not found: {key.session_key}/{key.action_seq}")
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {key.session_key}/{key.action_seq} is not pending ({action.state.status=})")
        fut = self._pending_decisions.get(key)
        if fut is None or fut.done():
            raise ValueError(f"Action {key.session_key}/{key.action_seq} is not awaiting a human decision")
        fut.set_result(decision)

    async def withdraw(self, key: ActionKey) -> Action:
        """Agent-initiated withdrawal of a pending action.

        Raises ValueError if the action does not exist or is not pending.
        """
        action = await self._req_storage.get_action(key)
        if action is None:
            raise ValueError(f"Action not found: {key.session_key}/{key.action_seq}")
        if not isinstance(action.state, PendingState):
            raise ValueError(f"Action {key.session_key}/{key.action_seq} is not pending ({action.state.status=})")
        result = await self._update_and_notify(key, WithdrawnState(), WithdrawnDetail())
        fut = self._pending_decisions.pop(key, None)
        if fut is not None and not fut.done():
            fut.cancel()
        return result

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Schedule a coroutine as a background task, keeping a reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _update_and_notify(self, key: ActionKey, new_state: ActionState, detail: LogEventDetail) -> Action:
        """Update action state and append log entry atomically, then notify subscribers."""
        action, _ = await self._req_storage.update_state_and_log(key, new_state, detail)
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/actions/{key.action_seq}")
        await self._notify_subscribers(f"resource://sessions/{key.session_key}/log_hwm")
        self._broadcast_sse({"type": "action_updated", "session_key": key.session_key, "action_seq": key.action_seq})
        return action
