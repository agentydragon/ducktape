"""Agent handle - wraps Agent with image digest and system prompt from /init.

AgentHandle provides a similar interface to Agent (process_message, run) but manages:
- Container lifecycle with OCI image
- Running init script and using its output as system prompt
- System message injection

Usage:
    async with AgentEnvironment(...) as comp:
        handle = await AgentHandle.create(
            agent_run_id=run_id,
            image_digest="sha256:abc123...",
            model_client=openai_client,
            mcp_client=mcp_client,
            compositor=comp,
            handlers=[],
        )
        handle.process_message(UserMessage.text("Review this code"))
        result = await handle.run()

Note: image_digest must be a canonical OCI digest (sha256:...).
The /init script output becomes the system prompt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from agent_core.agent import Agent, AgentResult, Message
from agent_core.handler import BaseHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_pkg.host.init_runner import run_init_script
from openai_utils.model import SystemMessage
from openai_utils.types import ReasoningSummary
from props.core.db_event_handler import DatabaseEventHandler

if TYPE_CHECKING:
    from fastmcp.client import Client

    from openai_utils.model import OpenAIModelProto
    from props.core.docker_env import PropertiesDockerCompositor

logger = logging.getLogger(__name__)


# Note: load_definition_archive() removed - agent definitions are now OCI images.
# Images are pulled from registry by AgentEnvironment, not loaded from database archives.


@dataclass
class AgentHandle:
    """Handle to a running agent with transcript management.

    Provides Agent-like interface (process_message, run) while managing:
    - Container lifecycle
    - System message injection from /init output

    Use create() classmethod to construct.
    """

    agent_run_id: UUID
    image_digest: str
    agent: Agent
    compositor: PropertiesDockerCompositor
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def process_message(self, message: Message) -> None:
        """Add a message to the agent's transcript and notify handlers.

        Same semantics as Agent.process_message() - messages are added to
        the conversation history and handlers are notified.

        Args:
            message: The message to add
        """
        self.agent.process_message(message)

    async def run(self) -> AgentResult:
        """Run the agent loop until completion.

        Returns after the agent finishes (submit tool, max turns, abort, etc.).
        Turn limits are controlled by MaxTurnsHandler passed to create().

        Thread-safe: only one run() call can execute at a time.
        """
        async with self._lock:
            return await self.agent.run()

    @classmethod
    async def create(
        cls,
        *,
        agent_run_id: UUID,
        image_digest: str,
        model_client: OpenAIModelProto,
        mcp_client: Client,
        compositor: PropertiesDockerCompositor,
        handlers: list[BaseHandler],
        dynamic_instructions: Callable[[], Awaitable[str]] | None = None,
        parallel_tool_calls: bool = False,
        reasoning_summary: ReasoningSummary | None = None,
    ) -> AgentHandle:
        """Create an AgentHandle with system prompt from /init output.

        Args:
            agent_run_id: UUID for this agent run (used for DB tracking)
            image_digest: Canonical OCI image digest (sha256:...)
            model_client: OpenAI-compatible model client
            mcp_client: FastMCP client connected to compositor
            compositor: MCP compositor with mounted Docker runtime server (has .runtime attribute)
            handlers: Additional handlers beyond the default (DatabaseEventHandler)
            dynamic_instructions: Optional callable that returns dynamic instructions string
            parallel_tool_calls: Whether to allow parallel tool calls (default False)
            reasoning_summary: Optional reasoning summary mode for the agent

        Returns:
            AgentHandle ready for process_message() and run() calls.

        Raises:
            InitFailedError: If init script fails
        """
        # TODO: For conversational sub-agents (agent that returns text to parent),
        # add CaptureTextHandler here. Currently all agents use RedirectOnTextMessageHandler
        # or custom handlers that remind and continue on text.
        agent = await Agent.create(
            mcp_client=mcp_client,
            client=model_client,
            handlers=[DatabaseEventHandler(agent_run_id=agent_run_id), *handlers],
            tool_policy=AllowAnyToolOrTextMessage(),
            dynamic_instructions=dynamic_instructions,
            parallel_tool_calls=parallel_tool_calls,
            reasoning_summary=reasoning_summary,
        )

        # Insert system message from init output
        system_prompt = await run_init_script(mcp_client, compositor.runtime)
        logger.debug(f"Init script returned {len(system_prompt)} bytes")
        agent.process_message(SystemMessage.text(system_prompt))

        return cls(agent_run_id=agent_run_id, image_digest=image_digest, agent=agent, compositor=compositor)
