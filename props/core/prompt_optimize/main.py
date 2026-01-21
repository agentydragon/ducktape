"""Prompt optimizer agent main entry point for in-container execution.

This is the CMD entrypoint for the prompt optimizer container. It:
1. Connects to host MCP server for orchestration tools (run_critic, run_grader)
2. Renders the system prompt
3. Runs the agent loop until submit succeeds or failure
4. Exits with appropriate code

Architecture:
- Local tools: exec, submit, report_failure (via DirectToolProvider)
- Remote tools: run_critic, run_grader (via MCP-over-HTTP to host)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path

from fastmcp.client import Client
from pydantic import BaseModel, Field

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.tool_provider import CompositeToolProvider
from agent_pkg.runtime.mcp import mcp_client_from_env
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import SystemMessage
from props.core.loop_utils import create_bound_model_from_env, render_system_prompt, setup_logging

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to optimize prompts. Do not output text directly. "
    "Use run_critic and run_grader to evaluate agents, then call submit when done."
)

# Default workspace path
WORKSPACE = Path("/workspace")


# =============================================================================
# Tool argument models
# =============================================================================


class SubmitArgs(BaseModel):
    """Arguments for submit tool."""

    summary: str = Field(..., description="Summary of the optimization results and findings")


class ReportFailureArgs(BaseModel):
    """Arguments for report_failure tool."""

    message: str = Field(..., description="Description of why optimization could not be completed")


# =============================================================================
# Loop state and tools
# =============================================================================


class LoopStatus(StrEnum):
    """Agent loop execution status."""

    IN_PROGRESS = auto()
    EXITED_SUCCESS = auto()
    EXITED_FAILURE = auto()


@dataclass
class LoopState:
    """Mutable state for agent loop."""

    status: LoopStatus = LoopStatus.IN_PROGRESS


def create_local_tool_provider(state: LoopState) -> DirectToolProvider:
    """Create tool provider with local tools."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, running tests, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def submit(args: SubmitArgs) -> None:
        """Finalize and submit the optimization run.

        Signals exit. Host updates agent_run status based on exit code 0.
        """
        state.status = LoopStatus.EXITED_SUCCESS
        logger.info("Optimization submitted: %s", args.summary)

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> None:
        """Report that the optimization could not be completed.

        Use when there are blocking issues (e.g., no viable path forward).
        Signals exit. Host updates agent_run status based on exit code 1.
        """
        state.status = LoopStatus.EXITED_FAILURE
        logger.info("Reported failure: %s", args.message)

    return provider


class LoggingHandler(BaseHandler):
    """Handler that logs events for debugging."""

    def on_error(self, exc: Exception) -> None:
        logger.error("Agent error: %s", exc)
        raise exc


async def run_prompt_optimizer_loop(system_prompt: str, mcp_client: Client) -> int:
    """Run the prompt optimizer agent loop.

    Args:
        system_prompt: The system prompt for the optimizer agent
        mcp_client: Connected MCP client for remote tools (run_critic, run_grader)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    state = LoopState()
    local_provider = create_local_tool_provider(state)

    # Create MCP tool provider for remote tools
    mcp_provider = MCPToolProvider(mcp_client)

    # Combine providers (local tools take precedence)
    tool_provider = CompositeToolProvider(mcp_provider, local_provider)

    bound_model = create_bound_model_from_env()

    # Create handlers
    handlers: list[BaseHandler] = [
        LoggingHandler(),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        AbortIf(lambda: state.status != LoopStatus.IN_PROGRESS),
    ]

    # Create and run agent
    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=handlers,
        client=bound_model,
        parallel_tool_calls=True,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    # Add system prompt
    agent.process_message(SystemMessage.text(system_prompt))

    await agent.run()
    match state.status:
        case LoopStatus.EXITED_SUCCESS:
            logger.info("Optimization completed")
            return 0
        case LoopStatus.EXITED_FAILURE:
            logger.info("Optimization failed")
            return 1
        case LoopStatus.IN_PROGRESS:
            logger.warning("Agent finished without explicit exit")
            return 1


# =============================================================================
# Entry point
# =============================================================================


async def main() -> int:
    """Main entry point for prompt optimizer agent."""
    setup_logging()

    logger.info("Prompt optimizer agent starting")

    # Connect to host MCP server for orchestration tools
    logger.info("Connecting to host MCP server")
    async with mcp_client_from_env() as (mcp_client, init_result):
        logger.info(
            "Connected to MCP server: %s (version %s)", init_result.serverInfo.name, init_result.serverInfo.version
        )

        # Render system prompt
        logger.info("Rendering system prompt")
        system_prompt = render_system_prompt("props/docs/agents/prompt_optimizer.md.j2")

        # Run the agent loop
        logger.info("Starting agent loop")
        exit_code = await run_prompt_optimizer_loop(system_prompt, mcp_client)

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
