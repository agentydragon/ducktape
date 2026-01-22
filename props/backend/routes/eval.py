"""Evaluation API routes - critic evaluation orchestration for PO/PI agents.

These endpoints replace the MCP-based PromptEvalServer, providing HTTP REST
endpoints that agents can call directly.

Endpoints:
- POST /api/eval/run_critic - Run a critic agent on an example
- GET /api/eval/grading_status/{critic_run_id} - Check grading status (non-blocking)

Access control:
- Admin (localhost or postgres creds): Full access
- PO/PI agents: Full access to these endpoints
- Other agents: No access

Response models are shared between backend and agent containers via props.core.eval_api.
Polling/waiting logic is implemented client-side in the agent containers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from props.backend.auth import ACL_CAN_USE_EVAL_API, get_auth_context, get_caller_type
from props.core.exceptions import AgentDidNotSubmitError
from props.core.ids import DefinitionId
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample
from props.core.splits import Split
from props.critic.exceptions import CriticExecutionError
from props.critic_dev.shared import TargetMetric
from props.db.examples import Example
from props.db.models import AgentDefinition, AgentRun, AgentRunStatus, GradingEdge, GradingPending, Snapshot
from props.db.session import get_session

if TYPE_CHECKING:
    from props.core.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Request/Response models (shared with agent containers)
# =============================================================================


class RunCriticRequest(BaseModel):
    """Request to run a critic agent."""

    definition_id: DefinitionId = Field(description="Agent package ID (e.g., 'critic' or a digest)")
    example: ExampleSpec = Field(description="Example to evaluate")
    timeout_seconds: int = Field(default=3600, description="Max seconds before container is killed")
    budget_usd: float | None = Field(default=None, description="Max USD cost for this agent")
    critic_model: str = Field(default="gpt-5.1-codex-mini", description="Model for the critic agent")
    target_metric: TargetMetric = Field(default=TargetMetric.WHOLE_REPO, description="Target metric mode")


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


# =============================================================================
# Helper functions
# =============================================================================


def _get_eval_auth_context(request: Request) -> UUID | None:
    """Get and validate auth context for eval endpoints.

    Returns parent_run_id for agent callers (None for admin).
    Raises HTTPException if not authorized.
    """
    auth = get_auth_context(request)
    caller_type, agent_run_id = get_caller_type(auth)

    # Check if caller type is allowed to use eval API
    if caller_type not in ACL_CAN_USE_EVAL_API:
        raise HTTPException(status_code=403, detail=f"Caller type {caller_type} not allowed to access eval endpoints")

    return agent_run_id


def get_registry(request: Request) -> AgentRegistry:
    """Get registry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/run_critic")
async def run_critic(request: Request, body: RunCriticRequest) -> RunCriticResponse:
    """Run critic agent using an agent package.

    Loads critic package from database and runs the /init script to get
    the system prompt, then runs the critic on the specified example.

    Validates split-based access restrictions:
    - TRAIN split: all example types allowed
    - VALID split: restrictions depend on target_metric mode
    - TEST split: completely off-limits

    Returns critic_run_id. Use GET /grading_status/{critic_run_id} to poll for results.
    """
    parent_run_id = _get_eval_auth_context(request)
    registry = get_registry(request)

    # Validate definition exists
    with get_session() as session:
        definition = session.get(AgentDefinition, body.definition_id)
        if not definition:
            raise HTTPException(status_code=404, detail=f"Agent definition not found: {body.definition_id}")

        # Load and validate snapshot
        snapshot_slug = body.example.snapshot_slug
        db_snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one_or_none()
        if not db_snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_slug} not found")

        # Validate split-based access restrictions
        if db_snapshot.split == Split.TEST:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: test split is off-limits. Snapshot {snapshot_slug} is in test split.",
            )

        # Look up example from database to validate it exists
        example = Example.from_spec_or_none(session, body.example)

        if not example:
            raise HTTPException(status_code=404, detail=f"Example not found: {body.example.model_dump()}")

        # Check if this is a per-file example (SingleFileSetExample) or whole-snapshot (WholeSnapshotExample)
        is_per_file = isinstance(body.example, SingleFileSetExample)

        # Check VALID scope restrictions based on target metric mode
        if db_snapshot.split == Split.VALID and is_per_file and body.target_metric == TargetMetric.WHOLE_REPO:
            assert isinstance(body.example, SingleFileSetExample)
            raise HTTPException(
                status_code=400, detail="Valid split in whole-repo mode requires whole-snapshot examples only"
            )

    # Execute critic run using registry
    try:
        critic_run_id = await registry.run_critic(
            image_ref=body.definition_id,
            example=body.example,
            model=body.critic_model,
            timeout_seconds=body.timeout_seconds,
            parent_run_id=parent_run_id,
            budget_usd=body.budget_usd,
        )
    except CriticExecutionError as e:
        raise HTTPException(status_code=500, detail=f"Critic execution failed: {e}")
    except AgentDidNotSubmitError as e:
        raise HTTPException(status_code=500, detail=f"Agent did not submit: {e}")

    # Get final status
    with get_session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        status = critic_run.status

    return RunCriticResponse(critic_run_id=critic_run_id, status=status)


@router.get("/grading_status/{critic_run_id}")
async def get_grading_status(request: Request, critic_run_id: UUID) -> GradingStatusResponse:
    """Check grading status for a critic run (non-blocking).

    Returns immediately with current grading status. If is_complete=False,
    the client should poll again after a delay (e.g., 5 seconds).

    A critique is "graded" when all (issue, GT_occurrence) pairs have
    corresponding grading edges - not just when a grader run exists.
    """
    _get_eval_auth_context(request)  # Validate auth

    with get_session() as session:
        # Check for remaining drift using grading_pending view
        pending_count = (
            session.query(func.count())
            .select_from(GradingPending)
            .filter(GradingPending.critique_run_id == critic_run_id)
            .scalar()
            or 0
        )

        if pending_count > 0:
            # Not complete yet - return partial status
            return GradingStatusResponse(is_complete=False, pending_count=pending_count)

        # No drift - critique is fully graded
        critic_run = session.get(AgentRun, critic_run_id)
        if not critic_run:
            raise HTTPException(status_code=404, detail=f"Critic run {critic_run_id} not found")

        critic_config = critic_run.critic_config()
        example_spec = critic_config.example
        snapshot_slug = example_spec.snapshot_slug
        snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
        split = snapshot.split

        # Find matching example to check scope kind
        example = Example.from_spec(session, example_spec)
        scope_kind = example.example_kind

        # Compute grading metrics from edges for this critique
        total_credit = (
            session.query(func.sum(GradingEdge.credit))
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .scalar()
            or 0.0
        )

        max_credit = (
            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .distinct()
            .count()
        )

        # Find the grader run(s) that contributed edges
        grader_run_ids = (
            session.query(GradingEdge.grader_run_id)
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .distinct()
            .all()
        )
        # Use the first grader run ID for the response (usually there's only one)
        grader_run_id = grader_run_ids[0][0] if grader_run_ids else critic_run_id

        return GradingStatusResponse(
            is_complete=True,
            pending_count=0,
            grader_run_id=grader_run_id,
            total_credit=float(total_credit),
            max_credit=max_credit,
            split=split,
            example_kind=scope_kind,
        )
