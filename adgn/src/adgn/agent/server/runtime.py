from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import UTC, datetime
import logging
from typing import Any
import uuid

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from adgn.agent.agent import MiniCodex
from adgn.agent.approvals import ApprovalHub, ApprovalPolicyEngine
from adgn.agent.handler import AssistantText, BaseHandler, ToolCall, ToolCallOutput, UserText
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import PersistenceRunStatus
from adgn.agent.persist.handler import RunPersistenceHandler
from adgn.agent.server.bus import ServerBus, UiEndTurn, UiMessage
from adgn.agent.types import AgentID
from adgn.agent.server.protocol import (
    ApprovalBrief,
    ApprovalPolicyInfo,
    Envelope,
    ErrorCode,
    ErrorEvt,
    FunctionCallOutput,
    ProposalInfo,
    RunState,
    RunStatus as UiRunStatus,
    RunStatusEvt,
    ServerMessage,
    SessionState,
    Snapshot,
    SnapshotDetails,
    ToolCallEvt as UiToolCall,
    UiEndTurnEvt,
    UiMessageEvt,
    UiMessagePayload,
    UiStateUpdated,
    UserText as UiUserText,
)
from adgn.agent.server.reducer import reduce_ui_state
from adgn.agent.server.state import UiState, new_state
from adgn.agent.server.status_shared import RunPhase, determine_run_phase
from adgn.mcp._shared.calltool import convert_fastmcp_result

logger = logging.getLogger(__name__)


class ConnectionManager(BaseHandler):
    def __init__(self) -> None:
        self._clients: dict[int, tuple[WebSocket, asyncio.Queue[Any | None], asyncio.Task]] = {}
        self._session: AgentSession | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._event_id: int = 0
        self._session_id: str = str(uuid.uuid4())
        # Optional: session state change notifier for MCP resource updates
        self._session_state_notifier: Callable[[], None] | None = None

    async def connect(self, ws: WebSocket) -> None:
        # Accept only if not already accepted by the route handler
        if ws.application_state is not WebSocketState.CONNECTED:
            try:
                await ws.accept()
            except Exception as e:
                # If a close has already been sent or the client disconnected, log and propagate
                logger.error("WebSocket accept failed", extra={"error": str(e)}, exc_info=True)
                raise
        # If still not connected after best-effort accept, do not register
        if ws.application_state is not WebSocketState.CONNECTED:
            return
        q: asyncio.Queue[Any | None] = asyncio.Queue()
        client_id = id(ws)
        task = asyncio.create_task(self._sender_loop(client_id, ws, q))
        self._clients[client_id] = (ws, q, task)

    async def disconnect(self, ws: WebSocket) -> None:
        cid = id(ws)
        conn = self._clients.pop(cid, None)
        if conn:
            _ws, q, task = conn
            # Graceful shutdown: signal sender loop to exit and await task
            q.put_nowait(None)
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _sender_loop(self, client_id: int, ws: WebSocket, queue: asyncio.Queue[Any | None]) -> None:
        while True:
            payload = await queue.get()
            if payload is None:
                break
            # If the websocket isn't in a connected state, stop sending
            if ws.application_state is not WebSocketState.CONNECTED:
                break
            try:
                logger.info(
                    "manager: sending to client",
                    extra={"client_id": client_id, "kind": payload.get("type") or payload.get("kind")},
                )
                await ws.send_json(payload)
            except Exception as e:
                logger.error(
                    "ws send_json failed; stopping sender loop",
                    extra={"client_id": client_id, "error": str(e)},
                    exc_info=True,
                )
                # Break sender loop - connection is broken
                break

    def _next_event_id(self) -> int:
        self._event_id += 1
        return self._event_id

    async def send_json(self, payload: ServerMessage) -> None:
        envelope = Envelope(
            session_id=self._session_id,
            event_id=self._next_event_id(),
            event_at=datetime.now(UTC),
            payload=payload,
        )
        dumped = envelope.model_dump(mode="json")
        for _ws, q, _task in list(self._clients.values()):
            q.put_nowait(dumped)

    async def _send_and_reduce(self, payload: ServerMessage) -> None:
        await self.send_payload(payload)
        assert self._session is not None
        await self._session._apply_ui_event(payload)
        # Run status mirroring removed with WebSocket status broadcasts

    async def _emit_ui_bus_messages(self) -> None:
        assert self._session is not None
        if self._session.ui_bus is None:
            return
        bus = self._session.ui_bus
        for item in bus.drain_messages():
            if isinstance(item, UiMessage):
                await self._send_and_reduce(
                    UiMessageEvt(message=UiMessagePayload(mime=item.mime, content=item.content))
                )
            elif isinstance(item, UiEndTurn):
                await self._send_and_reduce(UiEndTurnEvt())

    async def send_payload(self, payload: ServerMessage) -> None:
        await self.send_json(payload)

    def set_session(self, session: AgentSession) -> None:
        self._session = session

    def _spawn(self, coro: Awaitable[None]) -> None:
        t: asyncio.Task[Any] = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def flush(self) -> None:
        if not self._bg_tasks:
            return
        tasks = list(self._bg_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

    def on_user_text_event(self, evt: UserText) -> None:
        ut = UiUserText(text=evt.text)
        self._spawn(self._send_and_reduce(ut))
        # Notify MCP bridge of session state change
        if self._session_state_notifier is not None:
            self._session_state_notifier()

    async def _send_direct_all(self, payload: ServerMessage) -> None:
        envelope = Envelope(
            session_id=self._session_id,
            event_id=self._next_event_id(),
            event_at=datetime.now(UTC),
            payload=payload,
        )
        dumped = envelope.model_dump(mode="json")
        for ws, _q, _task in list(self._clients.values()):
            await ws.send_json(dumped)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        raise RuntimeError("assistant_text not allowed in UI mode; use ui.send_message tool instead")

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._spawn(self._send_and_reduce(UiToolCall(tool_call=evt)))
        # Notify MCP bridge of session state change
        if self._session_state_notifier is not None:
            self._session_state_notifier()

    # No per-tool interception; Policy Gateway middleware emits approval_pending via notifier

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        # FastMCP CallToolResult is not a Pydantic model; project minimal fields
        fco = FunctionCallOutput(call_id=evt.call_id, result=convert_fastmcp_result(evt.result))
        self._spawn(self._send_and_reduce(fco))
        self._spawn(self._emit_ui_bus_messages())
        # Notify MCP bridge of session state change
        if self._session_state_notifier is not None:
            self._session_state_notifier()

    def set_session_state_notifier(self, notifier: Callable[[], None]) -> None:
        """Set notifier callback for session state changes (for MCP resource updates)."""
        self._session_state_notifier = notifier


class AgentSession:
    def __init__(
        self,
        manager: ConnectionManager,
        approval_hub: ApprovalHub | None = None,
        *,
        persistence=None,
        agent_id: AgentID | None = None,
        ui_bus: ServerBus | None = None,
        approval_engine: ApprovalPolicyEngine | None = None,
    ) -> None:
        self._task: asyncio.Task | None = None
        self.approval_hub = approval_hub or ApprovalHub()
        self._lock = asyncio.Lock()
        self.active_run: RunState | None = None
        self._run_counter = 0
        self._agent: MiniCodex | None = None
        self._manager = manager
        self._persistence = persistence
        self.ui_bus: ServerBus | None = ui_bus
        self.ui_state: UiState = new_state()
        self.approval_engine: ApprovalPolicyEngine | None = approval_engine
        self._persist_handler: RunPersistenceHandler | None = None
        # Optional: agent identifier to associate runs with a specific hosted agent
        self.agent_id: AgentID | None = agent_id
        # No Docker client on session; runtime server handles any containerization
        # Optional: UI state change notifier for MCP resource updates
        self._ui_state_notifier: Callable[[], None] | None = None

    def current_run_phase(self) -> RunPhase:
        """Compute the current run phase from live signals (no stored state).

        - IDLE: no active run
        - WAITING_APPROVAL: there are pending approvals
        - TOOLS_RUNNING: MCP manager reports in-flight requests
        - SAMPLING: default while running without approvals or tool exec
        """
        pending = len(self.approval_hub.pending)
        # Tool inflight detection is not exposed at this layer
        has_inflight = False
        return determine_run_phase(
            active_run_id=(self.active_run.run_id if self.active_run else None),
            pending_approvals=pending,
            mcp_has_inflight=has_inflight,
        )

    async def build_snapshot(self, sampling=None) -> Snapshot:
        if self.active_run:
            self.active_run.pending_approvals = [
                ApprovalBrief(tool_call=req.tool_call) for req in self.approval_hub._requests.values()
            ]

        approval_policy = None
        if self.approval_engine is None:
            raise RuntimeError("approval_engine not configured for session")

        content, policy_id = self.approval_engine.get_policy()
        proposals: list[ProposalInfo] = []
        # Load proposals from persistence policy store
        if self._persistence is not None and self.agent_id:
            rows = await self._persistence.list_policy_proposals(self.agent_id)
            for r in rows:
                pid = str(r.id)
                raw = str(r.status)
                proposals.append(ProposalInfo(id=pid, status=ProposalStatus(raw)))
        approval_policy = ApprovalPolicyInfo(content=content, id=policy_id, proposals=proposals)

        # Build preferred details bundle when all components are present
        details = None
        if (self.active_run is not None) and (sampling is not None) and (approval_policy is not None):
            details = SnapshotDetails(run_state=self.active_run, sampling=sampling, approval_policy=approval_policy)

        return Snapshot(
            v="1.0.0",
            session_state=SessionState(
                session_id=self._manager._session_id,
                version="1.0.0",
                capabilities=[],
                last_event_id=self._manager._event_id or None,
                active_run_id=(self.active_run.run_id if self.active_run else None),
                run_counter=self._run_counter,
            ),
            approval_policy=approval_policy,
            details=details,
        )

    def attach_agent(self, agent: MiniCodex, *, model: str | None = None, system: str | None = None) -> None:
        self._agent = agent
        self._model = model
        self._system_text = system
        self._manager.set_session(self)

    def set_persist_handler(self, handler: RunPersistenceHandler) -> None:
        self._persist_handler = handler

    def set_ui_state_notifier(self, notifier: Callable[[], None]) -> None:
        """Set notifier callback for UI state changes (for MCP resource updates)."""
        self._ui_state_notifier = notifier

    async def _apply_ui_event(self, evt: ServerMessage) -> None:
        self.ui_state = reduce_ui_state(self.ui_state, evt)
        await self._manager.send_payload(UiStateUpdated(v="ui_state_v1", seq=self.ui_state.seq, state=self.ui_state))
        # Notify MCP bridge if notifier is set
        if self._ui_state_notifier is not None:
            self._ui_state_notifier()

    async def run(self, prompt: str) -> None:
        async with self._lock:
            if self._task is not None and not self._task.done():
                await self._manager.send_payload(ErrorEvt(code=ErrorCode.BUSY, message="agent_busy"))
                return
            self._task = asyncio.create_task(self._run_impl(prompt))

    async def cancel_active_run(self) -> None:
        """Cancel currently running task (if any) and await its completion."""
        # First, send protocol-level cancellations for any in-flight MCP requests
        if self._agent is not None:
            # Best-effort: synthesize aborted outputs for pending calls; do not
            # attempt to cancel MCP requests via private attributes
            try:
                # Synthesize aborted outputs for any pending tool calls so the
                # Responses API invariant (each function_call has an output) holds.
                # This prevents downstream 400 errors when the SDK validates input.
                self._agent.abort_pending_tool_calls()
            except Exception:
                # Best-effort; do not block abort on synthesis failures
                logger.error("abort_pending_tool_calls failed", exc_info=True)
                # Re-raise after logging - synthesis failures indicate broken state
                raise
        t = self._task
        if t is None or t.done():
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    async def _run_impl(self, prompt: str) -> None:
        if self._agent is not None:
            # Agent notices are injected via the NotificationsHandler/ServerModeHandler from MCP resource updates
            run_id = uuid.uuid4()
            started = datetime.now(UTC)
            model_params: dict[str, Any] = {}
            if self._persistence is None:
                raise RuntimeError("persistence not configured")
            await self._persistence.start_run(
                run_id=run_id,
                agent_id=self.agent_id,
                system_message=self._system_text,
                model=self._model,
                model_params=model_params,
                started_at=started,
            )
            await self._manager.send_payload(
                RunStatusEvt(
                    run_state=RunState(
                        run_id=run_id,
                        status=UiRunStatus.RUNNING,
                        started_at=started,
                        finished_at=None,
                        pending_approvals=[],
                        last_event_id=None,
                    )
                )
            )
            # Also push a fresh Snapshot so UIs that rely on snapshot-only
            # state (not incremental run_status) update immediately.
            # This helps early UI elements like the Abort button appear
            # deterministically even if they don't consume run_status events.
            await self._manager.send_payload(await self.build_snapshot())
            self.active_run = RunState(
                run_id=run_id, status=UiRunStatus.RUNNING, started_at=started, pending_approvals=[], last_event_id=None
            )
            self._run_counter += 1
            # Notify MCP bridge of session state change (run started)
            if self._manager._session_state_notifier is not None:
                self._manager._session_state_notifier()
            finish_status = PersistenceRunStatus.FINISHED
            try:
                await self._agent.run(user_text=prompt)
            except asyncio.CancelledError:
                await self._manager.send_payload(ErrorEvt(code=ErrorCode.ABORTED))
                finish_status = PersistenceRunStatus.ABORTED
            except Exception as e:
                await self._manager.send_payload(
                    ErrorEvt(code=ErrorCode.AGENT_ERROR, message=f"agent_run_exception: {e}")
                )
                finish_status = PersistenceRunStatus.ERROR
            finally:
                if self.active_run:
                    self.active_run.status = UiRunStatus.FINISHED
                    self.active_run.finished_at = datetime.now(UTC)
                self.active_run = None
                await self._manager.flush()
                if self._persist_handler is not None:
                    # Ensure all transcript events have been persisted before finishing the run
                    await self._persist_handler.drain()
                await self._persistence.finish_run(run_id=run_id, status=finish_status, finished_at=datetime.now(UTC))
                await self._manager.send_payload(
                    RunStatusEvt(
                        run_state=RunState(
                            run_id=run_id,
                            status=UiRunStatus.FINISHED,
                            started_at=started,
                            finished_at=datetime.now(UTC),
                            pending_approvals=[],
                            last_event_id=None,
                        )
                    )
                )
                # Keep snapshot run_state in sync with finished status
                await self._manager.send_payload(await self.build_snapshot())
                # Notify MCP bridge of session state change (run finished)
                if self._manager._session_state_notifier is not None:
                    self._manager._session_state_notifier()
            return
        await self._manager.send_payload(ErrorEvt(code=ErrorCode.NO_AGENT, message="no_agent_attached"))
        return
