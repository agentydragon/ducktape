"""Shared agent loop infrastructure for critic development agents.

Contains tool argument models, loop state, tool provider factory, and handlers
used by both prompt optimizer and improvement agents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import BaseHandler
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from props.core.eval_client import EvalClient, wait_until_graded
from props.core.ids import DefinitionId
from props.core.models.examples import ExampleSpec
from props.db.database import Database

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to optimize prompts. Do not output text directly. "
    "Use run_critic and wait_until_graded to evaluate agents, then keep iterating."
)

# Default workspace path
WORKSPACE = Path("/workspace")


# =============================================================================
# Tool argument models
# =============================================================================


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
# Loop state
# =============================================================================


class LoopStatus(StrEnum):
    """Agent loop execution status."""

    IN_PROGRESS = auto()
    EXITED_FAILURE = auto()


@dataclass
class LoopState:
    """Mutable state for agent loop."""

    status: LoopStatus = LoopStatus.IN_PROGRESS


# =============================================================================
# Handlers
# =============================================================================


class LoggingHandler(BaseHandler):
    """Handler that logs errors for debugging."""

    def on_error(self, exc: Exception) -> None:
        logger.error("Agent error: %s", exc)
        raise exc


# =============================================================================
# Tool provider
# =============================================================================


def create_tool_provider(
    state: LoopState, eval_client: EvalClient, critic_model: str, db: Database
) -> DirectToolProvider:
    """Create tool provider with shared tools (no submit)."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, running tests, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> None:
        """Report that the task could not be completed.

        Use when there are blocking issues (e.g., no viable path forward).
        Signals exit with failure status.
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
            critic_run_id, db, timeout_seconds=args.timeout_seconds, poll_interval_seconds=args.poll_interval_seconds
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
