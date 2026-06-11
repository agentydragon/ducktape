from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

from agent_core.agent import Agent
from agent_core.events import AssistantText, Response, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from agent_server.agent_types import AgentID
from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.mcp.ui.server import UiServer
from agent_server.persist.handler import RunPersistenceHandler
from agent_server.persist.types import Persistence
from agent_server.server.bus import ServerBus, UiEndTurn, UiMessage
from agent_server.server.protocol import (
    FunctionCallOutput,
    ServerMessage,
    ToolCall as UiToolCall,
    UiEndTurnEvt,
    UiMessageEvt,
    UiMessagePayload,
    UserText as UiUserText,
)
from agent_server.server.reducer import Reducer
from agent_server.server.state import UiState, new_state
from mcp_infra.mounted import Mounted
from openai_utils.model import UserMessage as OAIUserMessage

logger = logging.getLogger(__name__)


class UiEventHandler(BaseHandler):
    """Handles agent events and delivers messages to UI clients via ServerBus."""

    def __init__(self) -> None:
        self._session: AgentSession | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._event_id: int = 0
        self._session_id: str = str(uuid.uuid4())

    def _next_event_id(self) -> int:
        self._event_id += 1
        return self._event_id

    async def _send_and_reduce(self, payload: ServerMessage) -> None:
        assert self._session is not None
        await self._session._apply_ui_event(payload)

    async def _emit_ui_bus_messages(self) -> None:
        assert self._session is not None
        if self._session.ui_bus is None:
            return
        for item in self._session.ui_bus.drain_messages():
            if isinstance(item, UiMessage):
                await self._send_and_reduce(
                    UiMessageEvt(message=UiMessagePayload(mime=item.mime, content=item.content))
                )
            elif isinstance(item, UiEndTurn):
                await self._send_and_reduce(UiEndTurnEvt())

    def set_session(self, session: AgentSession) -> None:
        self._session = session

    def on_response(self, evt: Response) -> None:
        return None

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        t: asyncio.Task[None] = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def flush(self) -> None:
        if not self._bg_tasks:
            return
        await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

    def on_user_text_event(self, evt: UserText) -> None:
        self._spawn(self._send_and_reduce(UiUserText(text=evt.text)))

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        raise RuntimeError("assistant_text not allowed in UI mode; use ui.send_message tool instead")

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._spawn(self._send_and_reduce(UiToolCall(name=evt.name, args_json=evt.args_json, call_id=evt.call_id)))

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._spawn(self._send_and_reduce(FunctionCallOutput(call_id=evt.call_id, result=evt.result)))
        self._spawn(self._emit_ui_bus_messages())


class AgentSession:
    def __init__(
        self,
        manager: UiEventHandler,
        *,
        persistence: Persistence,
        agent_id: AgentID,
        approval_engine: PolicyEngine,
        ui_bus: ServerBus | None = None,
        ui_mount: Mounted[UiServer] | None = None,
    ) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._agent: Agent | None = None
        self._manager = manager
        self._persistence: Persistence = persistence
        self.ui_bus: ServerBus | None = ui_bus
        self._ui_mount: Mounted[UiServer] | None = ui_mount
        self._reducer: Reducer = Reducer(ui_mount)
        self.ui_state: UiState = new_state()
        self.approval_engine: PolicyEngine = approval_engine
        self._persist_handler: RunPersistenceHandler | None = None
        self.agent_id: AgentID = agent_id

    def attach_agent(self, agent: Agent, *, model: str | None = None, system: str | None = None) -> None:
        self._agent = agent
        self._model = model
        self._system_text = system
        self._manager.set_session(self)

    def set_persist_handler(self, handler: RunPersistenceHandler) -> None:
        self._persist_handler = handler

    async def _apply_ui_event(self, evt: ServerMessage) -> None:
        self.ui_state = self._reducer.reduce(self.ui_state, evt)

    async def run(self, prompt: str) -> None:
        async with self._lock:
            if self._task is not None and not self._task.done():
                # Error now returned via HTTP error response, not sent via dead send_payload
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
                logger.debug("abort_pending_tool_calls failed", exc_info=True)
        t = self._task
        if t is None or t.done():
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    async def _run_impl(self, prompt: str) -> None:
        if self._agent is not None:
            # Agent notices are injected via the NotificationsHandler/ServerModeHandler from MCP resource updates
            try:
                self._agent.process_message(OAIUserMessage.text(prompt))
                await self._agent.run()
            except asyncio.CancelledError:
                # Error now logged, not sent via dead send_payload
                logger.debug("agent_run_cancelled")
            except Exception as e:
                # Error now logged, not sent via dead send_payload
                logger.error(f"agent_run_exception: {e}", exc_info=True)
            finally:
                await self._manager.flush()
                if self._persist_handler is not None:
                    # Ensure all transcript events have been persisted
                    await self._persist_handler.drain()
        else:
            # Error now logged, not sent via dead send_payload
            logger.error("no_agent_attached")
