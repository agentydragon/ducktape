"""Shared agent loop infrastructure for critic developer agents.

Contains tool argument models, loop state, tool provider factory, and handlers
used by both optimize and improve modes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import Field

from agent_core.direct_provider import DirectToolProvider
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.agents.critic_dev.grading import wait_until_graded
from props.core.eval_api_models import CriticRunStatus, GradingStatusResponse, RunCriticRequest, StartCriticResponse
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.db.snapshot_io import fetch_snapshot_to_path

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to optimize prompts. Do not output text directly. "
    "Use start_critic, wait_until_critic_completed, and wait_until_graded to evaluate agents, "
    "then keep iterating."
)

# Default workspace path
WORKSPACE = Path("/workspace")


# =============================================================================
# Tool argument models
# =============================================================================


class ReportFailureArgs(OpenAIStrictModeBaseModel):
    """Arguments for report_failure tool."""

    message: str = Field(..., description="Description of why optimization could not be completed")


class WaitUntilCriticCompletedArgs(OpenAIStrictModeBaseModel):
    """Arguments for wait_until_critic_completed tool."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for")
    timeout_seconds: int = Field(ge=1, le=600, description="Max time to wait in seconds (1-600)")


class WaitUntilGradedToolArgs(OpenAIStrictModeBaseModel):
    """Arguments for wait_until_graded tool."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(default=300, ge=10, le=3600, description="Max time to wait (default 300s)")


class FetchSnapshotArgs(OpenAIStrictModeBaseModel):
    """Arguments for fetch_snapshot tool."""

    snapshot_slug: str = Field(description="Snapshot slug (e.g., 'ducktape/2025-11-26-00')")
    path: str = Field(default="/workspace", description="Directory to extract snapshot into")


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


def create_tool_provider(state: LoopState, http_client: httpx.AsyncClient, db: Database) -> DirectToolProvider:
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
    async def start_critic(args: RunCriticRequest) -> StartCriticResponse:
        """Start a critic agent on an example. Returns immediately with critic_run_id.

        After calling this, use wait_until_critic_completed to wait for the critic to
        finish, then use wait_until_graded to wait for grading results.
        """
        logger.info(f"Starting critic: definition={args.definition_id}, example={args.example}")
        resp = await http_client.post("/api/runs/critic", json=args.model_dump(mode="json"))
        resp.raise_for_status()
        response = StartCriticResponse.model_validate(resp.json())
        logger.info(f"Critic started: {response.critic_run_id}")
        return response

    @provider.tool
    async def wait_until_critic_completed(args: WaitUntilCriticCompletedArgs) -> CriticRunStatus:
        """Wait until a critic run has exited or timed out.

        Polls the database until the critic run status is no longer IN_PROGRESS.
        Call start_critic first, then this tool, then wait_until_graded.

        Raises TimeoutError if the critic does not complete within timeout_seconds.
        """
        logger.info(f"Waiting for critic to complete: {args.critic_run_id}")
        deadline = time.monotonic() + args.timeout_seconds
        poll_interval = 5.0

        while time.monotonic() < deadline:
            with db.session() as session:
                run = session.get(AgentRun, args.critic_run_id)
                if run is None:
                    raise ValueError(f"Critic run {args.critic_run_id} not found")
                if run.status != AgentRunStatus.IN_PROGRESS:
                    logger.info(f"Critic completed: {args.critic_run_id}, status={run.status}")
                    return CriticRunStatus(
                        critic_run_id=args.critic_run_id, status=run.status, container_exit_code=run.container_exit_code
                    )
            await asyncio.sleep(poll_interval)

        raise TimeoutError(f"Critic run {args.critic_run_id} did not complete within {args.timeout_seconds}s")

    @provider.tool
    async def wait_until_graded_tool(args: WaitUntilGradedToolArgs) -> GradingStatusResponse:
        """Wait for a critic run to be fully graded.

        Polls the database directly until grading is complete or timeout.
        The critic run must have already completed (use wait_until_critic_completed first).
        """
        logger.info(f"Waiting for grading: {args.critic_run_id}")
        response = await wait_until_graded(
            args.critic_run_id, db, timeout_seconds=args.timeout_seconds, poll_interval_seconds=5
        )
        logger.info(f"Grading complete: total_credit={response.total_credit}, max_credit={response.max_credit}")
        return response

    @provider.tool
    def fetch_snapshot(args: FetchSnapshotArgs) -> str:
        """Fetch a snapshot from the database and extract it to a local directory.

        Use this to inspect snapshot source code and ground truth context.
        """
        dest = Path(args.path)
        fetch_snapshot_to_path(args.snapshot_slug, dest, db)
        logger.info("Fetched snapshot %s to %s", args.snapshot_slug, dest)
        return f"Fetched snapshot {args.snapshot_slug} to {dest}"

    return provider
