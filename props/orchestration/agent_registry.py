"""Agent registry - unified orchestration layer for agent runs.

AgentRegistry is THE entry point for running agents. It owns shared resources
(Docker client, database config).

In-container architecture:
- Container runs its own agent loop (CMD entrypoint)
- Container talks to LLM proxy (OPENAI_BASE_URL env var)
- Container connects to backend REST API for eval operations (PROPS_BACKEND_URL env var)
- Container exits 0 on success, non-zero on failure
- Host scaffold: creates temp DB user, starts container, waits for exit

Usage:
    registry = AgentRegistry(
        docker_client=docker_client,
        db=db,
        agent_base_env=config.agent_env,
        registry_config=RegistryProxyConfig(host="127.0.0.1", port=8000),
    )
    async with registry:
        critic_run_id = await registry.run_critic(
            image_ref=BUILTIN_TAG,
            example=example,
            model="gpt-4o",
            timeout_seconds=3600,
            parent_run_id=None,
            budget_usd=None,
        )
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Annotated, Literal
from uuid import UUID, uuid4

import aiodocker
import httpx
from pydantic import BaseModel, Field

from props.core.agent_types import (
    AgentType,
    CriticTypeConfig,
    GraderTypeConfig,
    ImprovementTypeConfig,
    PromptOptimizerTypeConfig,
)
from props.core.display import short_uuid
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import BUILTIN_TAG, RegistryProxyConfig, is_digest
from props.critic_dev.improve.main import TerminationSuccess
from props.critic_dev.shared import TargetMetric
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, Snapshot
from props.orchestration.loop_agent_env import ContainerResult, run_loop_agent

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
    """

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        db: Database,
        db_config: DatabaseConfig,
        agent_base_env: dict[str, str],
        registry_config: RegistryProxyConfig,
        extra_hosts: dict[str, str] | None = None,
    ) -> None:
        self._docker_client = docker_client
        self._db = db
        self._db_config = db_config
        self._agent_base_env = agent_base_env
        self._registry_config = registry_config
        self._extra_hosts = extra_hosts

    async def close(self) -> None:
        await self._docker_client.close()

    async def __aenter__(self) -> AgentRegistry:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.close()

    # --- Image Resolution ---

    # TODO: Consider consolidating with resolve_image_ref_async in oci_utils.py
    # (that one resolves via Docker daemon inspect/pull; this one resolves tags
    # to digests via the registry proxy).

    async def _resolve_image_ref(self, agent_type: AgentType, ref: str) -> str:
        """Resolve image reference to digest via registry proxy.

        Uses self._db_config credentials for HTTP basic auth to the proxy.

        Returns:
            Digest (sha256:...) - either the provided digest or resolved from tag

        Raises:
            ValueError: If tag doesn't exist or proxy returns error
        """
        if is_digest(ref):
            logger.debug(f"Reference {ref} is already a digest, returning as-is")
            return ref

        repository = str(agent_type)

        proxy_url = self._registry_config.proxy_url
        manifest_url = f"{proxy_url}/v2/{repository}/manifests/{ref}"
        headers = {
            "Accept": ", ".join(
                ["application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"]
            )
        }
        auth = httpx.BasicAuth(self._db_config.user, self._db_config.password)

        logger.info(f"Resolving tag {repository}:{ref} via proxy at {proxy_url}")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.head(manifest_url, headers=headers, auth=auth, timeout=10)
        except httpx.HTTPError as e:
            raise ValueError(f"Failed to resolve tag {repository}:{ref}: {e}")

        if resp.status_code == 404:
            raise ValueError(f"Image not found: {repository}:{ref}")

        if resp.status_code != 200:
            raise ValueError(f"Proxy returned error {resp.status_code} for {repository}:{ref}: {resp.text}")

        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            raise ValueError(f"Proxy didn't return Docker-Content-Digest header for {repository}:{ref}")

        logger.info(f"Resolved {repository}:{ref} → {digest}")
        return str(digest)

    async def _resolve_image(self, agent_type: AgentType, ref: str) -> tuple[str, str]:
        """Resolve image ref to (digest, full OCI reference)."""
        digest = await self._resolve_image_ref(agent_type, ref)
        oci_ref = self._registry_config.build_oci_reference(agent_type, digest)
        return digest, oci_ref

    def _finalize_run(self, result: ContainerResult, agent_run_id: UUID) -> AgentRunStatus:
        """Interpret container exit, update AgentRun status in DB, return final status."""
        status = self._interpret_container_result(result, agent_run_id)
        with self._db.session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            if found_run.status == AgentRunStatus.IN_PROGRESS:
                found_run.status = status
                session.commit()
                logger.info(f"Updated {agent_run_id} status to {status}")
        return status

    # --- Execution Methods ---

    async def run_critic(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        model: str,
        timeout_seconds: int,
        parent_run_id: UUID | None,
        budget_usd: float | None,
    ) -> UUID:
        """Run a critic agent. Returns agent run ID (query DB for status)."""
        agent_run_id = uuid4()
        image_digest, image = await self._resolve_image(AgentType.CRITIC, image_ref)

        with self._db.session() as session:
            session.query(Snapshot).filter_by(slug=example.snapshot_slug).one()

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                parent_agent_run_id=parent_run_id,
                model=model,
                type_config=CriticTypeConfig(example=example),
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            image=image,
            agent_base_env=self._agent_base_env,
            registry_config=self._registry_config,
            timeout_seconds=timeout_seconds,
            container_name=f"critic-{short_uuid(agent_run_id)}",
            extra_hosts=self._extra_hosts,
        )

        self._finalize_run(result, agent_run_id)
        return agent_run_id

    async def run_prompt_optimizer(
        self,
        *,
        budget: float,
        optimizer_model: str,
        critic_model: str,
        target_metric: TargetMetric,
        timeout_seconds: int,
    ) -> UUID:
        """Run a prompt optimizer agent. Returns agent run ID (query DB for status)."""
        agent_run_id = uuid4()
        image_digest, image = await self._resolve_image(AgentType.PROMPT_OPTIMIZER, BUILTIN_TAG)

        with self._db.session() as session:
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

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            image=image,
            agent_base_env=self._agent_base_env,
            registry_config=self._registry_config,
            container_name=f"promptopt-{short_uuid(agent_run_id)}",
            timeout_seconds=timeout_seconds,
            extra_hosts=self._extra_hosts,
        )

        self._finalize_run(result, agent_run_id)
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
        """Run an improvement agent that creates definitions to beat baselines on the allowed examples."""
        if not examples:
            raise ValueError("examples must not be empty")

        run_id = uuid4()
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix=f"improve_agent_{str(run_id)[:8]}_"))

        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        image_digest, image = await self._resolve_image(AgentType.IMPROVEMENT, BUILTIN_TAG)

        type_config = ImprovementTypeConfig(
            baseline_image_refs=baseline_image_refs,
            allowed_examples=examples,
            improvement_model=improvement_model,
            critic_model=critic_model,
            grader_model=critic_model,  # Not actively used (grading by daemons)
        )

        with self._db.session() as session:
            agent_run = AgentRun(
                agent_run_id=run_id,
                image_digest=image_digest,
                model=improvement_model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=run_id,
            db_config=self._db_config,
            image=image,
            agent_base_env=self._agent_base_env,
            registry_config=self._registry_config,
            container_name=f"improve-{short_uuid(run_id)}",
            timeout_seconds=timeout_seconds,
            extra_hosts=self._extra_hosts,
        )

        final_status = self._finalize_run(result, run_id)

        # Determine outcome from final status
        outcome: ImprovementOutcome
        if final_status == AgentRunStatus.TIMED_OUT:
            outcome = OutcomeUnexpectedTermination(message=f"Container timed out after {timeout_seconds} seconds")
        elif final_status == AgentRunStatus.COMPLETED:
            outcome = OutcomeExhausted()  # TODO: Parse actual success details from DB
        else:
            outcome = OutcomeUnexpectedTermination(message=f"Container exited with code {result.exit_code}")

        return ImprovementResult(tokens_used=0, run_id=run_id, outcome=outcome)  # TODO: Track tokens

    def _interpret_container_result(self, result: ContainerResult, agent_run_id: UUID) -> AgentRunStatus:
        if result.exit_code == 0:
            # Check DB - container should have set status to COMPLETED
            with self._db.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run and run.status == AgentRunStatus.COMPLETED:
                    return AgentRunStatus.COMPLETED
                # Container exited 0 but didn't submit - unexpected
                logger.warning(f"Container exited 0 but status is {run.status if run else 'None'}")
                return AgentRunStatus.COMPLETED
        elif result.exit_code == -1:
            # Timeout
            logger.warning(f"Container timed out: {agent_run_id}")
            return AgentRunStatus.TIMED_OUT
        else:
            # Non-zero exit - check if container set REPORTED_FAILURE
            with self._db.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run and run.status == AgentRunStatus.REPORTED_FAILURE:
                    return AgentRunStatus.REPORTED_FAILURE
            logger.error(f"Container failed with exit code {result.exit_code}: stderr={result.stderr[:500]}")
            return AgentRunStatus.REPORTED_FAILURE

    # --- State Tracking ---

    def get(self, run_id: UUID) -> AgentRunView | None:
        with self._db.session() as session:
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
        with self._db.session() as session:
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

    async def run_snapshot_grader(self, *, snapshot_slug: SnapshotSlug, model: str) -> UUID:
        """Run a snapshot grader daemon. Blocks until daemon exits.

        The grader daemon listens for pg_notify on grading_pending channel, grades all
        critiques for the snapshot until no drift remains, sleeps when no drift.
        Daemons run indefinitely until cancelled.
        """
        agent_run_id = uuid4()
        image_digest, image = await self._resolve_image(AgentType.GRADER, BUILTIN_TAG)

        with self._db.session() as session:
            session.query(Snapshot).filter_by(slug=snapshot_slug).one()

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                model=model,
                type_config=GraderTypeConfig(snapshot_slug=snapshot_slug),
                status=AgentRunStatus.IN_PROGRESS,
            )
            session.add(agent_run)
            session.commit()

        result = await run_loop_agent(
            docker_client=self._docker_client,
            agent_run_id=agent_run_id,
            db_config=self._db_config,
            image=image,
            agent_base_env=self._agent_base_env,
            registry_config=self._registry_config,
            container_name=f"grader-{short_uuid(agent_run_id)}",
            extra_hosts=self._extra_hosts,
        )

        self._finalize_run(result, agent_run_id)
        return agent_run_id
