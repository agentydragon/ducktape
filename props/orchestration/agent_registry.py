"""Agent registry - unified orchestration layer for agent runs.

AgentRegistry is THE entry point for running agents. It owns shared resources
(container executor, database config).

In-container architecture:
- Container runs its own agent loop (CMD entrypoint)
- Container talks to LLM proxy (OPENAI_BASE_URL env var)
- Container connects to backend REST API for eval operations (PROPS_BACKEND_URL env var)
- Container exits 0 on success, non-zero on failure
- Host scaffold: creates temp DB user, starts container, waits for exit

Usage:
    from props.orchestration.docker_executor import DockerExecutor

    executor = DockerExecutor(docker_client, network_name="props-agents")
    registry = AgentRegistry(
        executor=executor,
        db=db,
        backend_url="http://props-backend:8000",
        agent_base_env=config.agent_env,
        registry_config=RegistryProxyConfig(host="127.0.0.1", port=8000),
    )
    async with registry:
        image = await registry.resolve_image(AgentType.CRITIC, BUILTIN_TAG)
        critic_run_id = await registry.run_critic(
            image=image,
            example=example,
            model="gpt-4o",
            timeout_seconds=3600,
            parent_run_id=None,
            budget_usd=5.0,
        )
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

import httpx

from props.core.agent_types import (
    AgentType,
    CriticDevImproveTypeConfig,
    CriticDevOptimizeTypeConfig,
    CriticTypeConfig,
    GraderTypeConfig,
    TargetMetric,
    TypeConfig,
)
from props.core.display import short_uuid
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import RegistryProxyConfig, is_digest
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunBudgetStatus, AgentRunStatus, Snapshot
from props.orchestration.agent_credentials import ensure_agent_role
from props.orchestration.executor import ContainerExecutor, ContainerHandle, ContainerResult, Exited, TimedOut

logger = logging.getLogger(__name__)


# --- Exceptions ---


class BudgetExceededError(Exception):
    """Raised when a child agent's requested budget exceeds parent's remaining budget."""


class ImageResolutionError(Exception):
    """Raised when an image reference cannot be resolved or pulled."""


# --- Resolved Image ---


@dataclass(frozen=True)
class ResolvedImage:
    """Pre-resolved OCI image reference. Use resolve_image() to create."""

    digest: str
    oci_ref: str


# --- Agent Run View ---


@dataclass
class AgentRunView:
    """Unified view of an agent run from DB."""

    agent_run_id: UUID
    image_digest: str
    model: str
    status: AgentRunStatus
    created_at: datetime

    @classmethod
    def from_orm(cls, run: AgentRun) -> AgentRunView:
        return cls(
            agent_run_id=run.agent_run_id,
            image_digest=run.image_digest,
            model=run.model,
            status=run.status,
            created_at=run.created_at,
        )


class AgentRegistry:
    """Unified orchestration layer for agent runs using in-container architecture.

    Owns shared resources and provides the single entry point for execution.
    Container lifecycle is delegated to a ContainerExecutor (Docker, Kubernetes, etc.).
    """

    def __init__(
        self,
        executor: ContainerExecutor,
        db: Database,
        db_config: DatabaseConfig,
        backend_url: str,
        agent_base_env: dict[str, str],
        registry_config: RegistryProxyConfig,
    ) -> None:
        self._executor = executor
        self._db = db
        self._db_config = db_config
        self._backend_url = backend_url
        self._agent_base_env = agent_base_env
        self._registry_config = registry_config

    async def close(self) -> None:
        await self._executor.close()

    async def __aenter__(self) -> AgentRegistry:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.close()

    # --- Image Resolution ---

    async def _pull_image(self, image: str) -> str:
        """Ensure image is available to the executor, returning a runtime-specific image ID."""
        full_ref = self._registry_config.normalize_image_ref(image)
        return await self._executor.ensure_image(full_ref)

    async def _resolve_image_ref(self, agent_type: AgentType, ref: str) -> str:
        """Resolve image reference to digest via registry proxy.

        Uses self._db_config credentials for HTTP basic auth to the proxy.

        Returns:
            Digest (sha256:...) - either the provided digest or resolved from tag

        Raises:
            ImageResolutionError: If tag doesn't exist or proxy returns error
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
            raise ImageResolutionError(f"Failed to resolve tag {repository}:{ref}: {e}")

        if resp.status_code == 404:
            raise ImageResolutionError(f"Image not found: {repository}:{ref}")

        if resp.status_code != 200:
            raise ImageResolutionError(f"Proxy returned error {resp.status_code} for {repository}:{ref}: {resp.text}")

        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            raise ImageResolutionError(f"Proxy didn't return Docker-Content-Digest header for {repository}:{ref}")

        logger.info(f"Resolved {repository}:{ref} → {digest}")
        return str(digest)

    async def resolve_image(self, agent_type: AgentType, ref: str) -> ResolvedImage:
        """Resolve image tag/digest to a ResolvedImage.

        Raises:
            ImageResolutionError: If the image cannot be resolved.
        """
        digest = await self._resolve_image_ref(agent_type, ref)
        oci_ref = self._registry_config.build_oci_reference(agent_type, digest)
        return ResolvedImage(digest=digest, oci_ref=oci_ref)

    async def _create_container(self, agent_run_id: UUID, *, image: str) -> ContainerHandle:
        """Ensure image, create DB role, create and start an agent container."""
        image_id = await self._pull_image(image)
        logger.info("Using image %s from %s", image_id[:19], image)

        creds = await ensure_agent_role(self._db_config, agent_run_id)
        logger.info("Agent role ready: %s", creds.username)

        name = f"agent-{short_uuid(agent_run_id)}"

        # OpenAI SDK sends api_key as Bearer token. The backend auth middleware
        # accepts Bearer tokens containing base64-encoded username:password.
        api_key = self._db_config.with_user(creds.username, creds.password).basic_auth_token
        env = {
            **self._agent_base_env,
            "PGUSER": creds.username,
            "PGPASSWORD": creds.password,
            "PROPS_BACKEND_URL": self._backend_url,
            "OPENAI_BASE_URL": f"{self._backend_url}/v1",
            "OPENAI_API_KEY": api_key,
        }

        return await self._executor.run_container(
            name=name,
            image_id=image_id,
            env=env,
            labels={"adgn.project": "props", "adgn.agent_run_id": str(agent_run_id)},
        )

    async def _run_agent(self, agent_run_id: UUID, *, image: str, timeout_seconds: int | None = None) -> AgentRunStatus:
        """Run agent container, update DB status, return final status.

        Full lifecycle: create container → wait for exit → capture logs → update status.
        timeout_seconds=None means no timeout (for long-running agents).
        """
        handle = await self._create_container(agent_run_id, image=image)
        try:
            result: ContainerResult = await handle.wait(timeout_seconds=timeout_seconds)

            # Log container output
            exit = result.exit
            if isinstance(exit, Exited) and exit.exit_code == 0:
                logger.info("Container %s stdout:\n%s", handle.name, result.stdout)
                if result.stderr:
                    logger.info("Container %s stderr:\n%s", handle.name, result.stderr)
            else:
                logger.error("Container %s stdout:\n%s", handle.name, result.stdout)
                logger.error("Container %s stderr:\n%s", handle.name, result.stderr)

        finally:
            try:
                await handle.kill_and_delete()
                logger.info("Deleted container")
            except Exception as e:
                logger.warning("Failed to delete container: %s", e)

        # Determine and persist status
        exit = result.exit
        if isinstance(exit, TimedOut):
            status = AgentRunStatus.TIMED_OUT
            container_exit_code = None
        else:
            status = AgentRunStatus.EXITED
            container_exit_code = exit.exit_code

        with self._db.session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            if found_run.status == AgentRunStatus.IN_PROGRESS:
                found_run.status = status
                found_run.container_exit_code = container_exit_code
                session.commit()
                logger.info(f"Updated {agent_run_id} status to {status}")
        return status

    # --- Execution Methods ---

    def _validate_spawn_budget(self, parent_run_id: UUID, child_budget_usd: float) -> None:
        """Validate that spawning a child with the given budget doesn't exceed parent's remaining budget.

        Uses the agent_run_budget_status view which recursively sums descendant costs.
        """
        # TODO: This only checks the immediate parent's remaining budget. It should also:
        # 1. Subtract the budgets of still-running child agents from remaining, not just
        #    their actual spend so far — a running child could spend up to its full budget.
        # 2. Walk up the parent chain and enforce budget constraints at every ancestor level,
        #    not just the immediate parent.
        with self._db.session() as session:
            status = session.get(AgentRunBudgetStatus, parent_run_id)
            if status is None:
                raise BudgetExceededError(f"Parent run {parent_run_id} not found")

            if child_budget_usd > status.remaining_usd:
                raise BudgetExceededError(
                    f"Cannot spawn child with ${child_budget_usd:.2f} budget: "
                    f"parent has ${status.remaining_usd:.2f} remaining "
                    f"(${status.tree_spent_usd:.2f} spent of ${status.budget_usd:.2f})"
                )

    def _create_run(
        self,
        *,
        image: ResolvedImage,
        model: str,
        type_config: TypeConfig,
        budget_usd: float,
        parent_run_id: UUID | None = None,
        verify_snapshot: SnapshotSlug | None = None,
    ) -> UUID:
        """Create an agent_run DB record. Returns agent_run_id."""
        agent_run_id = uuid4()
        with self._db.session() as session:
            if verify_snapshot is not None:
                session.query(Snapshot).filter_by(slug=verify_snapshot).one()
            session.add(
                AgentRun(
                    agent_run_id=agent_run_id,
                    image_digest=image.digest,
                    parent_agent_run_id=parent_run_id,
                    model=model,
                    type_config=type_config,
                    status=AgentRunStatus.IN_PROGRESS,
                    budget_usd=budget_usd,
                )
            )
            session.commit()
        return agent_run_id

    async def run_critic(
        self,
        *,
        image: ResolvedImage,
        example: ExampleSpec,
        model: str,
        timeout_seconds: int,
        parent_run_id: UUID | None,
        budget_usd: float,
    ) -> UUID:
        """Run a critic agent. Returns agent run ID (query DB for status)."""
        if parent_run_id is not None:
            self._validate_spawn_budget(parent_run_id, budget_usd)

        agent_run_id = self._create_run(
            image=image,
            model=model,
            type_config=CriticTypeConfig(example=example),
            budget_usd=budget_usd,
            parent_run_id=parent_run_id,
            verify_snapshot=example.snapshot_slug,
        )
        await self._run_agent(agent_run_id, image=image.oci_ref, timeout_seconds=timeout_seconds)
        return agent_run_id

    async def run_critic_dev_optimize(
        self,
        *,
        image: ResolvedImage,
        budget: float,
        optimizer_model: str,
        critic_model: str,
        target_metric: TargetMetric,
        timeout_seconds: int,
    ) -> UUID:
        """Run a critic-dev optimizer agent. Returns agent run ID (query DB for status)."""
        agent_run_id = self._create_run(
            image=image,
            model=optimizer_model,
            type_config=CriticDevOptimizeTypeConfig(
                target_metric=target_metric, optimizer_model=optimizer_model, critic_model=critic_model
            ),
            budget_usd=budget,
        )
        await self._run_agent(agent_run_id, image=image.oci_ref, timeout_seconds=timeout_seconds)
        return agent_run_id

    async def run_critic_dev_improve(
        self,
        *,
        image: ResolvedImage,
        examples: list[ExampleSpec],
        baseline_image_digests: list[str],
        budget_usd: float,
        improvement_model: str,
        critic_model: str,
        timeout_seconds: int,
        output_dir: Path | None = None,
    ) -> UUID:
        """Run a critic-dev improve agent that creates definitions to beat baselines on the allowed examples.

        Returns agent run ID. Query DB for final status.
        """
        if not examples:
            raise ValueError("examples must not be empty")

        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="improve_agent_"))
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve baseline refs to digests (tags → sha256:...)
        resolved_baselines = [await self._resolve_image_ref(AgentType.CRITIC, ref) for ref in baseline_image_digests]

        agent_run_id = self._create_run(
            image=image,
            model=improvement_model,
            type_config=CriticDevImproveTypeConfig(
                baseline_image_digests=resolved_baselines,
                allowed_examples=examples,
                improvement_model=improvement_model,
                critic_model=critic_model,
            ),
            budget_usd=budget_usd,
        )
        await self._run_agent(agent_run_id, image=image.oci_ref, timeout_seconds=timeout_seconds)
        return agent_run_id

    # --- State Tracking ---

    def get(self, run_id: UUID) -> AgentRunView | None:
        with self._db.session() as session:
            db_run = session.get(AgentRun, run_id)
            if not db_run:
                return None
            return AgentRunView.from_orm(db_run)

    def list_recent(self, limit: int = 50) -> list[AgentRunView]:
        with self._db.session() as session:
            runs = session.query(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit).all()
            return [AgentRunView.from_orm(r) for r in runs]

    async def run_snapshot_grader(self, *, image: ResolvedImage, snapshot_slug: SnapshotSlug, model: str) -> UUID:
        """Run a snapshot grader. Blocks until it exits."""
        agent_run_id = self._create_run(
            image=image,
            model=model,
            type_config=GraderTypeConfig(snapshot_slug=snapshot_slug),
            budget_usd=10_000.0,
            verify_snapshot=snapshot_slug,
        )
        await self._run_agent(agent_run_id, image=image.oci_ref)
        return agent_run_id

    async def start_snapshot_grader(
        self, *, image: ResolvedImage, snapshot_slug: SnapshotSlug, model: str
    ) -> ContainerHandle:
        """Start a snapshot grader, returning a handle to kill it."""
        agent_run_id = self._create_run(
            image=image,
            model=model,
            type_config=GraderTypeConfig(snapshot_slug=snapshot_slug),
            budget_usd=10_000.0,
            verify_snapshot=snapshot_slug,
        )
        return await self._create_container(agent_run_id, image=image.oci_ref)
