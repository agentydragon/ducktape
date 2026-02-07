"""Shared agent loop infrastructure for critic developer agents.

Contains tool argument models, loop state, tool provider factory, and handlers
used by both optimize and improve modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from uuid import UUID

from pydantic import Field

from agent_core.direct_provider import DirectToolProvider
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.agents.critic_dev.eval_client import CriticRunClient
from props.agents.critic_dev.grading import wait_until_graded
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
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


class ReportFailureArgs(OpenAIStrictModeBaseModel):
    """Arguments for report_failure tool."""

    message: str = Field(..., description="Description of why optimization could not be completed")


class RunCriticToolArgs(OpenAIStrictModeBaseModel):
    """Arguments for run_critic tool (subset of RunCriticRequest for agent use)."""

    definition_id: DefinitionId = Field(
        description="Image ref: OCI digest (sha256:...) for custom images, or 'latest' for builtin"
    )
    example: ExampleSpec = Field(description="Example to evaluate")
    timeout_seconds: int = Field(description="Max seconds before container is killed")
    budget_usd: float = Field(description="Max USD cost for this agent")


class WaitUntilGradedToolArgs(OpenAIStrictModeBaseModel):
    """Arguments for wait_until_graded tool."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(default=300, ge=10, le=3600, description="Max time to wait (default 300s)")


# =============================================================================
# Loop state
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


# =============================================================================
# Tool provider
# =============================================================================


def create_tool_provider(
    state: LoopState, eval_client: CriticRunClient, critic_model: str, db: Database
) -> DirectToolProvider:
    """Create tool provider with shared tools (no submit)."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, running tests, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def report_success() -> None:
        """Report that the task completed successfully. Signals exit with success status."""
        state.status = LoopStatus.EXITED_SUCCESS
        logger.info("Reported success")

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> None:
        """Report that the task could not be completed.

        Use when there are blocking issues (e.g., no viable path forward).
        Signals exit with failure status.
        """
        state.status = LoopStatus.EXITED_FAILURE
        logger.info("Reported failure: %s", args.message)

    @provider.tool
    async def run_critic(args: RunCriticToolArgs) -> RunCriticResponse:
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
        return response

    @provider.tool
    async def wait_until_graded_tool(args: WaitUntilGradedToolArgs) -> GradingStatusResponse:
        """Wait for a critic run to be fully graded.

        Polls the database directly until grading is complete or timeout.
        """
        logger.info(f"Waiting for grading: {args.critic_run_id}")
        response = await wait_until_graded(
            args.critic_run_id, db, timeout_seconds=args.timeout_seconds, poll_interval_seconds=5
        )
        logger.info(f"Grading complete: total_credit={response.total_credit}, max_credit={response.max_credit}")
        return response

    return provider
