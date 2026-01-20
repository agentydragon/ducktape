"""In-container agent loop for prompt optimizer agents.

Runs the full agent loop inside the container:
1. Fetches agent_run_id from environment
2. Constructs system prompt
3. Connects to host MCP server for orchestration tools (run_critic, run_grader)
4. Calls LLM via proxy (OPENAI_BASE_URL)
5. Executes local tools (exec) and remote MCP tools
6. Exits on successful submit or reported failure

Architecture:
- Local tools: exec, submit, report_failure (via DirectToolProvider)
- Remote tools: run_critic, run_grader (via MCP-over-HTTP to host)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path

from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.tool_provider import CompositeToolProvider
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.direct import DirectExecArgs, run_direct_exec
from openai_utils.model import SystemMessage
from props.core.loop_utils import create_bound_model_from_env
from props.core.prompt_optimize.tools import ReportFailureArgs, SubmitArgs

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to optimize prompts. Do not output text directly. "
    "Use run_critic and run_grader to evaluate agents, then call submit when done."
)

# Default workspace path
WORKSPACE = Path("/workspace")


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
