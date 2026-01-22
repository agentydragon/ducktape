"""Shared models for the eval API.

These models are used by both:
- Backend routes (props/backend/routes/eval.py)
- Container agents (props/critic_dev/optimize/main.py, props/critic_dev/improve/main.py)

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

    definition_id: DefinitionId = Field(description="Agent package ID (e.g., 'critic' or a digest)")
    example: ExampleSpec = Field(description="Example to evaluate")
    timeout_seconds: int = Field(default=3600, description="Max seconds before container is killed")
    budget_usd: float | None = Field(default=None, description="Max USD cost for this agent")
    critic_model: str = Field(default="gpt-5.1-codex-mini", description="Model for the critic agent")


# =============================================================================
# Response models
# =============================================================================


class RunCriticResponse(BaseModel):
    """Response from running a critic agent."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic agent run")
    status: AgentRunStatus = Field(description="Final status of the critic run")


class GradingStatusResponse(BaseModel):
    """Response with grading status for a critic run.

    If is_complete=False, the client should poll again after a delay.
    If is_complete=True, the grading results are included.
    """

    is_complete: bool = Field(description="True if grading is complete (no pending edges)")
    pending_count: int = Field(description="Number of grading edges still pending")

    # Fields below are only populated when is_complete=True
    grader_run_id: UUID | None = Field(default=None, description="agent_run_id of the grader run")
    total_credit: float | None = Field(default=None, description="Sum of credits for TP matches")
    max_credit: int | None = Field(default=None, description="Number of distinct TP occurrences")
    split: Split | None = Field(default=None, description="Data split of the evaluated example")
    example_kind: ExampleKind | None = Field(default=None, description="Kind of example")
