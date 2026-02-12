"""Runs API routes for triggering and monitoring agent runs.

Read endpoints use agent credential passthrough - RLS policies filter results
based on the caller's database role. Write endpoints (validation triggers)
require admin access. Critic run endpoint (POST /critic) requires critic_run_access ACL.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func

from props.backend.auth import (
    AgentDb,
    CallerType,
    parse_credentials,
    require_admin_access,
    require_critic_run_access,
    validate_postgres_credentials,
)
from props.backend.deps import AdminDb
from props.backend.routes.ground_truth import get_snapshot_or_404
from props.core.agent_types import AgentType, CriticTypeConfig, TargetMetric, TypeConfig
from props.core.eval_api_models import RunCriticRequest, RunCriticResponse
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.oci_utils import BUILTIN_TAG
from props.core.splits import Split
from props.db.database import Database
from props.db.examples import Example
from props.db.models import (
    AgentRun,
    AgentRunStatus,
    FileSetMember,
    GradingEdge,
    GradingTarget,
    LLMRequest,
    LLMRunCost,
    RecallByDefinitionSplitKind,
    Snapshot,
)
from props.db.query_builders import query_recall_by_example
from props.db.snapshots import LocationAnchor
from props.orchestration.agent_registry import AgentRegistry, BudgetExceededError, ImageResolutionError

router = APIRouter()
logger = logging.getLogger(__name__)


# --- Enums ---


class JobStatus(StrEnum):
    """Validation job status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# --- Models ---


class ActiveRunInfo(BaseModel):
    agent_run_id: UUID
    image_digest: str
    model: str
    status: AgentRunStatus
    created_at: datetime

    @classmethod
    def from_db(cls, run: AgentRun) -> ActiveRunInfo:
        return cls(
            agent_run_id=run.agent_run_id,
            image_digest=run.image_digest,
            model=run.model,
            status=run.status,
            created_at=run.created_at,
        )


class ActiveRunsResponse(BaseModel):
    runs: list[ActiveRunInfo]


class ValidationRunRequest(BaseModel):
    image_digest: str
    example_kind: ExampleKind
    split: Split = Split.VALID
    n_samples: int = Field(ge=1, le=50, default=5)
    critic_model: str
    budget_usd: float = Field(ge=0, description="Max USD cost per critic agent")


class ValidationRunResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    n_examples_sampled: int
    message: str


class JobInfo(BaseModel):
    """Information about a validation job."""

    job_id: UUID
    image_digest: str
    example_kind: ExampleKind
    n_samples: int
    status: JobStatus
    completed: int
    failed: int


class JobsResponse(BaseModel):
    """Response for jobs endpoint."""

    jobs: list[JobInfo]


class ChildRunInfo(BaseModel):
    """Brief info about a child agent run."""

    agent_run_id: UUID
    agent_type: AgentType
    status: AgentRunStatus


class GraderRunInfo(BaseModel):
    """Info about a grader run that graded this critic."""

    agent_run_id: UUID
    status: AgentRunStatus
    grading_edges: list[GradingEdgeInfo] = Field(description="This grader's output edges")


class GradingEdgeInfo(BaseModel):
    """Individual grading edge for API response."""

    critique_issue_id: str
    target: GradingTarget
    rationale: str


class ReportedIssueOccurrenceInfo(BaseModel):
    """Occurrence of a reported issue."""

    occurrence_id: int
    note: str | None
    locations: list[LocationAnchor]


class ReportedIssueInfo(BaseModel):
    """Issue reported by a critic run."""

    issue_id: str
    rationale: str
    occurrences: list[ReportedIssueOccurrenceInfo]


# Type-specific details (only fields unique to each agent type)


class CriticRunSpecifics(BaseModel):
    """Critic-specific fields."""

    agent_type: Literal[AgentType.CRITIC] = AgentType.CRITIC
    resolved_files: list[str] | None = Field(description="Resolved file paths for file_set examples")
    grader_runs: list[GraderRunInfo] = Field(description="Grader runs with their edges nested")
    reported_issues: list[ReportedIssueInfo] = Field(description="Issues found by the critic")


class GraderRunSpecifics(BaseModel):
    """Grader-specific fields."""

    agent_type: Literal[AgentType.GRADER] = AgentType.GRADER
    grading_edges: list[GradingEdgeInfo] = Field(description="Output edges from this grader")


class OtherRunSpecifics(BaseModel):
    """Other agent types have no specific fields."""

    agent_type: Literal[AgentType.CRITIC_DEV_OPTIMIZE, AgentType.CRITIC_DEV_IMPROVE, AgentType.FREEFORM]


RunSpecifics = Annotated[CriticRunSpecifics | GraderRunSpecifics | OtherRunSpecifics, Field(discriminator="agent_type")]


class LLMCostStats(BaseModel):
    """LLM cost stats — used both per-model and as aggregate totals."""

    requests: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float

    @staticmethod
    def aggregate(by_model: dict[str, LLMCostStats]) -> LLMCostStats:
        return LLMCostStats(
            requests=sum(s.requests for s in by_model.values()),
            input_tokens=sum(s.input_tokens for s in by_model.values()),
            cached_tokens=sum(s.cached_tokens for s in by_model.values()),
            output_tokens=sum(s.output_tokens for s in by_model.values()),
            cost_usd=sum(s.cost_usd for s in by_model.values()),
        )


class LLMCostSummary(BaseModel):
    """Aggregated LLM cost summary for an agent run, with per-model breakdown."""

    totals: LLMCostStats
    by_model: dict[str, LLMCostStats]


class AgentRunDetail(BaseModel):
    """Detailed view of an agent run with type-specific details nested."""

    # Common fields for all agent types
    agent_run_id: UUID
    image_digest: str
    parent_agent_run_id: UUID | None
    model: str
    status: AgentRunStatus
    budget_usd: float
    container_exit_code: int | None
    created_at: datetime
    updated_at: datetime
    type_config: TypeConfig
    llm_call_count: int
    child_runs: list[ChildRunInfo]

    # Container output (captured after container exits)
    container_stdout: str | None
    container_stderr: str | None

    # LLM costs aggregated for this run
    llm_costs: LLMCostSummary | None

    # Type-specific details (discriminated union)
    details: RunSpecifics


class RunInfo(BaseModel):
    """Run information for list view."""

    agent_run_id: UUID
    image_digest: str
    type_config: TypeConfig
    model: str
    status: AgentRunStatus
    created_at: datetime
    updated_at: datetime
    split: Split | None = None


class RunsListResponse(BaseModel):
    """Response for paginated runs list."""

    runs: list[RunInfo]
    total_count: int
    offset: int
    limit: int


# --- LLM Requests Models ---


class LLMRequestInfo(BaseModel):
    """LLM request information for API response.

    Directly mirrors LLMRequest ORM model fields.
    """

    model_config = {"from_attributes": True}

    id: int
    model: str
    request_body: dict
    response_body: dict | None
    error: str | None
    latency_ms: int | None
    created_at: datetime


class LLMRequestsResponse(BaseModel):
    """Response for LLM requests list."""

    requests: list[LLMRequestInfo]


# --- Job Tracking ---


@dataclass
class ValidationJob:
    """Tracks a validation batch job."""

    job_id: UUID
    image_digest: str
    example_kind: ExampleKind
    n_samples: int
    critic_model: str
    budget_usd: float
    status: JobStatus = JobStatus.RUNNING
    completed: int = 0
    failed: int = 0
    task: asyncio.Task | None = None
    examples: list[ExampleSpec] = field(default_factory=list)


# In-memory job tracking (jobs are transient, not persisted)
_jobs: dict[UUID, ValidationJob] = {}


# --- Helpers ---


def get_registry(request: Request) -> AgentRegistry:
    """Get registry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


# --- Helper functions ---


def edges_to_info(edges: list[GradingEdge]) -> list[GradingEdgeInfo]:
    """Convert GradingEdge ORM objects to API info objects."""
    return [
        GradingEdgeInfo(critique_issue_id=edge.critique_issue_id, target=edge.to_target(), rationale=edge.rationale)
        for edge in edges
    ]


# --- Endpoints ---


@router.get("/active")
def list_active_runs(request: Request, agent_db: AgentDb) -> ActiveRunsResponse:
    """List all active agent runs.

    Queries database for runs with IN_PROGRESS status.
    RLS policies filter visible runs based on caller's database role.
    """
    with agent_db.session() as session:
        db_runs = (
            session.query(AgentRun)
            .filter(AgentRun.status == AgentRunStatus.IN_PROGRESS)
            .order_by(AgentRun.created_at.desc())
            .all()
        )

        result = [ActiveRunInfo.from_db(db_run) for db_run in db_runs]

    return ActiveRunsResponse(runs=result)


@router.get("/jobs", dependencies=[Depends(require_admin_access)])
def list_jobs() -> JobsResponse:
    """List all validation jobs."""
    return JobsResponse(jobs=_get_active_jobs())


@router.get("")
def list_runs(
    agent_db: AgentDb,
    status: AgentRunStatus | None = None,
    image_digest: str | None = None,
    agent_type: AgentType | None = None,
    split: Split | None = None,
    example_kind: ExampleKind | None = None,
    offset: int = 0,
    limit: int = 100,
) -> RunsListResponse:
    """List all agent runs with optional filters and pagination.

    RLS policies filter visible runs based on caller's database role.
    """
    limit = min(limit, 500)  # Cap at 500

    with agent_db.session() as session:
        query = session.query(AgentRun)

        if status:
            query = query.filter(AgentRun.status == status)
        if image_digest:
            query = query.filter(AgentRun.image_digest == image_digest)
        if agent_type:
            # agent_type is stored in JSONB type_config
            query = query.filter(AgentRun.type_config["agent_type"].astext == agent_type)
        if example_kind:
            # example_kind is at type_config->'example'->>'kind'
            query = query.filter(AgentRun.type_config["example"]["kind"].astext == example_kind)

        # Join with snapshots to get split.
        # Critic runs: type_config->'example'->>'snapshot_slug'
        # Grader runs: type_config->>'snapshot_slug'
        snapshot_slug_expr = func.coalesce(
            AgentRun.type_config["example"]["snapshot_slug"].astext, AgentRun.type_config["snapshot_slug"].astext
        )
        query = query.outerjoin(Snapshot, snapshot_slug_expr == Snapshot.slug)

        if split:
            query = query.filter(Snapshot.split == split)

        total_count = query.count()

        runs_with_split = (
            query.add_columns(Snapshot.split).order_by(AgentRun.created_at.desc()).offset(offset).limit(limit).all()
        )

        return RunsListResponse(
            runs=[_build_run_info(r, split) for r, split in runs_with_split],
            total_count=total_count,
            offset=offset,
            limit=limit,
        )


@router.post("/validation", dependencies=[Depends(require_admin_access)])
async def trigger_validation_runs(
    request: Request, body: ValidationRunRequest, admin_db: AdminDb
) -> ValidationRunResponse:
    """Trigger validation critic runs: sample N examples, run 1 critic per example.

    Runs are started in the background in parallel. Poll /api/runs/jobs for status.
    Grading is handled automatically by snapshot graders.
    """
    registry = get_registry(request)

    # Get examples of the requested kind and split
    with admin_db.session() as session:
        examples = (
            session.query(Example)
            .join(Snapshot, Snapshot.slug == Example.snapshot_slug)
            .filter(Snapshot.split == body.split)
            .filter(Example.example_kind == body.example_kind)
            .order_by(Example.snapshot_slug)
            .all()
        )

        if not examples:
            raise HTTPException(status_code=404, detail=f"No {body.split} examples of kind {body.example_kind}")

        # Sample N examples
        n_to_sample = min(body.n_samples, len(examples))
        sampled = random.sample(examples, n_to_sample)
        example_specs = [e.to_example_spec() for e in sampled]

    # Create job
    job_id = uuid4()
    job = ValidationJob(
        job_id=job_id,
        image_digest=body.image_digest,
        example_kind=body.example_kind,
        n_samples=n_to_sample,
        critic_model=body.critic_model,
        budget_usd=body.budget_usd,
        examples=example_specs,
    )
    _jobs[job_id] = job

    # Spawn background task with parallel execution
    job.task = asyncio.create_task(_run_validation_batch(job=job, registry=registry, db=admin_db))

    slugs = [e.snapshot_slug for e in example_specs[:3]]
    message = f"Started {n_to_sample} validation runs. Snapshots: {slugs}{'...' if n_to_sample > 3 else ''}"

    return ValidationRunResponse(
        job_id=job_id, status=JobStatus.RUNNING, n_examples_sampled=n_to_sample, message=message
    )


async def _run_validation_batch(job: ValidationJob, registry: AgentRegistry, db: Database) -> None:
    """Run critic for each example in the job, in parallel.

    Grading is handled automatically by snapshot graders.
    """
    # Default timeout: 1 hour per agent
    timeout_seconds = 3600

    try:
        image = await registry.resolve_image(AgentType.CRITIC, job.image_digest)

        async def run_one(example: ExampleSpec) -> bool:
            """Run critic for one example. Returns True on success."""
            try:
                logger.info(f"[Job {job.job_id}] Running critic on {example.snapshot_slug}")
                critic_run_id = await registry.run_critic(
                    image=image,
                    example=example,
                    model=job.critic_model,
                    timeout_seconds=timeout_seconds,
                    parent_run_id=None,
                    budget_usd=job.budget_usd,
                )

                # Check critic status
                with db.session() as session:
                    critic_run = session.get(AgentRun, critic_run_id)
                    if (
                        critic_run is None
                        or critic_run.status != AgentRunStatus.EXITED
                        or critic_run.container_exit_code != 0
                    ):
                        status = critic_run.status if critic_run else "not found"
                        logger.warning(f"[Job {job.job_id}] Critic failed with status {status}")
                        return False

                logger.info(f"[Job {job.job_id}] Critic exited: {critic_run_id}")
                return True

            except Exception:
                logger.exception(f"[Job {job.job_id}] Error processing {example.snapshot_slug}")
                return False

        # Run all examples in parallel
        results = await asyncio.gather(*[run_one(e) for e in job.examples], return_exceptions=True)

        # Count successes and failures
        for result in results:
            if result is True:
                job.completed += 1
            else:
                job.failed += 1

        job.status = JobStatus.COMPLETED
        logger.info(f"[Job {job.job_id}] Finished: {job.completed} completed, {job.failed} failed")

    except Exception:
        logger.exception(f"[Job {job.job_id}] Batch failed")
        job.status = JobStatus.FAILED


# --- Optimize / Improve Endpoints ---


class OptimizeRunRequest(BaseModel):
    target_metric: TargetMetric = Field(description="Validation metric mode: whole-repo or targeted")
    budget_usd: float = Field(ge=0, description="Dollar budget for optimization")
    optimizer_model: str = Field(description="Model for the optimizer agent")
    critic_model: str = Field(description="Model for critic evaluations")
    timeout_seconds: int = Field(ge=60, le=86400, description="Container timeout in seconds")


class OptimizeRunResponse(BaseModel):
    agent_run_id: UUID


@router.post("/optimize", dependencies=[Depends(require_admin_access)])
async def trigger_optimize_run(request: Request, body: OptimizeRunRequest) -> OptimizeRunResponse:
    """Launch a critic developer optimize agent."""
    registry = get_registry(request)
    image = await registry.resolve_image(AgentType.CRITIC_DEV_OPTIMIZE, BUILTIN_TAG)
    run_id = await registry.run_critic_dev_optimize(
        image=image,
        budget=body.budget_usd,
        optimizer_model=body.optimizer_model,
        critic_model=body.critic_model,
        target_metric=body.target_metric,
        timeout_seconds=body.timeout_seconds,
    )
    return OptimizeRunResponse(agent_run_id=run_id)


class ImproveRunRequest(BaseModel):
    n_examples: int = Field(default=10, ge=1, le=100, description="Number of Pareto-optimal training examples")
    budget_usd: float = Field(ge=0, description="Dollar budget for improvement")
    improvement_model: str = Field(description="Model for the improvement agent")
    critic_model: str = Field(description="Model for critic evaluations")
    timeout_seconds: int = Field(ge=60, le=86400, description="Container timeout in seconds")
    baseline_image_digests: list[str] | None = Field(
        default=None, description="Baseline definitions to improve. If None, auto-selects best by validation LCB."
    )
    examples: list[ExampleSpec] | None = Field(
        default=None, description="Training examples. If None, auto-selects Pareto-optimal from best definition."
    )


class ImproveRunResponse(BaseModel):
    agent_run_id: UUID
    definition_id: str = Field(description="Selected definition (provided or auto-selected)")
    n_examples_selected: int


@router.post("/improve", dependencies=[Depends(require_admin_access)])
async def trigger_improve_run(request: Request, body: ImproveRunRequest, admin_db: AdminDb) -> ImproveRunResponse:
    """Launch a critic developer improve agent.

    When baseline_image_digests and examples are provided, uses them directly.
    Otherwise, auto-selects the best critic definition (by validation LCB) and
    top Pareto training examples.
    """
    registry = get_registry(request)

    if body.baseline_image_digests and body.examples:
        # Use provided values directly
        definition_id = body.baseline_image_digests[0]
        allowed_examples = body.examples
    else:
        # Auto-select definition and examples
        with admin_db.session() as session:
            # Find definitions with enough graded training examples
            critic_runs = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
                    AgentRun.status == AgentRunStatus.EXITED,
                )
                .all()
            )

            definition_to_examples: dict[str, set[ExampleSpec]] = {}
            for cr in critic_runs:
                critic_config = cr.critic_config()
                example_spec = critic_config.example
                snapshot = session.query(Snapshot).filter_by(slug=example_spec.snapshot_slug).first()
                if not snapshot or snapshot.split != Split.TRAIN:
                    continue
                # Check if this critic run has been graded (has grading edges)
                has_grading = session.query(GradingEdge).filter(GradingEdge.critique_run_id == cr.agent_run_id).first()
                if has_grading:
                    definition_to_examples.setdefault(cr.image_digest, set()).add(example_spec)

            eligible = [
                (def_id, len(examples))
                for def_id, examples in definition_to_examples.items()
                if len(examples) >= body.n_examples
            ]
            if not eligible:
                raise HTTPException(
                    status_code=422, detail=f"No definitions have {body.n_examples}+ training examples with grader runs"
                )

            eligible_ids = {d for d, _ in eligible}

            # Pick best by validation LCB (whole-snapshot)
            valid_stats = (
                session.query(RecallByDefinitionSplitKind)
                .filter(
                    RecallByDefinitionSplitKind.split == Split.VALID,
                    RecallByDefinitionSplitKind.example_kind == ExampleKind.WHOLE_SNAPSHOT,
                    RecallByDefinitionSplitKind.critic_image_digest.in_(eligible_ids),
                )
                .all()
            )
            with_runs = [
                s for s in valid_stats if s.status_counts and s.status_counts.get(AgentRunStatus.EXITED, 0) > 0
            ]
            if not with_runs:
                raise HTTPException(
                    status_code=422,
                    detail=f"No definitions with {body.n_examples}+ training examples have validation results",
                )

            def get_lcb(s: RecallByDefinitionSplitKind) -> float:
                return s.recall_stats.lcb95 if s.recall_stats and s.recall_stats.lcb95 is not None else -1.0

            best = max(with_runs, key=get_lcb)
            definition_id = best.critic_image_digest

            # Select Pareto-optimal training examples
            recall_rows = query_recall_by_example(session, split=Split.TRAIN, critic_image_digest=definition_id)
            if not recall_rows:
                raise HTTPException(status_code=422, detail=f"No grader runs found for definition {definition_id[:16]}")

            sorted_examples = sorted(
                [(row.example, row.recall) for row in recall_rows], key=lambda x: x[1], reverse=True
            )
            allowed_examples = [ex for ex, _ in sorted_examples[: body.n_examples]]

    image = await registry.resolve_image(AgentType.CRITIC_DEV_IMPROVE, BUILTIN_TAG)
    run_id = await registry.run_critic_dev_improve(
        image=image,
        examples=allowed_examples,
        baseline_image_digests=body.baseline_image_digests or [definition_id],
        budget_usd=body.budget_usd,
        improvement_model=body.improvement_model,
        critic_model=body.critic_model,
        timeout_seconds=body.timeout_seconds,
    )

    return ImproveRunResponse(
        agent_run_id=run_id, definition_id=definition_id, n_examples_selected=len(allowed_examples)
    )


# --- Critic Run Endpoints ---


@router.post("/critic")
async def run_critic(
    request: Request,
    body: RunCriticRequest,
    admin_db: AdminDb,
    auth: Annotated[tuple[CallerType, UUID | None], Depends(require_critic_run_access)],
) -> RunCriticResponse:
    """Run critic agent using an agent package.

    Uses admin_db: this is a privileged API that starts container workloads.

    Validates split-based access restrictions:
    - TRAIN split: all example types allowed
    - VALID split: restrictions depend on target_metric mode
    - TEST split: completely off-limits

    Returns critic_run_id. Use wait_until_graded() to poll DB for grading completion.
    """
    _, parent_run_id = auth
    registry = get_registry(request)

    # Validate snapshot and example
    with admin_db.session() as session:
        snapshot_slug = body.example.snapshot_slug
        db_snapshot = get_snapshot_or_404(session, snapshot_slug)

        if db_snapshot.split == Split.TEST:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: test split is off-limits. Snapshot {snapshot_slug} is in test split.",
            )

        example = Example.from_spec_or_none(session, body.example)
        if not example:
            raise HTTPException(status_code=404, detail=f"Example not found: {body.example.model_dump()}")

    # Resolve image ref and execute critic run
    try:
        image = await registry.resolve_image(AgentType.CRITIC, body.definition_id)
    except ImageResolutionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        critic_run_id = await registry.run_critic(
            image=image,
            example=body.example,
            model=body.critic_model,
            timeout_seconds=body.timeout_seconds,
            parent_run_id=parent_run_id,
            budget_usd=body.budget_usd,
        )
    except BudgetExceededError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Get final status — read attributes inside session to avoid DetachedInstanceError
    with admin_db.session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        assert critic_run is not None
        return RunCriticResponse(
            critic_run_id=critic_run_id, status=critic_run.status, container_exit_code=critic_run.container_exit_code
        )


# --- Run Detail Endpoints ---


@router.get("/{run_id}")
def get_run(run_id: UUID, agent_db: AgentDb) -> AgentRunDetail:
    """Get details of a specific agent run. RLS enforces access."""
    with agent_db.session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Agent run {run_id} not found")

        # Count LLM API calls for this run
        llm_call_count = session.query(LLMRequest).filter(LLMRequest.agent_run_id == run_id).count()

        # Get child runs
        child_run_rows = (
            session.query(AgentRun).filter(AgentRun.parent_agent_run_id == run_id).order_by(AgentRun.created_at).all()
        )
        child_runs = [
            ChildRunInfo(agent_run_id=child.agent_run_id, agent_type=child.type_config.agent_type, status=child.status)
            for child in child_run_rows
        ]

        # Resolve files for critic runs with file_set examples
        resolved_files: list[str] | None = None
        grader_runs: list[GraderRunInfo] = []
        reported_issues: list[ReportedIssueInfo] = []
        grading_edges_for_grader: list[GradingEdgeInfo] = []

        if run.type_config.agent_type == AgentType.CRITIC:
            example = run.type_config.example
            if example.kind == ExampleKind.FILE_SET:
                members = (
                    session.query(FileSetMember.file_path)
                    .filter(
                        FileSetMember.snapshot_slug == example.snapshot_slug,
                        FileSetMember.files_hash == example.files_hash,
                    )
                    .order_by(FileSetMember.file_path)
                    .all()
                )
                resolved_files = [m.file_path for m in members]

            # Find grader runs for this snapshot
            grader_rows = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.GRADER,
                    AgentRun.type_config["snapshot_slug"].astext == example.snapshot_slug,
                )
                .order_by(AgentRun.created_at)
                .all()
            )

            # Fetch all edges for this critic run from all graders
            grader_run_ids = [g.agent_run_id for g in grader_rows]
            all_edges = (
                session.query(GradingEdge)
                .filter(GradingEdge.grader_run_id.in_(grader_run_ids), GradingEdge.critique_run_id == run_id)
                .all()
                if grader_run_ids
                else []
            )
            edges_by_grader: dict[UUID, list[GradingEdge]] = {}
            for edge in all_edges:
                edges_by_grader.setdefault(edge.grader_run_id, []).append(edge)

            # Build GraderRunInfo with pre-grouped edges (only include graders with edges for this run)
            for grader in grader_rows:
                grader_edges = edges_by_grader.get(grader.agent_run_id, [])
                if grader_edges:  # Only include if there are edges for this critic run
                    grader_runs.append(
                        GraderRunInfo(
                            agent_run_id=grader.agent_run_id,
                            status=grader.status,
                            grading_edges=edges_to_info(grader_edges),
                        )
                    )

            # Get reported issues for critic runs
            reported_issues = [
                ReportedIssueInfo(
                    issue_id=issue.issue_id,
                    rationale=issue.rationale,
                    occurrences=[
                        ReportedIssueOccurrenceInfo(occurrence_id=occ.id, note=None, locations=occ.locations)
                        for occ in issue.occurrences
                    ],
                )
                for issue in run.reported_issues
            ]

        elif run.type_config.agent_type == AgentType.GRADER:
            # For grader runs, get their own edges
            edges = session.query(GradingEdge).filter(GradingEdge.grader_run_id == run_id).all()
            grading_edges_for_grader = edges_to_info(edges)

        # Build type-specific details
        details: CriticRunSpecifics | GraderRunSpecifics | OtherRunSpecifics
        if run.type_config.agent_type == AgentType.CRITIC:
            details = CriticRunSpecifics(
                resolved_files=resolved_files, grader_runs=grader_runs, reported_issues=reported_issues
            )
        elif run.type_config.agent_type == AgentType.GRADER:
            details = GraderRunSpecifics(grading_edges=grading_edges_for_grader)
        else:
            details = OtherRunSpecifics(agent_type=run.type_config.agent_type)

        # Get LLM cost stats for this run
        llm_cost_rows = session.query(LLMRunCost).filter(LLMRunCost.agent_run_id == run_id).all()

        llm_costs: LLMCostSummary | None = None
        if llm_cost_rows:
            by_model: dict[str, LLMCostStats] = {
                row.model: LLMCostStats(
                    requests=row.request_count or 0,
                    input_tokens=row.input_tokens or 0,
                    cached_tokens=row.cached_input_tokens or 0,
                    output_tokens=row.output_tokens or 0,
                    cost_usd=row.cost_usd or 0.0,
                )
                for row in llm_cost_rows
            }
            llm_costs = LLMCostSummary(totals=LLMCostStats.aggregate(by_model), by_model=by_model)

        # Return unified AgentRunDetail with nested type-specific details
        return AgentRunDetail(
            agent_run_id=run.agent_run_id,
            image_digest=run.image_digest,
            parent_agent_run_id=run.parent_agent_run_id,
            model=run.model,
            status=run.status,
            budget_usd=run.budget_usd,
            container_exit_code=run.container_exit_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
            type_config=run.type_config,
            llm_call_count=llm_call_count,
            child_runs=child_runs,
            container_stdout=run.container_stdout,
            container_stderr=run.container_stderr,
            llm_costs=llm_costs,
            details=details,
        )


@router.get("/{run_id}/llm_requests")
def get_run_llm_requests(run_id: UUID, agent_db: AgentDb) -> LLMRequestsResponse:
    """Get LLM requests for a specific agent run. RLS filters visible requests."""
    with agent_db.session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Agent run {run_id} not found")

        requests = (
            session.query(LLMRequest)
            .filter(LLMRequest.agent_run_id == run_id)
            .order_by(LLMRequest.created_at.asc())
            .all()
        )

        return LLMRequestsResponse(requests=[LLMRequestInfo.model_validate(req) for req in requests])


# --- WebSocket for Runs Feed (list updates) ---


class WsFeedRunsMessage(BaseModel):
    """WebSocket message containing recent runs."""

    type: Literal["runs"] = "runs"
    runs: list[RunInfo]


class WsFeedJobsMessage(BaseModel):
    """WebSocket message containing active jobs."""

    type: Literal["jobs"] = "jobs"
    jobs: list[JobInfo]


# Track active feed connections
_feed_connections: set[WebSocket] = set()


def _build_run_info(run: AgentRun, split: Split | None) -> RunInfo:
    """Convert AgentRun ORM to RunInfo."""
    return RunInfo(
        agent_run_id=run.agent_run_id,
        image_digest=run.image_digest,
        type_config=run.type_config,
        model=run.model,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        split=split,
    )


def _get_recent_runs(session, limit: int = 20) -> list[RunInfo]:
    """Get recent runs with split info."""
    runs = session.query(AgentRun).order_by(AgentRun.updated_at.desc()).limit(limit).all()

    # Pre-fetch all snapshots to avoid N+1 queries
    snapshot_slugs = {
        run.type_config.example.snapshot_slug for run in runs if isinstance(run.type_config, CriticTypeConfig)
    }
    snapshots = session.query(Snapshot).filter(Snapshot.slug.in_(snapshot_slugs)).all() if snapshot_slugs else []
    snapshot_by_slug = {s.slug: s for s in snapshots}

    # Build result with looked-up splits
    result = []
    for run in runs:
        split = None
        if isinstance(run.type_config, CriticTypeConfig):
            snapshot_slug = run.type_config.example.snapshot_slug
            if snapshot_slug in snapshot_by_slug:
                split = snapshot_by_slug[snapshot_slug].split
        result.append(_build_run_info(run, split))
    return result


def _get_active_jobs() -> list[JobInfo]:
    """Get active validation jobs from in-memory store."""
    return [JobInfo.model_validate(job, from_attributes=True) for job in _jobs.values()]


@router.websocket("/feed")
async def runs_feed(websocket: WebSocket) -> None:
    """WebSocket endpoint for live runs/jobs feed.

    Sends initial state then streams updates when runs or jobs change.
    Requires admin token as ?token= query parameter.
    """
    db: Database = websocket.app.state.admin_db

    # Validate token from query parameter
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    parsed = parse_credentials(f"Bearer {token}")
    if not parsed:
        await websocket.close(code=4001, reason="Invalid token")
        return
    username, password = parsed
    result = validate_postgres_credentials(username, password, db.config)
    if not result.is_valid:
        await websocket.close(code=4001, reason="Invalid credentials")
        return

    await websocket.accept()
    _feed_connections.add(websocket)

    try:
        # Send initial state
        with db.session() as session:
            runs = _get_recent_runs(session)
            jobs = _get_active_jobs()
            await websocket.send_json(WsFeedRunsMessage(runs=runs).model_dump(mode="json"))
            await websocket.send_json(WsFeedJobsMessage(jobs=jobs).model_dump(mode="json"))
            last_updated = max((r.updated_at for r in runs), default=datetime.min)
            last_job_state = [(j.job_id, j.completed, j.failed) for j in jobs]

        # Poll for changes
        while True:
            await asyncio.sleep(1.0)

            with db.session() as session:
                # Check for new/updated runs
                current_runs = _get_recent_runs(session)
                current_updated = max((r.updated_at for r in current_runs), default=datetime.min)

                if current_updated > last_updated:
                    await websocket.send_json(WsFeedRunsMessage(runs=current_runs).model_dump(mode="json"))
                    last_updated = current_updated

                # Check for job changes
                current_jobs = _get_active_jobs()
                current_job_state = [(j.job_id, j.completed, j.failed) for j in current_jobs]

                if current_job_state != last_job_state:
                    await websocket.send_json(WsFeedJobsMessage(jobs=current_jobs).model_dump(mode="json"))
                    last_job_state = current_job_state

    except WebSocketDisconnect:
        logger.debug("Feed WebSocket disconnected")
    finally:
        _feed_connections.discard(websocket)
