"""Agent registry - unified orchestration layer for critic and grader runs.

AgentRegistry is THE entry point for running agents. It owns shared resources
(Docker client, database config) and manages concurrency via an internal semaphore.

The registry uses the in-container agent loop model:
1. Agent loop runs inside the container (CMD entrypoint)
2. Container talks to LLM proxy (OPENAI_BASE_URL)
3. Tools executed via subprocess inside container
4. Container exits 0 on success, non-zero on failure

Host scaffold responsibilities:
1. Create AgentRun in database with type config, resource limits, started_at
2. Start container via run_loop_agent() with timeout enforcement
3. Wait for container exit (or kill on timeout)
4. Determine final status based on exit code and work completed
   (agents cannot update their own status due to RLS)
5. Set ended_at and container_exit_code

Status determination (outside container):
- Exit code 0 + issues reported → COMPLETED (for critics)
- Exit code 0 + all grading edges completed → COMPLETED (for graders)
- Exit code != 0 or validation failed → REPORTED_FAILURE
- Timeout → REPORTED_FAILURE

Resource limits:
- timeout_seconds: Host kills container after timeout; enforced by agent_registry
- budget_usd: Proxy enforces USD cost budget across agent and subagents

Usage:
    registry = AgentRegistry(docker_client, db_config, llm_proxy_url)
    async with registry:
        critic_run_id = await registry.run_critic(
            image_ref="critic",
            example=example,
            model="gpt-4o",
            timeout_seconds=3600,
            budget_usd=10.0,
        )
        # Check status from DB
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import aiodocker
from sqlalchemy import text

from props.core.agent_types import AgentType, CriticTypeConfig, GraderTypeConfig, SnapshotGraderTypeConfig
from props.core.db.config import DatabaseConfig
from props.core.db.models import AgentRun, AgentRunStatus, CanonicalIssuesSnapshot, FileSet, ReportedIssue, Snapshot
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.grader.persistence import orm_fp_to_db, orm_tp_to_db
from props.core.ids import SnapshotSlug
from props.core.loop_agent_env import ContainerResult, run_loop_agent
from props.core.models.examples import ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class AgentRunView:
    """Unified view of an agent run from DB."""

    agent_run_id: UUID
    image_digest: str
    model: str
    status: AgentRunStatus
    created_at: datetime


@dataclass
class ActiveRun:
    """Tracks an in-memory active run."""

    task: asyncio.Task[ContainerResult] | None = None


class AgentRegistry:
    """Unified orchestration layer for critic and grader runs.

    Uses the in-container agent loop model where agents run their own loops
    inside containers and exit 0 on success.

    Owns shared resources and provides the single entry point for execution.
    Manages concurrency via internal semaphore.
    """

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        llm_proxy_url: str = "http://props-llm-proxy:5052",
        max_parallel: int = 4,
    ) -> None:
        self._docker_client = docker_client
        self._db_config = db_config
        self._llm_proxy_url = llm_proxy_url
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._active: dict[UUID, ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Clean up resources."""
        await self._docker_client.close()

    async def __aenter__(self) -> AgentRegistry:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.close()

    # --- Execution Methods ---

    async def run_critic(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        model: str,
        parent_run_id: UUID | None = None,
        timeout_seconds: int | None = None,
        budget_usd: float | None = None,
    ) -> UUID:
        """Run a critic agent. Acquires semaphore slot.

        The critic runs its agent loop inside the container and exits 0 on success.

        Args:
            image_ref: Image reference (tag or digest) - REQUIRED for explicit version control
            example: Example specification (snapshot + scope)
            model: Model name for LLM calls (e.g., "gpt-4o")
            parent_run_id: Optional parent agent run ID (e.g., prompt optimizer)
            timeout_seconds: Max seconds before container is killed (default: no limit)
            budget_usd: Max USD cost for this agent (enforced by proxy)

        Returns:
            Agent run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_critic_impl(
                image_ref=image_ref,
                example=example,
                model=model,
                parent_run_id=parent_run_id,
                timeout_seconds=timeout_seconds,
                budget_usd=budget_usd,
            )

    async def _run_critic_impl(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        model: str,
        parent_run_id: UUID | None,
        timeout_seconds: int | None,
        budget_usd: float | None,
    ) -> UUID:
        """Internal critic execution (semaphore already acquired)."""
        snapshot_slug = example.snapshot_slug
        agent_run_id = uuid4()
        timed_out = False

        # Resolve image reference to digest, then build full OCI reference
        image_digest = resolve_image_ref(AgentType.CRITIC, image_ref)
        image = build_oci_reference(AgentType.CRITIC, image_digest)
        logger.info("Resolved critic image %s → %s", image_ref, image_digest)

        # Write initial AgentRun to DB with resource limits and started_at
        started_at = datetime.utcnow()
        with get_session() as session:
            type_config = CriticTypeConfig(example=example)

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                parent_agent_run_id=parent_run_id,
                model=model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
                timeout_seconds=timeout_seconds,
                budget_usd=budget_usd,
                started_at=started_at,
            )
            session.add(agent_run)
            session.commit()
            logger.info("Created critic run: agent_run_id=%s, snapshot_slug=%s", agent_run_id, snapshot_slug)

        # Track as active
        async with self._lock:
            self._active[agent_run_id] = ActiveRun(task=None)

        result: ContainerResult | None = None
        try:
            # Run agent container with optional timeout
            result = await run_loop_agent(
                self._docker_client,
                agent_run_id,
                self._db_config,
                image=image,
                llm_proxy_url=self._llm_proxy_url,
                container_name=f"critic-{short_uuid(agent_run_id)}",
                timeout_seconds=timeout_seconds,
            )

            # Check for timeout (exit_code=-1 sentinel)
            if result.exit_code == -1:
                timed_out = True

            # Container has exited - check status
            if not timed_out and result.exit_code != 0:
                logger.warning(
                    "Critic container exited with code %d: %s",
                    result.exit_code,
                    result.stderr[:500] if result.stderr else "(no stderr)",
                )

            # Determine final status based on exit code and work completed
            # (agents cannot update their own status due to RLS)
            ended_at = datetime.utcnow()
            with get_session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run is None:
                    raise RuntimeError(f"Agent run {agent_run_id} not found in database")

                run.ended_at = ended_at
                run.container_exit_code = result.exit_code if not timed_out else None

                if timed_out:
                    final_status = AgentRunStatus.TIMED_OUT
                elif result.exit_code == 0:
                    # Check if issues were reported (indicates successful submit)
                    issues_count = session.query(ReportedIssue).filter_by(agent_run_id=agent_run_id).count()
                    if issues_count > 0:
                        final_status = AgentRunStatus.COMPLETED
                    else:
                        logger.warning("Critic exited 0 but reported no issues - marking as failed")
                        final_status = AgentRunStatus.REPORTED_FAILURE
                else:
                    final_status = AgentRunStatus.REPORTED_FAILURE

                run.status = final_status
                session.commit()

            logger.info("Critic run completed: agent_run_id=%s, status=%s", agent_run_id, final_status)

        finally:
            # Remove from active tracking
            async with self._lock:
                self._active.pop(agent_run_id, None)

        return agent_run_id

    async def run_grader(
        self,
        *,
        critic_run_id: UUID,
        model: str,
        parent_run_id: UUID | None = None,
        timeout_seconds: int | None = None,
        budget_usd: float | None = None,
    ) -> UUID:
        """Run a one-off grader on a critic run. Acquires semaphore slot.

        The grader runs its agent loop inside the container and exits 0 on success.
        Always uses builtin grader image for evaluation consistency.

        Args:
            critic_run_id: ID of the critic run to grade
            model: Model name for LLM calls (e.g., "gpt-4o")
            parent_run_id: Optional parent agent run ID
            timeout_seconds: Max seconds before container is killed (default: no limit)
            budget_usd: Max USD cost for this agent (enforced by proxy)

        Returns:
            Grader run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_grader_impl(
                critic_run_id=critic_run_id,
                model=model,
                parent_run_id=parent_run_id,
                timeout_seconds=timeout_seconds,
                budget_usd=budget_usd,
            )

    async def _run_grader_impl(
        self,
        *,
        critic_run_id: UUID,
        model: str,
        parent_run_id: UUID | None,
        timeout_seconds: int | None,
        budget_usd: float | None,
    ) -> UUID:
        """Internal one-off grader execution (semaphore already acquired)."""
        grader_run_id = uuid4()
        timed_out = False

        # Always use builtin grader image
        image_digest = resolve_image_ref(AgentType.GRADER, BUILTIN_TAG)
        image = build_oci_reference(AgentType.GRADER, image_digest)
        logger.info("Using builtin grader image: %s", image_digest)

        # Load critic run and prepare canonical issues
        started_at = datetime.utcnow()
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run is None:
                raise ValueError(f"Critic run {critic_run_id} not found in database")

            if not isinstance(critic_run.type_config, CriticTypeConfig):
                raise ValueError(f"Critic run {critic_run_id} has wrong type_config type")

            example_spec = critic_run.type_config.example
            snapshot_slug = example_spec.snapshot_slug

            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()

            # Resolve scope to file set for TP/FP filtering
            if isinstance(example_spec, WholeSnapshotExample):
                reviewed_files = snapshot.files_with_issues()
                if not reviewed_files:
                    raise ValueError(f"Snapshot '{snapshot_slug}' has no files with ground truth issues")
            else:
                assert isinstance(example_spec, SingleFileSetExample)
                file_set = (
                    session.query(FileSet)
                    .filter_by(snapshot_slug=example_spec.snapshot_slug, files_hash=example_spec.files_hash)
                    .one()
                )
                reviewed_files = {Path(m.file_path) for m in file_set.members}

            # Filter TPs/FPs
            original_tp_count = len(snapshot.true_positives)
            filtered_orm_tps = [
                tp
                for tp in snapshot.true_positives
                if any(
                    any(alt.issubset(reviewed_files) for alt in occ.critic_scopes_expected_to_recall_set)
                    for occ in tp.occurrences
                )
            ]
            filtered_orm_fps = [
                fp
                for fp in snapshot.false_positives
                if any(bool({rf.file_path for rf in occ.relevant_file_orms} & reviewed_files) for occ in fp.occurrences)
            ]

            if original_tp_count > 0 and len(filtered_orm_tps) == 0:
                raise ValueError(
                    f"Cannot grade: 0/{original_tp_count} TPs in expected recall scope from reviewed files "
                    f"{sorted(str(f) for f in reviewed_files)}"
                )

            # Build canonical issues snapshot (direct ORM → DB conversion)
            canonical_snapshot = CanonicalIssuesSnapshot(
                true_positives=[orm_tp_to_db(tp) for tp in filtered_orm_tps],
                false_positives=[orm_fp_to_db(fp) for fp in filtered_orm_fps],
            )

            type_config = GraderTypeConfig(
                graded_agent_run_id=critic_run_id, canonical_issues_snapshot=canonical_snapshot.model_dump()
            ).model_dump(mode="json")

            # Write initial grader run with resource limits and started_at
            session.add(
                AgentRun(
                    agent_run_id=grader_run_id,
                    image_digest=image_digest,
                    parent_agent_run_id=parent_run_id,
                    model=model,
                    type_config=type_config,
                    status=AgentRunStatus.IN_PROGRESS,
                    timeout_seconds=timeout_seconds,
                    budget_usd=budget_usd,
                    started_at=started_at,
                )
            )
            session.commit()
            logger.info("Created grader run: agent_run_id=%s, snapshot_slug=%s", grader_run_id, snapshot_slug)

        # Track as active
        async with self._lock:
            self._active[grader_run_id] = ActiveRun(task=None)

        result: ContainerResult | None = None
        try:
            # Run agent container with optional timeout
            result = await run_loop_agent(
                self._docker_client,
                grader_run_id,
                self._db_config,
                image=image,
                llm_proxy_url=self._llm_proxy_url,
                container_name=f"grader-{short_uuid(grader_run_id)}",
                timeout_seconds=timeout_seconds,
            )

            # Check for timeout (exit_code=-1 sentinel)
            if result.exit_code == -1:
                timed_out = True

            # Container has exited - check status
            if not timed_out and result.exit_code != 0:
                logger.warning(
                    "Grader container exited with code %d: %s",
                    result.exit_code,
                    result.stderr[:500] if result.stderr else "(no stderr)",
                )

            # Determine final status based on exit code and work completed
            # (agents cannot update their own status due to RLS)
            ended_at = datetime.utcnow()
            with get_session() as session:
                run = session.get(AgentRun, grader_run_id)
                if run is None:
                    raise RuntimeError(f"Agent run {grader_run_id} not found in database")

                run.ended_at = ended_at
                run.container_exit_code = result.exit_code if not timed_out else None

                if timed_out:
                    final_status = AgentRunStatus.TIMED_OUT
                elif result.exit_code == 0:
                    # Check if all grading edges are complete (no pending edges)
                    pending_count = session.execute(
                        text("SELECT COUNT(*) FROM grading_pending WHERE critique_run_id = :critic_run_id"),
                        {"critic_run_id": critic_run_id},
                    ).scalar()

                    if pending_count == 0:
                        final_status = AgentRunStatus.COMPLETED
                    else:
                        logger.warning(
                            "Grader exited 0 but %d edges still pending - marking as failed", pending_count
                        )
                        final_status = AgentRunStatus.REPORTED_FAILURE
                else:
                    final_status = AgentRunStatus.REPORTED_FAILURE

                run.status = final_status
                session.commit()

            logger.info("Grader run completed: agent_run_id=%s, status=%s", grader_run_id, final_status)

        finally:
            # Remove from active tracking
            async with self._lock:
                self._active.pop(grader_run_id, None)

        return grader_run_id

    # --- Snapshot Grader Daemon ---

    async def run_snapshot_grader(
        self,
        *,
        snapshot_slug: SnapshotSlug,
        model: str,
        budget_usd: float | None = None,
    ) -> UUID:
        """Run a snapshot grader daemon. Blocks until shutdown or fatal error.

        The daemon runs its reconciliation loop inside the container:
        - Grades until grading_pending is empty
        - Sleeps waiting for pg_notify
        - Wakes and repeats

        Always uses builtin grader-daemon image for evaluation consistency.

        Note: Daemons are expected to run indefinitely; timeout_seconds is not supported.

        Args:
            snapshot_slug: Snapshot this daemon is responsible for
            model: Model name for LLM calls (e.g., "gpt-4o")
            budget_usd: Max USD cost for this agent (enforced by proxy)

        Returns:
            Daemon run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_snapshot_grader_impl(
                snapshot_slug=snapshot_slug,
                model=model,
                budget_usd=budget_usd,
            )

    async def _run_snapshot_grader_impl(
        self,
        *,
        snapshot_slug: SnapshotSlug,
        model: str,
        budget_usd: float | None,
    ) -> UUID:
        """Internal snapshot grader daemon execution."""
        grader_run_id = uuid4()

        # Use snapshot_grader image (not one-off grader)
        image_digest = resolve_image_ref(AgentType.SNAPSHOT_GRADER, BUILTIN_TAG)
        image = build_oci_reference(AgentType.SNAPSHOT_GRADER, image_digest)
        logger.info("Using builtin snapshot_grader image: %s", image_digest)

        # Verify snapshot exists
        started_at = datetime.utcnow()
        with get_session() as session:
            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one_or_none()
            if snapshot is None:
                raise ValueError(f"Snapshot '{snapshot_slug}' not found")

            type_config = SnapshotGraderTypeConfig(snapshot_slug=snapshot_slug).model_dump(mode="json")

            # Create initial daemon run with resource limits and started_at
            # Note: timeout_seconds not supported for daemons (they run indefinitely)
            session.add(
                AgentRun(
                    agent_run_id=grader_run_id,
                    image_digest=image_digest,
                    parent_agent_run_id=None,
                    model=model,
                    type_config=type_config,
                    status=AgentRunStatus.IN_PROGRESS,
                    budget_usd=budget_usd,
                    started_at=started_at,
                )
            )
            session.commit()
            logger.info("Created snapshot grader daemon: agent_run_id=%s, snapshot=%s", grader_run_id, snapshot_slug)

        # Track as active
        async with self._lock:
            self._active[grader_run_id] = ActiveRun(task=None)

        try:
            # Run daemon container (it handles its own reconciliation loop)
            # No timeout - daemons are expected to run indefinitely
            result = await run_loop_agent(
                self._docker_client,
                grader_run_id,
                self._db_config,
                image=image,
                llm_proxy_url=self._llm_proxy_url,
                container_name=f"snapshot-grader-{short_uuid(grader_run_id)}",
            )

            # Container has exited - check status
            # Grader daemons should run indefinitely - any exit is unexpected
            logger.error(
                "Grader daemon exited unexpectedly: exit_code=%d, stderr=%s",
                result.exit_code,
                result.stderr[:500] if result.stderr else "(no stderr)",
            )

            # Determine final status (daemons should never exit successfully)
            ended_at = datetime.utcnow()
            with get_session() as session:
                run = session.get(AgentRun, grader_run_id)
                if run is None:
                    raise RuntimeError(f"Agent run {grader_run_id} not found in database")

                run.ended_at = ended_at
                run.container_exit_code = result.exit_code
                # Daemons should run indefinitely - any exit is a failure
                final_status = AgentRunStatus.REPORTED_FAILURE
                run.status = final_status
                session.commit()

            logger.error("Grader daemon terminated: agent_run_id=%s, status=%s", grader_run_id, final_status)

        finally:
            # Remove from active tracking
            async with self._lock:
                self._active.pop(grader_run_id, None)

        return grader_run_id

    # --- State Tracking ---

    def get(self, run_id: UUID) -> AgentRunView | None:
        """Get agent run view from DB."""
        with get_session() as session:
            db_run = session.get(AgentRun, run_id)
            if not db_run:
                return None
            return AgentRunView(
                agent_run_id=db_run.agent_run_id,
                image_digest=db_run.image_digest,
                model=db_run.model,
                status=db_run.status,
                created_at=db_run.created_at,
            )

    def list_active(self) -> list[UUID]:
        """List IDs of currently running agents."""
        return list(self._active.keys())

    def list_recent(self, limit: int = 50) -> list[AgentRunView]:
        """List recent runs from database."""
        with get_session() as session:
            runs = session.query(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit).all()
            return [
                AgentRunView(
                    agent_run_id=r.agent_run_id,
                    image_digest=r.image_digest,
                    model=r.model,
                    status=r.status,
                    created_at=r.created_at,
                )
                for r in runs
            ]

    def is_active(self, run_id: UUID) -> bool:
        """Check if an agent run is currently active."""
        return run_id in self._active
