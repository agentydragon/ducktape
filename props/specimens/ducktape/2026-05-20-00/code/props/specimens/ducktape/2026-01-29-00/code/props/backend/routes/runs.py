"""Runs API routes for triggering and monitoring agent runs.

All endpoints require admin access (localhost admin or authenticated admin user).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from props.backend.auth import require_admin_access
from props.backend.routes.ground_truth import FileLocationInfo
from props.core.agent_types import AgentType, CriticTypeConfig, TypeConfig
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.models.true_positive import LineRange
from props.core.splits import Split
from props.db.examples import Example
from props.db.models import (
    AgentRun,
    AgentRunStatus,
    FileSetMember,
    GradingEdge,
    GradingTarget,
    LLMRequest,
    LLMRunCost,
    Snapshot,
)
from props.db.session import get_session
from props.orchestration.agent_registry import AgentRegistry

router = APIRouter(dependencies=[Depends(require_admin_access)])
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


class ActiveRunsResponse(BaseModel):
    runs: list[ActiveRunInfo]


class ValidationRunRequest(BaseModel):
    image_digest: str
    example_kind: ExampleKind
    split: Split = Split.VALID
    n_samples: int = Field(ge=1, le=50, default=5)
    critic_model: str = "gpt-5.1-codex-mini"
    # Note: grader_model removed - grading is handled by snapshot grader daemons


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
    grading_edges: list[GradingEdgeInfo]  # This grader's output edges


class GradingEdgeInfo(BaseModel):
    """Individual grading edge for API response."""

    critique_issue_id: str
    target: GradingTarget
    rationale: str


class ReportedIssueOccurrenceInfo(BaseModel):
    """Occurrence of a reported issue."""

    occurrence_id: str
    note: str | None
    files: list[FileLocationInfo]


class ReportedIssueInfo(BaseModel):
    """Issue reported by a critic run."""

    issue_id: str
    rationale: str
    occurrences: list[ReportedIssueOccurrenceInfo]


# Type-specific details (only fields unique to each agent type)


class CriticRunSpecifics(BaseModel):
    """Critic-specific fields."""

    agent_type: Literal[AgentType.CRITIC] = AgentType.CRITIC
    resolved_files: list[str] | None  # For file_set examples
    grader_runs: list[GraderRunInfo]  # Grader runs with their edges nested
    reported_issues: list[ReportedIssueInfo]  # Issues found by the critic


class GraderRunSpecifics(BaseModel):
    """Grader-specific fields."""

    agent_type: Literal[AgentType.GRADER] = AgentType.GRADER
    grading_edges: list[GradingEdgeInfo]  # Output edges from this grader


class OtherRunSpecifics(BaseModel):
    """Other agent types have no specific fields."""

    agent_type: Literal[AgentType.PROMPT_OPTIMIZER, AgentType.IMPROVEMENT, AgentType.FREEFORM]


RunSpecifics = Annotated[CriticRunSpecifics | GraderRunSpecifics | OtherRunSpecifics, Field(discriminator="agent_type")]


class LLMCostStats(BaseModel):
    """Aggregated LLM cost stats for an agent run."""

    total_requests: int
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    by_model: dict[str, dict]  # model -> {requests, input_tokens, cached_tokens, output_tokens, cost_usd}


class AgentRunDetail(BaseModel):
    """Detailed view of an agent run with type-specific details nested."""

    # Common fields for all agent types
    agent_run_id: UUID
    image_digest: str
    parent_agent_run_id: UUID | None
    model: str
    status: AgentRunStatus
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
    llm_costs: LLMCostStats | None

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
    # Split is only present for critic runs (derived from snapshot)
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
    total_count: int


# --- WebSocket Message Types (Discriminated Union) ---


class WsStatusMessage(BaseModel):
    """WebSocket message containing run status."""

    type: Literal["status"] = "status"
    status: AgentRunStatus
    container_exit_code: int | None


class WsCompleteMessage(BaseModel):
    """WebSocket message indicating stream is complete."""

    type: Literal["complete"] = "complete"


# Discriminated union of all WebSocket message types
WsMessage = Annotated[WsStatusMessage | WsCompleteMessage, Field(discriminator="type")]


# --- Job Tracking ---


@dataclass
class ValidationJob:
    """Tracks a validation batch job.

    Note: grader_model removed - grading is handled by snapshot grader daemons.
    """

    job_id: UUID
    image_digest: str
    example_kind: ExampleKind
    n_samples: int
    critic_model: str
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
def list_active_runs(request: Request) -> ActiveRunsResponse:
    """List all active agent runs.

    Queries database for runs with IN_PROGRESS status.
    In the new in-container architecture, agents run independently and
    status is tracked only in the database.
    """
    # Query database for IN_PROGRESS runs
    with get_session() as session:
        db_runs = (
            session.query(AgentRun)
            .filter(AgentRun.status == AgentRunStatus.IN_PROGRESS)
            .order_by(AgentRun.created_at.desc())
            .all()
        )

        result = [
            ActiveRunInfo(
                agent_run_id=db_run.agent_run_id,
                image_digest=db_run.image_digest,
                model=db_run.model,
                status=db_run.status,
                created_at=db_run.created_at,
            )
            for db_run in db_runs
        ]

    return ActiveRunsResponse(runs=result)


@router.get("/jobs")
def list_jobs() -> JobsResponse:
    """List all validation jobs."""
    # JobInfo is a subset of ValidationJob fields - use model_validate for clarity
    return JobsResponse(jobs=[JobInfo.model_validate(job, from_attributes=True) for job in _jobs.values()])


@router.get("")
def list_runs(
    status: AgentRunStatus | None = None,
    image_digest: str | None = None,
    agent_type: AgentType | None = None,
    split: Split | None = None,
    example_kind: ExampleKind | None = None,
    offset: int = 0,
    limit: int = 100,
) -> RunsListResponse:
    """List all agent runs with optional filters and pagination.

    Query parameters:
    - status: Filter by run status
    - image_digest: Filter by image digest
    - agent_type: Filter by agent type (critic, grader, etc.)
    - split: Filter by data split (train, valid, test)
    - example_kind: Filter by example kind (whole_snapshot, file_set)
    - offset: Pagination offset (default: 0)
    - limit: Pagination limit (default: 100, max: 500)
    """
    limit = min(limit, 500)  # Cap at 500

    with get_session() as session:
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

        # Join with snapshots to get split for critic runs
        # For critic runs, snapshot_slug is at type_config->'example'->>'snapshot_slug'
        query = query.outerjoin(Snapshot, AgentRun.type_config["example"]["snapshot_slug"].astext == Snapshot.slug)

        if split:
            query = query.filter(Snapshot.split == split)

        total_count = query.count()

        runs_with_split = (
            query.add_columns(Snapshot.split).order_by(AgentRun.created_at.desc()).offset(offset).limit(limit).all()
        )

        return RunsListResponse(
            runs=[
                RunInfo(
                    agent_run_id=r.agent_run_id,
                    image_digest=r.image_digest,
                    type_config=r.type_config,
                    model=r.model,
                    status=r.status,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    split=split,
                )
                for r, split in runs_with_split
            ],
            total_count=total_count,
            offset=offset,
            limit=limit,
        )


@router.post("/validation")
async def trigger_validation_runs(request: Request, body: ValidationRunRequest) -> ValidationRunResponse:
    """Trigger validation critic runs: sample N examples, run 1 critic per example.

    Runs are started in the background in parallel. Poll /api/runs/jobs for status.
    Registry semaphore limits actual concurrency.
    Grading is handled automatically by snapshot grader daemons.
    """
    registry = get_registry(request)

    # Get examples of the requested kind and split
    with get_session() as session:
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
        examples=example_specs,
    )
    _jobs[job_id] = job

    # Spawn background task with parallel execution
    job.task = asyncio.create_task(_run_validation_batch(job=job, registry=registry))

    slugs = [e.snapshot_slug for e in example_specs[:3]]
    message = f"Started {n_to_sample} validation runs. Snapshots: {slugs}{'...' if n_to_sample > 3 else ''}"

    return ValidationRunResponse(
        job_id=job_id, status=JobStatus.RUNNING, n_examples_sampled=n_to_sample, message=message
    )


async def _run_validation_batch(job: ValidationJob, registry: AgentRegistry) -> None:
    """Run critic for each example in the job, in parallel.

    Registry semaphore limits actual concurrency.
    Grading is handled automatically by snapshot grader daemons.
    """
    # Default timeout: 1 hour per agent
    timeout_seconds = 3600

    try:

        async def run_one(example: ExampleSpec) -> bool:
            """Run critic for one example. Returns True on success."""
            try:
                logger.info(f"[Job {job.job_id}] Running critic on {example.snapshot_slug}")
                critic_run_id = await registry.run_critic(
                    image_ref=job.image_digest,
                    example=example,
                    model=job.critic_model,
                    timeout_seconds=timeout_seconds,
                    parent_run_id=None,
                    budget_usd=None,
                )

                # Check critic status
                with get_session() as session:
                    critic_run = session.get(AgentRun, critic_run_id)
                    if critic_run is None or critic_run.status != AgentRunStatus.COMPLETED:
                        status = critic_run.status if critic_run else "not found"
                        logger.warning(f"[Job {job.job_id}] Critic failed with status {status}")
                        return False

                logger.info(f"[Job {job.job_id}] Critic completed: {critic_run_id}")
                return True

            except Exception:
                logger.exception(f"[Job {job.job_id}] Error processing {example.snapshot_slug}")
                return False

        # Run all examples in parallel; registry semaphore limits concurrency
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


# --- Run Detail Endpoints ---


@router.get("/{run_id}")
def get_run(run_id: UUID) -> AgentRunDetail:
    """Get details of a specific agent run."""
    with get_session() as session:
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

            # Find grader runs that graded this critic
            grader_rows = (
                session.query(AgentRun)
                .filter(AgentRun.type_config["graded_agent_run_id"].astext == str(run_id))
                .order_by(AgentRun.created_at)
                .all()
            )

            # Fetch all edges for all graders in one query (avoid N+1)
            grader_run_ids = [g.agent_run_id for g in grader_rows]
            all_edges = (
                session.query(GradingEdge).filter(GradingEdge.grader_run_id.in_(grader_run_ids)).all()
                if grader_run_ids
                else []
            )
            edges_by_grader: dict[UUID, list[GradingEdge]] = {}
            for edge in all_edges:
                edges_by_grader.setdefault(edge.grader_run_id, []).append(edge)

            # Build GraderRunInfo with pre-grouped edges
            for grader in grader_rows:
                grader_edges = edges_by_grader.get(grader.agent_run_id, [])
                grader_runs.append(
                    GraderRunInfo(
                        agent_run_id=grader.agent_run_id,
                        status=grader.status,
                        grading_edges=edges_to_info(grader_edges),
                    )
                )

            # Get reported issues for critic runs
            def _group_locations_by_file(locations: list) -> list[FileLocationInfo]:
                """Group flat location anchors by file into FileLocationInfo structure."""
                by_file: dict[str, list[LineRange]] = defaultdict(list)
                for loc in locations:
                    file_path = loc["file"]
                    start_line = loc.get("start_line")
                    end_line = loc.get("end_line")
                    if start_line is not None:
                        by_file[file_path].append(
                            LineRange(
                                start_line=start_line, end_line=end_line if end_line != start_line else None, note=None
                            )
                        )
                    else:
                        # Whole file
                        by_file[file_path] = []
                return [
                    FileLocationInfo(path=path, ranges=ranges_list if ranges_list else None)
                    for path, ranges_list in sorted(by_file.items())
                ]

            reported_issues = [
                ReportedIssueInfo(
                    issue_id=issue.issue_id,
                    rationale=issue.rationale,
                    occurrences=[
                        ReportedIssueOccurrenceInfo(
                            occurrence_id=str(occ.id), note=None, files=_group_locations_by_file(occ.locations)
                        )
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

        llm_costs: LLMCostStats | None = None
        if llm_cost_rows:
            by_model: dict[str, dict] = {}
            total_requests = 0
            total_input = 0
            total_cached = 0
            total_output = 0
            total_cost = 0.0
            for row in llm_cost_rows:
                requests = row.request_count or 0
                input_tokens = row.input_tokens or 0
                cached = row.cached_input_tokens or 0
                output_tokens = row.output_tokens or 0
                cost = row.cost_usd or 0.0
                by_model[row.model] = {
                    "requests": requests,
                    "input_tokens": input_tokens,
                    "cached_tokens": cached,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                }
                total_requests += requests
                total_input += input_tokens
                total_cached += cached
                total_output += output_tokens
                total_cost += cost
            llm_costs = LLMCostStats(
                total_requests=total_requests,
                total_input_tokens=total_input,
                total_cached_tokens=total_cached,
                total_output_tokens=total_output,
                total_cost_usd=total_cost,
                by_model=by_model,
            )

        # Return unified AgentRunDetail with nested type-specific details
        return AgentRunDetail(
            agent_run_id=run.agent_run_id,
            image_digest=run.image_digest,
            parent_agent_run_id=run.parent_agent_run_id,
            model=run.model,
            status=run.status,
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
def get_run_llm_requests(run_id: UUID) -> LLMRequestsResponse:
    """Get LLM requests for a specific agent run."""
    with get_session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Agent run {run_id} not found")

        requests = (
            session.query(LLMRequest)
            .filter(LLMRequest.agent_run_id == run_id)
            .order_by(LLMRequest.created_at.asc())
            .all()
        )

        return LLMRequestsResponse(
            requests=[LLMRequestInfo.model_validate(req) for req in requests], total_count=len(requests)
        )


# --- WebSocket for Live Event Streaming ---

# Track active WebSocket connections per run
_ws_connections: dict[UUID, set[WebSocket]] = {}


@router.websocket("/run/{run_id}/stream")
async def stream_run_events(websocket: WebSocket, run_id: UUID) -> None:
    """WebSocket endpoint for run status streaming.

    Events table deprecated. Now only streams status updates until run completes.
    """
    await websocket.accept()

    # Verify run exists
    with get_session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            await websocket.close(code=4004, reason=f"Agent run {run_id} not found")
            return

    # Track connection
    if run_id not in _ws_connections:
        _ws_connections[run_id] = set()
    _ws_connections[run_id].add(websocket)

    def _make_status_msg(run: AgentRun) -> WsStatusMessage:
        return WsStatusMessage(status=run.status, container_exit_code=run.container_exit_code)

    try:
        # Send initial status
        with get_session() as session:
            run = session.get(AgentRun, run_id)
            if run:
                await websocket.send_json(_make_status_msg(run).model_dump(mode="json"))

        # Poll for status changes (until run completes or client disconnects)
        while True:
            await asyncio.sleep(0.5)  # Poll every 500ms

            with get_session() as session:
                run = session.get(AgentRun, run_id)
                if run and run.status != AgentRunStatus.IN_PROGRESS:
                    # Send final status and close
                    await websocket.send_json(_make_status_msg(run).model_dump(mode="json"))
                    await websocket.send_json(WsCompleteMessage().model_dump(mode="json"))
                    break

    except WebSocketDisconnect:
        logger.debug(f"WebSocket disconnected for run {run_id}")
    finally:
        # Clean up connection tracking
        if run_id in _ws_connections:
            _ws_connections[run_id].discard(websocket)
            if not _ws_connections[run_id]:
                del _ws_connections[run_id]


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
    """
    await websocket.accept()
    _feed_connections.add(websocket)

    try:
        # Send initial state
        with get_session() as session:
            runs = _get_recent_runs(session)
            jobs = _get_active_jobs()
            await websocket.send_json(WsFeedRunsMessage(runs=runs).model_dump(mode="json"))
            await websocket.send_json(WsFeedJobsMessage(jobs=jobs).model_dump(mode="json"))
            last_updated = max((r.updated_at for r in runs), default=datetime.min)
            last_job_state = [(j.job_id, j.completed, j.failed) for j in jobs]

        # Poll for changes
        while True:
            await asyncio.sleep(1.0)

            with get_session() as session:
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
