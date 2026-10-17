"""Ember runtime - main orchestrator for the Matrix chat agent.

Uses agent_core.Agent for the agent loop and MCP tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastmcp import ClientGroup

from agent_core.agent import Agent
from agent_core.handler import RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from openai_utils.client_factory import build_client
from openai_utils.model import SystemMessage, UserMessage
from x.ember.config import EmberSettings
from x.ember.handlers import EmberPersistenceHandler, EmberSleepHandler
from x.ember.matrix_client import MatrixClient
from x.ember.mcp_tools import make_ember_client_group
from x.ember.python_session import ensure_kernel

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
    - FastMCP client group with Ember's MCP tools
    - agent_core.Agent for LLM interaction (persistent across conversation turns)
    - Handlers for persistence and sleep control

    Use create() classmethod for initialization, not __init__ directly.
    """

    def __init__(self, settings: EmberSettings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @classmethod
    async def create(cls, settings: EmberSettings) -> EmberRuntime:
        """Create and initialize an EmberRuntime.

        This is the primary way to create a runtime - handles all async initialization.
        """
        runtime = cls(settings)
        await runtime._initialize()
        return runtime

    async def _initialize(self) -> None:
        """Initialize or reinitialize all components."""
        # Create Matrix client
        self._matrix_client = MatrixClient(self._settings.matrix)

        # Ensure workspace exists
        self._settings.workspace_path.mkdir(parents=True, exist_ok=True)

        # Create handlers
        self._sleep_handler = EmberSleepHandler()
        handlers = [
            self._sleep_handler,
            EmberPersistenceHandler(self._settings.history_path),
            RedirectOnTextMessageHandler(TEXT_REMINDER),
        ]

        # Connect directly to the MCP tool servers.
        self._client_group: ClientGroup = make_ember_client_group(
            workspace_path=self._settings.workspace_path,
            sleep_callback=self._sleep_handler.request_sleep,
            status_provider=self._matrix_client,
            sleep_policy=self._settings.openai.sleep_tool_policy,
        )
        await self._client_group.__aenter__()

        # Create persistent agent
        model_client = build_client(
            self._settings.openai.model, reasoning_effort=self._settings.openai.reasoning_effort
        )

        self._agent = await Agent.create(
            tool_provider=MCPToolProvider(self._client_group),
            client=model_client,
            handlers=handlers,
            tool_policy=AllowAnyToolOrTextMessage(),
            reasoning_effort=self._settings.openai.reasoning_effort,
        )

        # Insert system prompt into agent's transcript
        self._agent.process_message(SystemMessage.text(self._settings.openai.system_prompt))

        # Ensure Python kernel is running
        ensure_kernel()

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
        await self._client_group.__aexit__(None, None, None)
        await self._matrix_client.close()

        logger.info("Ember runtime stopped")

    async def restart(self) -> None:
        """Restart the runtime with fresh components."""
        await self.stop()
        await self._initialize()
        await self.start()

    async def _loop(self) -> None:
        """Main event loop - poll Matrix and run agent."""
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
                await self._matrix_client.set_typing(room_ids, typing=True)

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
                    await self._matrix_client.set_typing(room_ids, typing=False)
