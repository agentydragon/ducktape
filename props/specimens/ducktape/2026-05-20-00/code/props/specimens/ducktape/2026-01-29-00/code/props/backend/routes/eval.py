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

Response models are shared between backend and agent containers via props.core.eval_api_models.
Polling/waiting logic is implemented client-side in the agent containers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func

from props.backend.auth import CallerType, require_eval_api_access
from props.core.eval_api_models import GradingStatusResponse, RunCriticRequest, RunCriticResponse
from props.core.exceptions import AgentDidNotSubmitError
from props.core.splits import Split
from props.critic.exceptions import CriticExecutionError
from props.db.examples import Example
from props.db.models import AgentDefinition, AgentRun, GradingEdge, GradingPending, Snapshot
from props.db.session import get_session

if TYPE_CHECKING:
    from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Helper functions
# =============================================================================


def get_registry(request: Request) -> AgentRegistry:
    """Get registry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/run_critic")
async def run_critic(
    request: Request, body: RunCriticRequest, auth: tuple[CallerType, UUID | None] = Depends(require_eval_api_access)
) -> RunCriticResponse:
    """Run critic agent using an agent package.

    Loads critic package from database and runs the /init script to get
    the system prompt, then runs the critic on the specified example.

    Validates split-based access restrictions:
    - TRAIN split: all example types allowed
    - VALID split: restrictions depend on target_metric mode
    - TEST split: completely off-limits

    Returns critic_run_id. Use GET /grading_status/{critic_run_id} to poll for results.
    """
    _, parent_run_id = auth
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
async def get_grading_status(
    critic_run_id: UUID, auth: tuple[CallerType, UUID | None] = Depends(require_eval_api_access)
) -> GradingStatusResponse:
    """Check grading status for a critic run (non-blocking).

    Returns immediately with current grading status. If is_complete=False,
    the client should poll again after a delay (e.g., 5 seconds).

    A critique is "graded" when all (issue, GT_occurrence) pairs have
    corresponding grading edges - not just when a grader run exists.
    """
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
