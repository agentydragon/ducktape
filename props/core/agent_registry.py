"""Agent registry - unified orchestration layer for agent runs.

AgentRegistry is THE entry point for running agents. It owns shared resources
(Docker client, database config) and manages concurrency via an internal semaphore.

In-container architecture:
- Container runs its own agent loop (CMD entrypoint)
- Container talks to LLM proxy (OPENAI_BASE_URL env var)
- Container connects to backend REST API for eval operations (PROPS_BACKEND_URL env var)
- Container exits 0 on success, non-zero on failure
- Host scaffold: creates temp DB user, starts container, waits for exit

Usage:
    registry = AgentRegistry(
        docker_client=docker_client,
        db_config=db_config,
        llm_proxy_url="http://props-backend:8000",
    )
    async with registry:
        critic_run_id = await registry.run_critic(
            image_ref="builtin",
            example=example,
            model="gpt-4o",
            timeout_seconds=3600,
        )
        # Check status from DB
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run.status == AgentRunStatus.COMPLETED:
                # Grading is handled by snapshot grader daemons
                pass
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid4

if TYPE_CHECKING:
    pass

import aiodocker
from pydantic import BaseModel, Field

from props.core.agent_types import (
    AgentType,
    CriticTypeConfig,
    ImprovementTypeConfig,
    PromptOptimizerTypeConfig,
    SnapshotGraderTypeConfig,
)
from props.core.display import short_uuid
from props.core.ids import SnapshotSlug
from props.core.loop_agent_env import ContainerResult, run_loop_agent
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref
from props.core.splits import Split
from props.critic_dev.improve.main import TerminationSuccess
from props.critic_dev.shared import TargetMetric
from props.db.config import DatabaseConfig
from props.db.models import AgentRun, AgentRunStatus, Snapshot
from props.db.session import get_session

logger = logging.getLogger(__name__)


# --- Improvement Agent Result Types ---


class OutcomeExhausted(BaseModel):
    kind: Literal["exhausted"] = "exhausted"


class OutcomeUnexpectedTermination(BaseModel):
    kind: Literal["unexpected_termination"] = "unexpected_termination"
    message: str


ImprovementOutcome = Annotated[
    TerminationSuccess | OutcomeExhausted | OutcomeUnexpectedTermination, Field(discriminator="kind")
]


class ImprovementResult(BaseModel):
    tokens_used: int
    run_id: UUID
    outcome: ImprovementOutcome


# --- Agent Run View ---


@dataclass
class AgentRunView:
    """Unified view of an agent run from DB."""

    agent_run_id: UUID
    image_digest: str
    model: str
    status: AgentRunStatus
    created_at: datetime


class AgentRegistry:
    """Unified orchestration layer for agent runs using in-container architecture.

    Owns shared resources and provides the single entry point for execution.
    Manages concurrency via internal semaphore.
    """

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        llm_proxy_url: str,
        max_parallel: int = 4,
        extra_hosts: dict[str, str] | None = None,
    ) -> None:
        self._docker_client = docker_client
        self._db_config = db_config
        self._llm_proxy_url = llm_proxy_url
        self._extra_hosts = extra_hosts
        self._semaphore = asyncio.Semaphore(max_parallel)

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
        timeout_seconds: int,
        parent_run_id: UUID | None = None,
        budget_usd: float | None = None,
    ) -> UUID:
        """Run a critic agent using in-container architecture.

        The container runs its own agent loop and exits 0 on success.

        Args:
            image_ref: Image reference (tag or digest)
            example: Example specification (snapshot + scope)
            model: Model name (e.g., "gpt-4o")
            timeout_seconds: Max seconds before container is killed
            parent_run_id: Optional parent agent run ID
            budget_usd: Optional spending budget (not yet implemented)

        Returns:
            Agent run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_critic_impl(
                image_ref=image_ref,
                example=example,
                model=model,
                timeout_seconds=timeout_seconds,
                parent_run_id=parent_run_id,
                budget_usd=budget_usd,
            )

    async def _run_critic_impl(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        model: str,
        timeout_seconds: int,
        parent_run_id: UUID | None,
        budget_usd: float | None,
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
            session.query(Snapshot).filter_by(slug=snapshot_slug).one()

            type_config = CriticTypeConfig(example=example)

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                parent_agent_run_id=parent_run_id,
                model=model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()
            logger.info(f"Created critic run: agent_run_id={agent_run_id}, snapshot_slug={snapshot_slug}")

        # Phase 2: Run container with in-container agent loop
        extra_env = {"MODEL": model}
        if budget_usd is not None:
            extra_env["BUDGET_USD"] = str(budget_usd)

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            image=image,
            llm_proxy_url=self._llm_proxy_url,
            timeout_seconds=timeout_seconds,
            extra_env=extra_env,
            container_name=f"critic-{short_uuid(agent_run_id)}",
            extra_hosts=self._extra_hosts,
        )

        # Phase 3: Interpret exit code and update status
        agent_status = self._interpret_container_result(result, agent_run_id)

        with get_session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            # Only update if still IN_PROGRESS (container may have set COMPLETED/REPORTED_FAILURE)
            if found_run.status == AgentRunStatus.IN_PROGRESS:
                found_run.status = agent_status
                session.commit()
                logger.info(f"Updated critic run: agent_run_id={agent_run_id}, status={agent_status}")

        return agent_run_id

    async def run_prompt_optimizer(
        self,
        *,
        budget: float,
        optimizer_model: str,
        critic_model: str,
        target_metric: TargetMetric,
        timeout_seconds: int,
        image_ref: str = BUILTIN_TAG,
    ) -> UUID:
        """Run a prompt optimizer agent using in-container architecture.

        The optimizer container:
        1. Talks to LLM proxy for agent responses
        2. Connects to MCP server on host for run_critic/wait_until_graded tools
        3. Can orchestrate critic sub-runs via the MCP tools

        Args:
            budget: Cost budget in USD
            optimizer_model: Model name for the optimizer agent
            critic_model: Model name for critic sub-runs
            target_metric: WHOLE_REPO or TARGETED
            timeout_seconds: Max seconds before container is killed
            image_ref: Optimizer image reference (default: builtin)

        Returns:
            Agent run ID (query DB for status)
        """
        async with self._semaphore:
            return await self._run_prompt_optimizer_impl(
                budget=budget,
                optimizer_model=optimizer_model,
                critic_model=critic_model,
                target_metric=target_metric,
                timeout_seconds=timeout_seconds,
                image_ref=image_ref,
            )

    async def _run_prompt_optimizer_impl(
        self,
        *,
        budget: float,
        optimizer_model: str,
        critic_model: str,
        target_metric: TargetMetric,
        timeout_seconds: int,
        image_ref: str,
    ) -> UUID:
        """Internal prompt optimizer execution (semaphore already acquired)."""
        # Get train snapshots from database
        with get_session() as session:
            train_snapshots = session.query(Snapshot).filter_by(split=Split.TRAIN).all()
            train_slugs = [SnapshotSlug(s.slug) for s in train_snapshots]

        logger.info(f"Using {len(train_slugs)} train snapshots (agent will fetch from database)")

        # Generate unique ID for this run
        agent_run_id = uuid4()
        logger.info(f"Prompt optimizer agent_run_id: {agent_run_id}")

        # Resolve image reference to digest and construct full OCI reference
        image_digest = resolve_image_ref(AgentType.PROMPT_OPTIMIZER, image_ref)
        image = build_oci_reference(AgentType.PROMPT_OPTIMIZER, image_digest)
        logger.info(f"Resolved prompt-optimizer image {image_ref} → {image}")

        # Phase 1: Write initial AgentRun to DB (BEFORE agent runs - FK constraint!)
        with get_session() as session:
            type_config = PromptOptimizerTypeConfig(
                target_metric=target_metric,
                optimizer_model=optimizer_model,
                critic_model=critic_model,
                grader_model=critic_model,  # Not actively used (grading by daemons)
                budget_limit=budget,
            )

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                model=optimizer_model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()

        logger.info(f"Created prompt optimizer AgentRun: {agent_run_id}")

        try:
            # Run the container with in-container agent loop
            # Container uses REST API (PROPS_BACKEND_URL) instead of MCP
            result = await run_loop_agent(
                docker_client=self._docker_client,
                agent_run_id=agent_run_id,
                db_config=self._db_config,
                image=image,
                llm_proxy_url=self._llm_proxy_url,
                extra_env={
                    # Backend URL for eval API (run_critic, wait_until_graded)
                    "PROPS_BACKEND_URL": self._llm_proxy_url,
                    # Critic model for eval tools
                    "PROPS_CRITIC_MODEL": critic_model,
                },
                container_name=f"promptopt-{short_uuid(agent_run_id)}",
                timeout_seconds=timeout_seconds,
                extra_hosts=self._extra_hosts,
            )

            timed_out = result.exit_code == -1
            if timed_out:
                logger.error(f"Container timed out after {timeout_seconds} seconds")
            else:
                logger.info(f"Container exited with code {result.exit_code}")
            if result.stderr:
                logger.info(f"Container stderr:\n{result.stderr}")

            # Update status based on exit code
            if timed_out:
                final_status = AgentRunStatus.TIMED_OUT
            elif result.exit_code == 0:
                final_status = AgentRunStatus.COMPLETED
            else:
                final_status = AgentRunStatus.REPORTED_FAILURE
            with get_session() as session:
                found_run = session.get(AgentRun, agent_run_id)
                if found_run:
                    found_run.status = final_status
                    session.commit()
                    logger.info(f"Updated agent_run status to {final_status.value}")

        finally:
            pass  # No cleanup needed - we use self as the registry

        logger.info("Optimization session complete.")
        logger.info(f"Budget: ${budget:.2f}")

        return agent_run_id

    async def run_improvement_agent(
        self,
        *,
        examples: list[ExampleSpec],
        baseline_image_refs: list[str],
        token_budget: int,
        improvement_model: str,
        critic_model: str,
        timeout_seconds: int,
        output_dir: Path | None = None,
    ) -> ImprovementResult:
        """Run an improvement agent using in-container architecture.

        The improvement agent creates agent definitions to beat baselines
        on the allowed examples.

        Args:
            examples: Examples the agent is allowed to optimize for
            baseline_image_refs: Baseline images to beat
            token_budget: Token budget limit
            improvement_model: Model name for the improvement agent
            critic_model: Model name for critic sub-runs
            timeout_seconds: Max seconds before container is killed
            output_dir: Optional output directory for agent artifacts

        Returns:
            ImprovementResult with run_id, tokens_used, and outcome
        """
        async with self._semaphore:
            return await self._run_improvement_agent_impl(
                examples=examples,
                baseline_image_refs=baseline_image_refs,
                token_budget=token_budget,
                improvement_model=improvement_model,
                critic_model=critic_model,
                timeout_seconds=timeout_seconds,
                output_dir=output_dir,
            )

    async def _run_improvement_agent_impl(
        self,
        *,
        examples: list[ExampleSpec],
        baseline_image_refs: list[str],
        token_budget: int,
        improvement_model: str,
        critic_model: str,
        timeout_seconds: int,
        output_dir: Path | None,
    ) -> ImprovementResult:
        """Internal improvement agent execution (semaphore already acquired)."""
        if not examples:
            raise ValueError("examples must not be empty")

        run_id = uuid4()
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix=f"improve_agent_{str(run_id)[:8]}_"))

        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"Starting improvement agent run {run_id}: "
            f"{len(examples)} examples, {token_budget:,} token budget, model={improvement_model}"
        )
        logger.info(f"Output directory: {output_dir}")

        # Always use builtin improvement image
        image_digest = resolve_image_ref(AgentType.IMPROVEMENT, BUILTIN_TAG)
        image = build_oci_reference(AgentType.IMPROVEMENT, image_digest)
        logger.info(f"Using builtin improvement image: {image_digest}")

        type_config = ImprovementTypeConfig(
            baseline_image_refs=baseline_image_refs,
            allowed_examples=examples,
            improvement_model=improvement_model,
            critic_model=critic_model,
            grader_model=critic_model,  # Not actively used (grading by daemons)
        )

        with get_session() as session:
            agent_run = AgentRun(
                agent_run_id=run_id,
                image_digest=image_digest,
                model=improvement_model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()

        try:
            # Run the container with in-container agent loop
            # Container uses REST API (PROPS_BACKEND_URL) instead of MCP
            result = await run_loop_agent(
                docker_client=self._docker_client,
                agent_run_id=run_id,
                db_config=self._db_config,
                image=image,
                llm_proxy_url=self._llm_proxy_url,
                extra_env={
                    # Backend URL for eval API (run_critic, wait_until_graded)
                    "PROPS_BACKEND_URL": self._llm_proxy_url,
                    # Critic model for eval tools
                    "PROPS_CRITIC_MODEL": critic_model,
                },
                container_name=f"improve-{short_uuid(run_id)}",
                timeout_seconds=timeout_seconds,
                extra_hosts=self._extra_hosts,
            )

            timed_out = result.exit_code == -1
            if timed_out:
                logger.error(f"Container timed out after {timeout_seconds} seconds")
            else:
                logger.info(f"Container exited with code {result.exit_code}")
            if result.stderr:
                logger.info(f"Container stderr:\n{result.stderr}")

            # Update status based on exit code
            if timed_out:
                final_status = AgentRunStatus.TIMED_OUT
            elif result.exit_code == 0:
                final_status = AgentRunStatus.COMPLETED
            else:
                final_status = AgentRunStatus.REPORTED_FAILURE
            with get_session() as session:
                found_run = session.get(AgentRun, run_id)
                if found_run:
                    found_run.status = final_status
                    found_run.container_exit_code = result.exit_code if not timed_out else None
                    session.commit()
                    logger.info(f"Updated agent_run status to {final_status.value}")

            # Determine outcome
            outcome: ImprovementOutcome
            if timed_out:
                outcome = OutcomeUnexpectedTermination(message=f"Container timed out after {timeout_seconds} seconds")
            elif result.exit_code == 0:
                # Success - for now just return exhausted (container writes details to DB)
                outcome = OutcomeExhausted()  # TODO: Parse actual success details from DB
            else:
                outcome = OutcomeUnexpectedTermination(message=f"Container exited with code {result.exit_code}")

            logger.info(f"Improvement agent completed: kind={outcome.kind}")
            return ImprovementResult(tokens_used=0, run_id=run_id, outcome=outcome)  # TODO: Track tokens

        finally:
            pass  # No cleanup needed

    def _interpret_container_result(self, result: ContainerResult, agent_run_id: UUID) -> AgentRunStatus:
        """Map container exit code to AgentRunStatus."""
        if result.exit_code == 0:
            # Check DB - container should have set status to COMPLETED
            with get_session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run and run.status == AgentRunStatus.COMPLETED:
                    return AgentRunStatus.COMPLETED
                # Container exited 0 but didn't submit - unexpected
                logger.warning(f"Container exited 0 but status is {run.status if run else 'None'}")
                return AgentRunStatus.COMPLETED
        elif result.exit_code == -1:
            # Timeout
            logger.warning(f"Container timed out: {agent_run_id}")
            return AgentRunStatus.MAX_TURNS_EXCEEDED
        else:
            # Non-zero exit - check if container set REPORTED_FAILURE
            with get_session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run and run.status == AgentRunStatus.REPORTED_FAILURE:
                    return AgentRunStatus.REPORTED_FAILURE
            logger.error(f"Container failed with exit code {result.exit_code}: {result.stderr[:500]}")
            return AgentRunStatus.REPORTED_FAILURE

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

    async def run_snapshot_grader(
        self, *, snapshot_slug: SnapshotSlug, model: str, timeout_seconds: int, image_ref: str = "grader"
    ) -> UUID:
        """Run a snapshot grader daemon. Blocks until daemon exits or timeout.

        The grader daemon:
        1. Listens for pg_notify on grading_pending channel
        2. Grades all critiques for the snapshot until no drift remains
        3. Sleeps when no drift, wakes on pg_notify
        4. Exits when timeout reached or shutdown signal received

        Args:
            snapshot_slug: Snapshot this grader is responsible for
            model: Model name (e.g., "gpt-4o")
            timeout_seconds: Max seconds before container is killed
            image_ref: Image reference (tag or digest), defaults to "grader"

        Returns:
            Agent run ID
        """
        async with self._semaphore:
            return await self._run_snapshot_grader_impl(
                snapshot_slug=snapshot_slug, model=model, timeout_seconds=timeout_seconds, image_ref=image_ref
            )

    async def _run_snapshot_grader_impl(
        self, *, snapshot_slug: SnapshotSlug, model: str, timeout_seconds: int, image_ref: str
    ) -> UUID:
        """Internal snapshot grader execution (semaphore already acquired)."""
        agent_run_id = uuid4()

        # Resolve image reference to digest
        image_digest = resolve_image_ref(AgentType.SNAPSHOT_GRADER, image_ref)
        image = build_oci_reference(AgentType.SNAPSHOT_GRADER, image_digest)
        logger.info(f"Resolved snapshot_grader image {image_ref} → {image_digest}")

        # Phase 1: Write initial AgentRun to DB
        with get_session() as session:
            # Verify snapshot exists
            session.query(Snapshot).filter_by(slug=snapshot_slug).one()

            type_config = SnapshotGraderTypeConfig(snapshot_slug=str(snapshot_slug))

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                model=model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()
            logger.info(f"Created snapshot_grader run: agent_run_id={agent_run_id}, snapshot_slug={snapshot_slug}")

        # Phase 2: Run container with in-container agent loop
        extra_env = {"MODEL": model}

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            image=image,
            llm_proxy_url=self._llm_proxy_url,
            timeout_seconds=timeout_seconds,
            extra_env=extra_env,
            container_name=f"grader-{short_uuid(agent_run_id)}",
            extra_hosts=self._extra_hosts,
        )

        # Phase 3: Interpret exit code and update status
        agent_status = self._interpret_container_result(result, agent_run_id)

        with get_session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            # Only update if still IN_PROGRESS
            if found_run.status == AgentRunStatus.IN_PROGRESS:
                found_run.status = agent_status
                session.commit()
                logger.info(f"Updated snapshot_grader run: agent_run_id={agent_run_id}, status={agent_status}")

        return agent_run_id
