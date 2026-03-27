"""Ember runtime - main orchestrator for the Matrix chat agent.

Uses agent_core.Agent for the agent loop and MCP tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from ember.config import EmberSettings
from ember.handlers import EmberPersistenceHandler, EmberSleepHandler
from ember.matrix_client import MatrixClient
from ember.mcp_tools import EmberCompositor
from ember.python_session import ensure_kernel
from openai_utils.client_factory import build_client
from openai_utils.model import SystemMessage, UserMessage

# Reminder injected when agent sends text instead of using tools
TEXT_REMINDER = (
    "Text messages won't be delivered to users. Use MCP tools to accomplish tasks, "
    "then call sleep_until_user_message to yield control."
)

logger = logging.getLogger(__name__)


class EmberRuntime:
    """Runtime orchestrator for ember Matrix chat agent.

    Manages lifecycle of:
    - Matrix client for chat I/O
    - EmberCompositor with MCP tools
    - agent_core.Agent for LLM interaction (persistent across conversation turns)
    - Handlers for persistence and sleep control

    Use create() classmethod for initialization, not __init__ directly.
    """

    def __init__(
        self,
        settings: EmberSettings,
        matrix_client: MatrixClient,
        compositor: EmberCompositor,
        mcp_client: Client,
        sleep_handler: EmberSleepHandler,
        agent: Agent,
    ) -> None:
        self._settings = settings
        self._matrix_client = matrix_client
        self._compositor = compositor
        self._mcp_client = mcp_client
        self._sleep_handler = sleep_handler  # Need reference for reset()
        self._agent = agent

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @classmethod
    async def create(cls, settings: EmberSettings) -> EmberRuntime:
        """Create and initialize an EmberRuntime.

        This is the primary way to create a runtime - handles all async initialization.
        """
        matrix_client = MatrixClient(settings.matrix)

        settings.workspace_path.mkdir(parents=True, exist_ok=True)

        sleep_handler = EmberSleepHandler()
        handlers = [
            sleep_handler,
            EmberPersistenceHandler(settings.history_path),
            RedirectOnTextMessageHandler(TEXT_REMINDER),
        ]

        compositor = EmberCompositor(
            workspace_path=settings.workspace_path,
            sleep_callback=sleep_handler.request_sleep,
            status_provider=matrix_client,
            sleep_policy=settings.openai.sleep_tool_policy,
        )
        await compositor.__aenter__()

        mcp_client = Client(compositor)
        await mcp_client.__aenter__()

        model_client = build_client(settings.openai.model, reasoning_effort=settings.openai.reasoning_effort)

        agent = await Agent.create(
            tool_provider=MCPToolProvider(mcp_client),
            client=model_client,
            handlers=handlers,
            tool_policy=AllowAnyToolOrTextMessage(),
            reasoning_effort=settings.openai.reasoning_effort,
            dynamic_instructions=compositor.render_agent_dynamic_instructions,
        )

        agent.process_message(SystemMessage.text(settings.openai.system_prompt))

        ensure_kernel()

        return cls(
            settings=settings,
            matrix_client=matrix_client,
            compositor=compositor,
            mcp_client=mcp_client,
            sleep_handler=sleep_handler,
            agent=agent,
        )

    async def start(self) -> None:
        """Start the runtime - begin Matrix client and main loop."""
        logger.info("Starting ember runtime")
        await self._matrix_client.start()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="ember-runtime-loop")

    async def stop(self) -> None:
        """Stop the runtime gracefully."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        # Cleanup components
        await self._mcp_client.__aexit__(None, None, None)
        await self._compositor.__aexit__(None, None, None)
        await self._matrix_client.close()

        logger.info("Ember runtime stopped")

    async def restart(self) -> None:
        """Restart the runtime with fresh components."""
        await self.stop()
        self._reinitialize(await EmberRuntime.create(self._settings))
        await self.start()

    def _reinitialize(self, other: EmberRuntime) -> None:
        self._matrix_client = other._matrix_client
        self._compositor = other._compositor
        self._mcp_client = other._mcp_client
        self._sleep_handler = other._sleep_handler
        self._agent = other._agent

    async def _loop(self) -> None:
        """Main event loop - poll Matrix and run agent."""
        try:
            while not self._stop_event.is_set():
                # Poll for Matrix events
                try:
                    async with asyncio.timeout(60.0):
                        events = await self._matrix_client.get_events()
                except TimeoutError:
                    events = []

                if not events:
                    continue

                # Format incoming messages
                message_text = "\n".join(f"{event.sender}: {event.body}" for event in events)
                logger.info("Received Matrix batch:\n%s", message_text)
                # nio sets room_id on events at runtime but it's not in type stubs
                room_ids = {event.room_id for event in events if event.room_id}

                # Set typing indicator
                if room_ids:
                    await self._matrix_client.set_typing(room_ids, True)

                try:
                    # Reset sleep handler for new conversation turn
                    self._sleep_handler.reset()

                    # Add user message to persistent agent and run
                    self._agent.process_message(UserMessage.text(message_text))

                    # Reset agent's finished state so it can run again
                    self._agent.finished = False

                    # Run agent until sleep handler aborts
                    await self._agent.run()
                finally:
                    if room_ids:
                        await self._matrix_client.set_typing(room_ids, False)

        except asyncio.CancelledError:
            raise
