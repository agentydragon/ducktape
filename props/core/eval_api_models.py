"""Shared models for critic run and grading APIs.

These models are used by both:
- Backend routes (props/backend/routes/runs.py — critic run endpoints)
- Container agents (props/agents/critic_dev/ — eval client)

This ensures consistent serialization/deserialization between backend and containers.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from props.core.ids import DefinitionId
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.splits import Split
from props.db.models import AgentRunStatus

# =============================================================================
# Request models
# =============================================================================


class RunCriticRequest(BaseModel):
    """Request to run a critic agent."""

    definition_id: DefinitionId = Field(description="Image ref: OCI digest (sha256:...) or tag (e.g., 'latest')")
    example: ExampleSpec = Field(description="Example to evaluate")
    timeout_seconds: int = Field(gt=0, description="Max seconds before container is killed")
    budget_usd: float = Field(gt=0, description="Max USD cost for this agent")
    critic_model: str = Field(description="Model for the critic agent")


# =============================================================================
# Response models
# =============================================================================


class RunCriticResponse(BaseModel):
    """Response from running a critic agent."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic agent run")
    status: AgentRunStatus = Field(description="Final status of the critic run")
    container_exit_code: int | None = Field(
        default=None,
        description="Container exit code (0=success). Only present when status is EXITED.",
    )


class GradingStatusResponse(BaseModel):
    """Response with grading status for a critic run.

    If is_complete=False, the client should poll again after a delay.
    If is_complete=True, the grading results are included.
    """

    is_complete: bool = Field(description="True if grading is complete (no pending edges)")
    pending_count: int = Field(description="Number of grading edges still pending")

    # Fields below are only populated when is_complete=True
    grader_run_ids: list[UUID] = Field(default_factory=list, description="agent_run_ids of grader runs")
    total_credit: float | None = Field(default=None, description="Sum of credits for TP matches")
    max_credit: int | None = Field(default=None, description="Number of distinct TP occurrences (recall denominator)")
    split: Split | None = Field(default=None, description="Data split of the evaluated example")
    example_kind: ExampleKind | None = Field(default=None, description="Kind of example")
