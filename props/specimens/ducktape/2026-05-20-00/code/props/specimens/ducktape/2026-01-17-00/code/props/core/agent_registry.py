"""Agent registry - unified orchestration layer for critic and grader runs.

AgentRegistry is THE entry point for running agents. It owns shared resources
(Docker client, database config, workspace manager) and manages concurrency
via an internal semaphore.

Usage:
    registry = AgentRegistry(docker_client, db_config, workspace_manager)
    async with registry:
        critic_run_id = await registry.run_critic(
            image_ref="critic",
            example=example,
            client=critic_client,
        )
        # Check status from DB
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run.status == AgentRunStatus.COMPLETED:
                grader_run_id = await registry.run_grader(
                    critic_run_id=critic_run_id,
                    client=grader_client,
                )
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import aiodocker
from fastmcp.client import Client

from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.turn_limit import MaxTurnsExceededError, MaxTurnsHandler
from mcp_infra.display.rich_display import CompactDisplayHandler
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import OpenAIModelProto, UserMessage
from openai_utils.types import ReasoningSummary
from props.core.agent_handle import AgentHandle
from props.core.agent_types import AgentType, CriticTypeConfig, GraderTypeConfig, SnapshotGraderTypeConfig
from props.core.agent_workspace import WorkspaceManager
from props.core.cli.common_options import DEFAULT_MAX_LINES
from props.core.critic.critic import CriticAgentEnvironment
from props.core.critic.exceptions import CriticExecutionError
from props.core.db.agent_definition_ids import GRADER_IMAGE_REF
from props.core.db.config import DatabaseConfig
from props.core.db.models import AgentRun, AgentRunStatus, CanonicalIssuesSnapshot, FileSet, Snapshot
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.exceptions import AgentDidNotSubmitError
from props.core.grader.daemon import GraderDaemonScaffold
from props.core.grader.drift_handler import format_notifications
from props.core.grader.grader import GraderAgentEnvironment
from props.core.grader.persistence import orm_fp_to_db, orm_tp_to_db
from props.core.grader.snapshot_grader_env import SnapshotGraderAgentEnvironment
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class AgentRunView:
    """Unified view of an agent run from memory or DB."""

    agent_run_id: UUID
    image_digest: str
    model: str
    status: AgentRunStatus
    created_at: datetime
    # Only set for active runs
    handle: AgentHandle | None = None


@dataclass
class ActiveRun:
    """Tracks an in-memory active run."""

    handle: AgentHandle
    task: asyncio.Task | None = None  # None if not yet rolling out


class AgentRegistry:
    """Unified orchestration layer for critic and grader runs.

    Owns shared resources and provides the single entry point for execution.
    Manages concurrency via internal semaphore.
    """

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        max_parallel: int = 4,
    ) -> None:
        self._docker_client = docker_client
        self._db_config = db_config
        self._workspace_manager = workspace_manager
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
        client: OpenAIModelProto,
        parent_run_id: UUID | None = None,
        verbose: bool = False,
        max_lines: int = DEFAULT_MAX_LINES,
        max_turns: int = 100,
        extra_handlers: tuple[BaseHandler, ...] = (),
    ) -> UUID:
        """Run a critic agent. Acquires semaphore slot.

        Args:
            image_ref: Image reference (tag or digest) - REQUIRED for explicit version control
            example: Example specification (snapshot + scope)
            client: OpenAI-compatible model client
            parent_run_id: Optional parent agent run ID (e.g., prompt optimizer)
            verbose: Whether to enable verbose display
            max_lines: Max lines per event in verbose display
            max_turns: Maximum agent turns before timeout
            extra_handlers: Additional handlers to add

        Returns:
            Agent run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_critic_impl(
                image_ref=image_ref,
                example=example,
                client=client,
                parent_run_id=parent_run_id,
                verbose=verbose,
                max_lines=max_lines,
                max_turns=max_turns,
                extra_handlers=extra_handlers,
            )

    async def _run_critic_impl(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        client: OpenAIModelProto,
        parent_run_id: UUID | None,
        verbose: bool,
        max_lines: int,
        max_turns: int,
        extra_handlers: tuple[BaseHandler, ...],
    ) -> UUID:
        """Internal critic execution (semaphore already acquired)."""
        snapshot_slug = example.snapshot_slug
        agent_run_id = uuid4()

        # Resolve image reference to digest, then build full OCI reference
        image_digest = resolve_image_ref(AgentType.CRITIC, image_ref)
        image = build_oci_reference(AgentType.CRITIC, image_digest)
        logger.info(f"Resolved critic image {image_ref} → {image_digest}")

        # Phase 1: Write initial AgentRun to DB
        with get_session() as session:
            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
            snapshot_split = snapshot.split

            type_config = CriticTypeConfig(example=example)

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                parent_agent_run_id=parent_run_id,
                model=client.model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()
            logger.info(f"Created critic run: agent_run_id={agent_run_id}, snapshot_slug={snapshot_slug}")

        # Set up environment
        comp_ctx = CriticAgentEnvironment(
            example=example,
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            workspace_manager=self._workspace_manager,
            image=image,
        )

        agent_status: AgentRunStatus
        async with comp_ctx as comp, Client(comp) as mcp_client:
            # Build handlers
            def _ready_state() -> bool:
                with get_session() as session:
                    run = session.get(AgentRun, agent_run_id)
                    return run is not None and run.status in (AgentRunStatus.COMPLETED, AgentRunStatus.REPORTED_FAILURE)

            handlers: list[BaseHandler] = []
            if verbose:
                display_handler = await CompactDisplayHandler.from_compositor(
                    comp,
                    max_lines=max_lines,
                    prefix=f"[CRITIC {short_uuid(agent_run_id)} {snapshot_split} {snapshot_slug}] ",
                )
                handlers.append(display_handler)

            handlers.extend(
                [
                    RedirectOnTextMessageHandler(
                        reminder_message=(
                            "Text messages won't be delivered. Mark issues via MCP tools, then call submit. "
                            "If you encounter unrecoverable problems, call report_failure instead."
                        )
                    ),
                    AbortIf(should_abort=_ready_state),
                    *extra_handlers,
                    MaxTurnsHandler(max_turns=max_turns),
                ]
            )

            # Create AgentHandle
            handle = await AgentHandle.create(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                model_client=client,
                mcp_client=mcp_client,
                compositor=comp,
                handlers=handlers,
                reasoning_summary=ReasoningSummary.DETAILED,
            )

            # Track as active
            async with self._lock:
                self._active[agent_run_id] = ActiveRun(handle=handle, task=None)

            try:
                await handle.run()
            except MaxTurnsExceededError:
                logger.warning(
                    f"Critic hit max turns limit ({max_turns}) for {snapshot_slug}, "
                    f"agent_run_id={short_uuid(agent_run_id)}"
                )
                agent_status = AgentRunStatus.MAX_TURNS_EXCEEDED
            except ContextLengthExceededError as e:
                logger.warning(
                    f"Critic hit context length limit for {snapshot_slug}, agent_run_id={short_uuid(agent_run_id)}: {e}"
                )
                agent_status = AgentRunStatus.CONTEXT_LENGTH_EXCEEDED
            else:
                # Check database status
                with get_session() as session:
                    run = session.get(AgentRun, agent_run_id)
                    if run is None:
                        raise CriticExecutionError("Agent run not found in database")

                    if run.status == AgentRunStatus.REPORTED_FAILURE:
                        raise CriticExecutionError(f"Critic reported failure: {run.completion_summary or 'No message'}")

                    if run.status != AgentRunStatus.COMPLETED:
                        raise AgentDidNotSubmitError(AgentType.CRITIC, agent_run_id)

                    agent_status = AgentRunStatus.COMPLETED
            finally:
                # Remove from active tracking
                async with self._lock:
                    self._active.pop(agent_run_id, None)

        # Phase 2: Update run with status
        with get_session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            found_run.status = agent_status
            session.commit()
            logger.info(f"Updated critic run: agent_run_id={agent_run_id}, status={agent_status}")

        return agent_run_id

    async def run_grader(
        self,
        *,
        critic_run_id: UUID,
        client: OpenAIModelProto,
        parent_run_id: UUID | None = None,
        verbose: bool = False,
        max_lines: int = DEFAULT_MAX_LINES,
        max_turns: int = 200,
        extra_handlers: tuple[BaseHandler, ...] = (),
    ) -> UUID:
        """Run a grader on a critic run. Acquires semaphore slot.

        Always uses builtin grader image for evaluation consistency.

        Args:
            critic_run_id: ID of the critic run to grade
            client: OpenAI-compatible model client
            parent_run_id: Optional parent agent run ID
            verbose: Whether to enable verbose display
            max_lines: Max lines per event in verbose display
            max_turns: Maximum agent turns before timeout
            extra_handlers: Additional handlers to add

        Returns:
            Grader run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_grader_impl(
                critic_run_id=critic_run_id,
                client=client,
                parent_run_id=parent_run_id,
                verbose=verbose,
                max_lines=max_lines,
                max_turns=max_turns,
                extra_handlers=extra_handlers,
            )

    async def _run_grader_impl(
        self,
        *,
        critic_run_id: UUID,
        client: OpenAIModelProto,
        parent_run_id: UUID | None,
        verbose: bool,
        max_lines: int,
        max_turns: int,
        extra_handlers: tuple[BaseHandler, ...],
    ) -> UUID:
        """Internal grader execution (semaphore already acquired)."""
        grader_run_id = uuid4()

        # Always use builtin grader image
        image_digest = resolve_image_ref(AgentType.GRADER, BUILTIN_TAG)
        image = build_oci_reference(AgentType.GRADER, image_digest)
        logger.info(f"Using builtin grader image: {image_digest}")

        # Load critic run and prepare canonical issues
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run is None:
                raise ValueError(f"Critic run {critic_run_id} not found in database")

            if not isinstance(critic_run.type_config, CriticTypeConfig):
                raise ValueError(f"Critic run {critic_run_id} has wrong type_config type")

            example_spec = critic_run.type_config.example
            snapshot_slug = example_spec.snapshot_slug

            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
            snapshot_split = snapshot.split

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

            # Write initial grader run
            session.add(
                AgentRun(
                    agent_run_id=grader_run_id,
                    image_digest=image_digest,
                    parent_agent_run_id=parent_run_id,
                    model=client.model,
                    type_config=type_config,
                    status=AgentRunStatus.IN_PROGRESS,
                )
            )
            session.commit()
            logger.info(f"Created grader run: agent_run_id={grader_run_id}, snapshot_slug={snapshot_slug}")

        # Set up environment
        comp_ctx = GraderAgentEnvironment(
            snapshot_slug=snapshot_slug,
            docker_client=self._docker_client,
            grader_run_id=grader_run_id,
            critic_run_id=critic_run_id,
            db_config=self._db_config,
            workspace_manager=self._workspace_manager,
            image=image,
        )

        agent_status: AgentRunStatus

        async with comp_ctx as compositor, Client(compositor) as mcp_client:
            # Build handlers
            def _grader_ready_state() -> bool:
                with get_session() as session:
                    found_run = session.get(AgentRun, grader_run_id)
                    return found_run is not None and found_run.status in (
                        AgentRunStatus.COMPLETED,
                        AgentRunStatus.MAX_TURNS_EXCEEDED,
                        AgentRunStatus.REPORTED_FAILURE,
                    )

            grader_handlers: list[BaseHandler] = [AbortIf(should_abort=_grader_ready_state)]
            if verbose:
                display_handler = await CompactDisplayHandler.from_compositor(
                    compositor,
                    max_lines=max_lines,
                    prefix=f"[GRADER {short_uuid(grader_run_id)} {snapshot_split} {snapshot_slug}] ",
                )
                grader_handlers.append(display_handler)

            grader_handlers.extend(
                [
                    RedirectOnTextMessageHandler(
                        reminder_message=(
                            "Text messages won't be delivered. Complete your grading decisions via MCP tools, "
                            "then call submit. If you encounter unrecoverable problems, call report_failure instead."
                        )
                    ),
                    *extra_handlers,
                    MaxTurnsHandler(max_turns=max_turns),
                ]
            )

            # Create AgentHandle
            agent_handle = await AgentHandle.create(
                agent_run_id=grader_run_id,
                image_digest=GRADER_IMAGE_REF,
                model_client=client,
                mcp_client=mcp_client,
                compositor=compositor,
                handlers=grader_handlers,
                dynamic_instructions=compositor.render_agent_dynamic_instructions,
                parallel_tool_calls=True,
                reasoning_summary=ReasoningSummary.DETAILED,
            )

            # Track as active
            async with self._lock:
                self._active[grader_run_id] = ActiveRun(handle=agent_handle, task=None)

            try:
                await agent_handle.run()

                # Validate database status
                with get_session() as session:
                    found_run = session.get(AgentRun, grader_run_id)
                    if found_run is None or found_run.status != AgentRunStatus.COMPLETED:
                        raise AgentDidNotSubmitError(AgentType.GRADER, grader_run_id)
                    agent_status = AgentRunStatus.COMPLETED

            except MaxTurnsExceededError:
                logger.warning(
                    f"Grader hit max turns limit ({max_turns}) for {snapshot_slug}, "
                    f"agent_run_id={short_uuid(grader_run_id)}"
                )
                with get_session() as session:
                    found_run = session.get(AgentRun, grader_run_id)
                    if found_run:
                        found_run.status = AgentRunStatus.MAX_TURNS_EXCEEDED
                        session.commit()
                agent_status = AgentRunStatus.MAX_TURNS_EXCEEDED
            finally:
                # Remove from active tracking
                async with self._lock:
                    self._active.pop(grader_run_id, None)

        logger.info(f"Grader run completed: agent_run_id={grader_run_id}, status={agent_status}")
        return grader_run_id

    # --- Snapshot Grader Daemon ---

    async def run_snapshot_grader(
        self,
        *,
        snapshot_slug: SnapshotSlug,
        client: OpenAIModelProto,
        verbose: bool = False,
        max_lines: int = DEFAULT_MAX_LINES,
        max_turns: int = 200,
    ) -> UUID:
        """Run a snapshot grader daemon. Blocks until shutdown or context exhausted.

        The daemon grades ALL critiques for the snapshot, sleeping when no drift
        and waking on pg_notify when GT changes or new critiques arrive.

        Always uses builtin grader image for evaluation consistency.

        Args:
            snapshot_slug: Snapshot this daemon is responsible for
            client: OpenAI-compatible model client
            verbose: Whether to enable verbose display
            max_lines: Max lines per event in verbose display
            max_turns: Max turns per wake cycle (resets after each sleep)

        Returns:
            Daemon run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_snapshot_grader_impl(
                snapshot_slug=snapshot_slug, client=client, verbose=verbose, max_lines=max_lines, max_turns=max_turns
            )

    async def _run_snapshot_grader_impl(
        self, *, snapshot_slug: SnapshotSlug, client: OpenAIModelProto, verbose: bool, max_lines: int, max_turns: int
    ) -> UUID:
        """Internal snapshot grader daemon execution."""
        grader_run_id = uuid4()

        # Always use builtin grader image
        image_digest = resolve_image_ref(AgentType.GRADER, BUILTIN_TAG)
        image = build_oci_reference(AgentType.GRADER, image_digest)
        logger.info(f"Using builtin grader image: {image_digest}")

        # Verify snapshot exists
        with get_session() as session:
            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one_or_none()
            if snapshot is None:
                raise ValueError(f"Snapshot '{snapshot_slug}' not found")

            type_config = SnapshotGraderTypeConfig(snapshot_slug=snapshot_slug).model_dump(mode="json")

            # Create initial daemon run
            session.add(
                AgentRun(
                    agent_run_id=grader_run_id,
                    image_digest=image_digest,
                    parent_agent_run_id=None,
                    model=client.model,
                    type_config=type_config,
                    status=AgentRunStatus.IN_PROGRESS,
                )
            )
            session.commit()
            logger.info(f"Created snapshot grader daemon: agent_run_id={grader_run_id}, snapshot={snapshot_slug}")

        # Set up environment
        env = SnapshotGraderAgentEnvironment(
            snapshot_slug=snapshot_slug,
            docker_client=self._docker_client,
            grader_run_id=grader_run_id,
            db_config=self._db_config,
            workspace_manager=self._workspace_manager,
            image=image,
        )

        agent_status: AgentRunStatus = AgentRunStatus.IN_PROGRESS

        async with (
            env as compositor,
            Client(compositor) as mcp_client,
            GraderDaemonScaffold(snapshot_slug, self._db_config) as scaffold,
        ):
            # Build handlers
            drift_handler = scaffold.create_drift_handler()

            daemon_handlers: list[BaseHandler] = [drift_handler]
            if verbose:
                display_handler = await CompactDisplayHandler.from_compositor(
                    compositor,
                    max_lines=max_lines,
                    prefix=f"[SNAPSHOT-GRADER {short_uuid(grader_run_id)} {snapshot_slug}] ",
                )
                daemon_handlers.append(display_handler)

            daemon_handlers.extend(
                [
                    RedirectOnTextMessageHandler(
                        reminder_message=(
                            "Text messages won't be delivered. Use props grader-agent CLI commands "
                            "to list pending edges, match issues to GT, and fill remaining edges."
                        )
                    ),
                    MaxTurnsHandler(max_turns=max_turns),
                ]
            )

            # Create AgentHandle
            agent_handle = await AgentHandle.create(
                agent_run_id=grader_run_id,
                image_digest=GRADER_IMAGE_REF,
                model_client=client,
                mcp_client=mcp_client,
                compositor=compositor,
                handlers=daemon_handlers,
                dynamic_instructions=compositor.render_agent_dynamic_instructions,
                parallel_tool_calls=True,
                reasoning_summary=ReasoningSummary.DETAILED,
            )

            # Track as active
            async with self._lock:
                self._active[grader_run_id] = ActiveRun(handle=agent_handle, task=None)

            try:
                # Daemon loop: run → sleep → wake → run → ...
                while not scaffold.is_shutdown:
                    try:
                        await agent_handle.run()
                    except MaxTurnsExceededError:
                        logger.warning("Snapshot grader hit max turns, resetting for next wake cycle")
                        # Reset turn count by recreating handlers with fresh MaxTurnsHandler
                        # For now, just continue to sleep/wake cycle

                    # Wait for next work (blocks until pg_notify or drift detected)
                    notifs = await scaffold.wait_for_drift_or_notification()

                    if scaffold.is_shutdown:
                        break

                    # Inject wake message and continue
                    if notifs:
                        wake_msg = format_notifications(notifs)
                        agent_handle.agent.process_message(UserMessage.text(wake_msg))
                    else:
                        agent_handle.agent.process_message(
                            UserMessage.text("Drift detected. Check grading_pending for new work.")
                        )

                agent_status = AgentRunStatus.COMPLETED

            except ContextLengthExceededError:
                logger.warning(f"Snapshot grader {grader_run_id} hit context limit")
                agent_status = AgentRunStatus.CONTEXT_LENGTH_EXCEEDED
            except Exception as e:
                logger.error(f"Snapshot grader {grader_run_id} failed: {e}", exc_info=True)
                agent_status = AgentRunStatus.REPORTED_FAILURE
            finally:
                # Remove from active tracking
                async with self._lock:
                    self._active.pop(grader_run_id, None)

                # Update status in DB
                with get_session() as session:
                    found_run = session.get(AgentRun, grader_run_id)
                    if found_run and found_run.status == AgentRunStatus.IN_PROGRESS:
                        found_run.status = agent_status
                        session.commit()

        logger.info(f"Snapshot grader daemon exited: agent_run_id={grader_run_id}, status={agent_status}")
        return grader_run_id

    # --- State Tracking ---

    def get(self, run_id: UUID) -> AgentRunView | None:
        """Get agent run view from memory (if active) or DB."""
        if run_id in self._active:
            active = self._active[run_id]
            return AgentRunView(
                agent_run_id=run_id,
                image_digest=active.handle.image_digest,
                model=active.handle.agent.model,
                status=AgentRunStatus.IN_PROGRESS,
                created_at=datetime.now(),
                handle=active.handle,
            )

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
                handle=None,
            )

    def list_active(self) -> list[AgentRunView]:
        """List all active (in-memory) runs."""
        result = []
        for run_id, active in self._active.items():
            result.append(
                AgentRunView(
                    agent_run_id=run_id,
                    image_digest=active.handle.image_digest,
                    model=active.handle.agent.model,
                    status=AgentRunStatus.IN_PROGRESS,
                    created_at=datetime.now(),
                    handle=active.handle,
                )
            )
        return result

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
                    handle=self._active.get(r.agent_run_id, ActiveRun(handle=None)).handle  # type: ignore
                    if r.agent_run_id in self._active
                    else None,
                )
                for r in runs
            ]

    async def cancel(self, run_id: UUID) -> bool:
        """Cancel a running agent.

        Returns True if cancelled, False if not found or not running.
        """
        if run_id not in self._active:
            return False

        active = self._active[run_id]
        if active.task is None or active.task.done():
            return False

        active.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await active.task

        self._active.pop(run_id, None)
        logger.info(f"Cancelled agent: {run_id}")
        return True
