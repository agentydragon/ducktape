"""Evaluation API routes - critic evaluation orchestration for PO/PI agents.

These endpoints replace the MCP-based PromptEvalServer, providing HTTP REST
endpoints that agents can call directly.

Endpoints:
- POST /api/eval/run_critic - Run a critic agent on an example
- POST /api/eval/wait_until_graded - Wait for grading to complete

Access control:
- Admin (localhost or postgres creds): Full access
- PO/PI agents: Full access to these endpoints
- Other agents: No access
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from props.backend.auth import AuthContext, get_auth_context
from props.core.exceptions import AgentDidNotSubmitError
from props.core.ids import DefinitionId
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample
from props.core.splits import Split
from props.critic.exceptions import CriticExecutionError
from props.critic_dev.shared import TargetMetric
from props.db.examples import Example
from props.db.models import AgentDefinition, AgentRun, AgentRunStatus, AgentType, GradingEdge, GradingPending, Snapshot
from props.db.session import get_session

if TYPE_CHECKING:
    from props.core.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Request/Response models
# =============================================================================


class RunCriticRequest(BaseModel):
    """Request to run a critic agent."""

    definition_id: DefinitionId = Field(
        description="Agent package ID (from 'props agent-pkg create' or 'critic' for baseline)"
    )
    example: ExampleSpec = Field(description="Example to evaluate (WholeSnapshotExample or SingleFileSetExample)")
    timeout_seconds: int = Field(default=3600, description="Max seconds before container is killed")
    budget_usd: float | None = Field(default=None, description="Max USD cost for this agent (enforced by proxy)")
    critic_model: str = Field(default="gpt-5.1-codex-mini", description="Model for the critic agent")
    target_metric: TargetMetric = Field(
        default=TargetMetric.WHOLE_REPO, description="Target metric mode for validation rules"
    )


class RunCriticResponse(BaseModel):
    """Response from running a critic agent."""

    critic_run_id: UUID = Field(
        description="agent_run_id of the critic agent run. Query agent_runs for output, costs, model."
    )
    status: AgentRunStatus = Field(description="Final status of the critic run")
    message: str = Field(description="Status message or error details")


class WaitUntilGradedRequest(BaseModel):
    """Request to wait for grading completion."""

    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(
        default=300, ge=10, le=3600, description="Maximum time to wait for grading (default 300s, max 1 hour)"
    )
    poll_interval_seconds: int = Field(
        default=5, ge=1, le=60, description="How often to check for grading completion (default 5s)"
    )


class WaitUntilGradedResponse(BaseModel):
    """Response from waiting for grading."""

    grader_run_id: UUID = Field(description="agent_run_id of the grader run that graded this critic")
    total_credit: float = Field(description="Sum of credits for TP matches (recall numerator)")
    max_credit: int = Field(description="Number of distinct TP occurrences (recall denominator)")
    message: str = Field(description="Query advice for getting aggregate metrics")


# =============================================================================
# Helper functions
# =============================================================================


_AGENT_STUCK_ADVICE = (
    "Agent exceeded turn limit. This could mean:\n"
    "  1. Agent needed more turns to complete the task (reading files, analyzing code, etc.)\n"
    "  2. Agent stuck in a loop or not following instructions\n"
    "  3. Agent ran out of tokens\n"
    "Check the transcript in the database to determine if the agent was making productive progress or stuck."
)

_VALIDATION_FUNCTION_NAME = "get_validation_run_aggregates()"

_FUNCTION_BASED_METRICS_ADVICE = (
    f"To get recall metrics, call the {_VALIDATION_FUNCTION_NAME} SQL function. "
    "This function returns per-run aggregate metrics (total_credit, n_occurrences per run). "
    "You must aggregate across runs manually if needed."
)

_VIEW_BASED_METRICS_ADVICE = (
    "To get recall metrics, query the recall_by_definition_split_kind or recall_by_example views. "
    "These views pre-aggregate occurrence-level credits across multiple runs and include stats (n_examples, n_runs, ucb, lcb)."
)


def _get_eval_auth_context(request: Request) -> tuple[UUID | None, bool]:
    """Get and validate auth context for eval endpoints.

    Returns (parent_run_id, is_authorized) - parent_run_id is set for agent callers.
    Raises HTTPException if not authorized.
    """
    auth: AuthContext = get_auth_context(request)

    # Check for auth errors
    if auth.error:
        raise HTTPException(status_code=401, detail=auth.error)

    # Allow admin access (localhost or postgres admin user)
    if auth.is_admin:
        return None, True

    # Require authentication for agents
    if not auth.is_authenticated or auth.agent_run_id is None:
        raise HTTPException(status_code=401, detail="Authorization required")

    # Check agent type - only PO and PI can use eval endpoints
    with get_session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Agent run not found")

        agent_type = agent_run.type_config.agent_type
        if agent_type not in {AgentType.PROMPT_OPTIMIZER, AgentType.IMPROVEMENT}:
            raise HTTPException(status_code=403, detail=f"Agent type {agent_type} not allowed to access eval endpoints")

        return auth.agent_run_id, True


def _trace_advice_for_run(run_id: UUID, is_grader: bool = False) -> str:
    """Generate trace query advice when we have a concrete run_id."""
    agent_type = "Grader" if is_grader else "Critic"
    return f"""{agent_type} agent run ID: {run_id}

Query examples:
-- Get run details:
SELECT * FROM agent_runs WHERE agent_run_id = '{run_id}';

-- Get execution trace:
SELECT event_type, payload FROM events WHERE agent_run_id = '{run_id}' ORDER BY sequence_num;

-- Get reasoning summaries:
SELECT payload FROM events WHERE agent_run_id = '{run_id}' AND event_type = 'reasoning' ORDER BY sequence_num;"""


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

    Returns critic_run_id for subsequent grading via wait_until_graded.
    """
    parent_run_id, _ = _get_eval_auth_context(request)
    registry = get_registry(request)

    # Validate definition exists
    with get_session() as session:
        definition = session.get(AgentDefinition, body.definition_id)
        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Agent definition not found: {body.definition_id}. "
                f"Use CLI: props agent-pkg create /workspace/my_critic/",
            )

        # Load and validate snapshot
        snapshot_slug = body.example.snapshot_slug
        db_snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one_or_none()
        if not db_snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_slug} not found")

        # Validate split-based access restrictions
        if db_snapshot.split == Split.TEST:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: 'test' split is completely off-limits. "
                f"You can only run evaluations on 'train' and 'valid' splits. "
                f"Snapshot {snapshot_slug} is in 'test' split.",
            )

        # Look up example from database to validate it exists
        example = Example.from_spec_or_none(session, body.example)

        if not example:
            # List available examples for this snapshot
            available = session.query(Example).filter_by(snapshot_slug=snapshot_slug).all()
            example_list = "\n".join(
                f"  - kind={ex.example_kind.value}, files_hash={ex.files_hash}" for ex in available[:10]
            )
            if len(available) > 10:
                example_list += f"\n  ... and {len(available) - 10} more"

            raise HTTPException(
                status_code=404,
                detail=f"No example found matching {body.example.model_dump()} "
                f"in snapshot {snapshot_slug}.\n"
                f"Available examples ({len(available)} total):\n{example_list}\n\n"
                f"Query the examples table to find valid examples:\n"
                f"SELECT snapshot_slug, example_kind, files_hash FROM examples WHERE snapshot_slug='{snapshot_slug}';",
            )

        # Check if this is a per-file example (SingleFileSetExample) or whole-snapshot (WholeSnapshotExample)
        is_per_file = isinstance(body.example, SingleFileSetExample)

        # Check VALID scope restrictions based on target metric mode
        if db_snapshot.split == Split.VALID and is_per_file and body.target_metric == TargetMetric.WHOLE_REPO:
            # Access files_hash only for SingleFileSetExample (type narrowing)
            assert isinstance(body.example, SingleFileSetExample)
            raise HTTPException(
                status_code=400,
                detail=f"valid split in whole-repo mode requires whole-snapshot examples only. "
                f"You requested a file_set example (files_hash={body.example.files_hash}). "
                f"Query for whole-snapshot examples: "
                f"SELECT snapshot_slug, example_kind, files_hash FROM examples "
                f"WHERE snapshot_slug='{snapshot_slug}' AND example_kind='whole_snapshot';",
            )

    # Execute critic run using registry
    try:
        critic_run_id = await registry.run_critic(
            image_ref=body.definition_id,  # definition_id is actually an image ref
            example=body.example,
            model=body.critic_model,
            timeout_seconds=body.timeout_seconds,
            parent_run_id=parent_run_id,
            budget_usd=body.budget_usd,
        )
    except CriticExecutionError as e:
        raise HTTPException(
            status_code=500, detail=f"Critic agent failed during execution: {e}\n\nSnapshot: {snapshot_slug}"
        )
    except AgentDidNotSubmitError as e:
        raise HTTPException(
            status_code=500, detail=f"{e}\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(e.agent_run_id)}"
        )

    # Check status to provide specific error messages
    with get_session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        status = critic_run.status

    if status == AgentRunStatus.MAX_TURNS_EXCEEDED:
        return RunCriticResponse(
            critic_run_id=critic_run_id,
            status=status,
            message=f"Critic agent exceeded maximum turns.\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(critic_run_id)}",
        )
    if status == AgentRunStatus.CONTEXT_LENGTH_EXCEEDED:
        return RunCriticResponse(
            critic_run_id=critic_run_id,
            status=status,
            message=f"Critic agent exceeded context length.\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(critic_run_id)}",
        )

    return RunCriticResponse(
        critic_run_id=critic_run_id,
        status=status,
        message="Critic completed successfully. Use wait_until_graded to get results.",
    )


@router.post("/wait_until_graded")
async def wait_until_graded(request: Request, body: WaitUntilGradedRequest) -> WaitUntilGradedResponse:
    """Wait for a critic run to be fully graded (no remaining drift).

    Polls the grading_pending view until there are no remaining edges
    for the critic run, then returns the grading results.

    A critique is "graded" when all (issue, GT_occurrence) pairs have
    corresponding grading edges - not just when a grader run exists.
    This properly handles multiple grader daemons contributing edges.
    """
    _get_eval_auth_context(request)  # Just validate auth

    start_time = time.monotonic()
    deadline = start_time + body.timeout_seconds
    last_pending_count: int | None = None

    while time.monotonic() < deadline:
        with get_session() as session:
            # Check for remaining drift using grading_pending view
            pending_count = (
                session.query(func.count())
                .select_from(GradingPending)
                .filter(GradingPending.critique_run_id == body.critic_run_id)
                .scalar()
                or 0
            )

            if pending_count == 0:
                # No drift - critique is fully graded
                # Get the critic run to determine split and example type
                critic_run = session.get(AgentRun, body.critic_run_id)
                if not critic_run:
                    raise HTTPException(status_code=404, detail=f"Critic run {body.critic_run_id} not found")

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
                    .filter(GradingEdge.critique_run_id == body.critic_run_id)
                    .filter(GradingEdge.tp_id.isnot(None))
                    .scalar()
                    or 0.0
                )

                max_credit = (
                    session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
                    .filter(GradingEdge.critique_run_id == body.critic_run_id)
                    .filter(GradingEdge.tp_id.isnot(None))
                    .distinct()
                    .count()
                )

                # Find the grader run(s) that contributed edges
                grader_run_ids = (
                    session.query(GradingEdge.grader_run_id)
                    .filter(GradingEdge.critique_run_id == body.critic_run_id)
                    .distinct()
                    .all()
                )
                # Use the first grader run ID for the response (usually there's only one)
                grader_run_id = grader_run_ids[0][0] if grader_run_ids else body.critic_run_id

                # Build query advice based on split and scope
                # Note: target_metric is not stored per-run, so we use a generic message
                if split == Split.VALID and scope_kind == ExampleKind.WHOLE_SNAPSHOT:
                    query_advice = (
                        f"{_FUNCTION_BASED_METRICS_ADVICE} "
                        f"Example: SELECT * FROM {_VALIDATION_FUNCTION_NAME} WHERE critique_run_id = '{body.critic_run_id}';"
                    )
                else:
                    query_advice = (
                        f"{_VIEW_BASED_METRICS_ADVICE} "
                        "Example: SELECT recall_stats FROM recall_by_definition_split_kind WHERE critic_image_digest ='...';"
                    )

                return WaitUntilGradedResponse(
                    grader_run_id=grader_run_id,
                    total_credit=float(total_credit),
                    max_credit=max_credit,
                    message=query_advice,
                )

            # Log progress if pending count changed
            if last_pending_count != pending_count:
                logger.debug(f"Waiting for grading: {pending_count} edges pending for {body.critic_run_id}")
                last_pending_count = pending_count

        # Not ready yet - wait before polling again
        await asyncio.sleep(body.poll_interval_seconds)

    # Timeout reached
    raise HTTPException(
        status_code=408,
        detail=f"Timeout waiting for critic run {body.critic_run_id} to be graded. "
        f"Waited {body.timeout_seconds} seconds, {last_pending_count} edges still pending. "
        "Check if the grader daemon is running and processing critic runs.",
    )
