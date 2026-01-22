"""Prompt optimizer agent main entry point for in-container execution.

This is the CMD entrypoint for the prompt optimizer container. It:
1. Connects to backend REST API for orchestration tools (run_critic)
2. Polls database directly for grading status (wait_until_graded)
3. Renders the system prompt
4. Runs the agent loop until submit succeeds or failure
5. Exits with appropriate code

Architecture:
- All tools registered on DirectToolProvider:
  - exec, submit, report_failure (local)
  - run_critic (call backend REST API)
  - wait_until_graded (poll database directly)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import SystemMessage
from props.core.eval_client import EvalClient, wait_until_graded
from props.core.ids import DefinitionId
from props.core.loop_utils import create_bound_model_from_env, render_system_prompt, setup_logging
from props.core.models.examples import ExampleSpec

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to optimize prompts. Do not output text directly. "
    "Use run_critic and wait_until_graded to evaluate agents, then call submit when done."
)

# Default workspace path
WORKSPACE = Path("/workspace")


# =============================================================================
# Tool argument models (for DirectToolProvider)
# =============================================================================


class SubmitArgs(BaseModel):
    """Arguments for submit tool."""

    summary: str = Field(..., description="Summary of the optimization results and findings")


class ReportFailureArgs(BaseModel):
    """Arguments for report_failure tool."""

    message: str = Field(..., description="Description of why optimization could not be completed")


class RunCriticToolArgs(BaseModel):
    """Arguments for run_critic tool (subset of RunCriticRequest for agent use)."""

    definition_id: DefinitionId = Field(
        description="Agent package ID (from 'props agent-pkg create' or 'critic' for baseline)"
    )
    example: ExampleSpec = Field(description="Example to evaluate (WholeSnapshotExample or SingleFileSetExample)")
    timeout_seconds: int = Field(default=3600, description="Max seconds before container is killed")
    budget_usd: float | None = Field(default=None, description="Max USD cost for this agent")


class WaitUntilGradedToolArgs(BaseModel):
    """Arguments for wait_until_graded tool."""

    critic_run_id: str = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(default=300, ge=10, le=3600, description="Max time to wait (default 300s)")
    poll_interval_seconds: int = Field(default=5, ge=1, le=60, description="Polling interval (default 5s)")


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


def create_tool_provider(state: LoopState, eval_client: EvalClient, critic_model: str) -> DirectToolProvider:
    """Create tool provider with all tools (local + eval API)."""
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

    @provider.tool
    async def run_critic(args: RunCriticToolArgs) -> str:
        """Run critic agent on an example.

        Returns critic_run_id. Use wait_until_graded to get grading results.
        """
        logger.info(f"Running critic: definition={args.definition_id}, example={args.example}")
        response = await eval_client.run_critic(
            definition_id=args.definition_id,
            example=args.example,
            timeout_seconds=args.timeout_seconds,
            budget_usd=args.budget_usd,
            critic_model=critic_model,
        )
        logger.info(f"Critic run completed: {response.critic_run_id}, status={response.status}")
        return (
            f"Critic run completed.\n"
            f"critic_run_id: {response.critic_run_id}\n"
            f"status: {response.status.value}\n\n"
            f"Use wait_until_graded with this critic_run_id to get grading results."
        )

    @provider.tool
    async def wait_until_graded_tool(args: WaitUntilGradedToolArgs) -> str:
        """Wait for a critic run to be fully graded.

        Polls the database directly until grading is complete or timeout.
        """
        critic_run_id = UUID(args.critic_run_id)
        logger.info(f"Waiting for grading: {critic_run_id}")
        response = await wait_until_graded(
            critic_run_id, timeout_seconds=args.timeout_seconds, poll_interval_seconds=args.poll_interval_seconds
        )
        logger.info(f"Grading complete: total_credit={response.total_credit}, max_credit={response.max_credit}")
        return (
            f"Grading complete.\n"
            f"grader_run_id: {response.grader_run_id}\n"
            f"total_credit: {response.total_credit}\n"
            f"max_credit: {response.max_credit}\n"
            f"split: {response.split}\n"
            f"example_kind: {response.example_kind}\n\n"
            f"Query aggregate metrics: SELECT * FROM recall_by_definition_split_kind "
            f"WHERE critique_run_id = '{critic_run_id}';"
        )

    return provider


class LoggingHandler(BaseHandler):
    """Handler that logs events for debugging."""

    def on_error(self, exc: Exception) -> None:
        logger.error("Agent error: %s", exc)
        raise exc


async def run_prompt_optimizer_loop(system_prompt: str, eval_client: EvalClient, critic_model: str) -> int:
    """Run the prompt optimizer agent loop.

    Args:
        system_prompt: The system prompt for the optimizer agent
        eval_client: EvalClient connected to backend for remote tools
        critic_model: Model to use for critic agents

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    state = LoopState()
    tool_provider = create_tool_provider(state, eval_client, critic_model)

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

    # Get critic model from environment (set by registry)
    critic_model = os.environ.get("PROPS_CRITIC_MODEL", "gpt-5.1-codex-mini")

    # Connect to backend REST API for orchestration tools
    logger.info("Connecting to backend REST API")
    async with EvalClient.from_env() as eval_client:
        logger.info("Connected to backend at %s", eval_client.backend_url)

        # Render system prompt
        logger.info("Rendering system prompt")
        system_prompt = render_system_prompt("props/docs/agents/prompt_optimizer.md.j2")

        # Run the agent loop
        logger.info("Starting agent loop")
        exit_code = await run_prompt_optimizer_loop(system_prompt, eval_client, critic_model)

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
