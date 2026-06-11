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

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import AsyncIterator
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


def _slug_to_container_segment(slug: str) -> str:
    """Normalize a snapshot slug for use in a Docker container name.

    Replaces '/' with '-' and truncates to 28 characters to keep names manageable.
    Example: 'ducktape/2025-09-03-00' → 'ducktape-2025-09-03-00'
    """
    return slug.replace("/", "-")[:28]


# --- Agent Run Handle ---


class AgentRunHandle:
    """Handle to a running agent managed by the registry.

    Awaiting the handle blocks until the agent run completes and returns the
    final AgentRunStatus. The registry owns the background task that captures
    container logs and updates DB status on exit.

    Use as an async context manager to ensure kill_and_delete() is always called:

        async with await registry.start_snapshot_grader(...) as handle:
            await asyncio.wait_for(done_event.wait(), timeout=90)
        # kill_and_delete() called automatically on exit

    Call kill_and_delete() directly to stop the agent early without a context manager.
    """

    def __init__(self, task: asyncio.Task[AgentRunStatus], name: str, agent_run_id: UUID) -> None:
        self._task = task
        self.name = name
        self.agent_run_id = agent_run_id

    def __await__(self):
        return self._task.__await__()

    async def __aenter__(self) -> AgentRunHandle:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.kill_and_delete()

    async def kill_and_delete(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


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
        model_parallelism_limits: dict[str, int] | None = None,
    ) -> None:
        self._executor = executor
        self._db = db
        self._db_config = db_config
        self._backend_url = backend_url
        self._agent_base_env = agent_base_env
        self._registry_config = registry_config
        # Track running background critic tasks by agent_run_id to prevent GC and allow lookup
        self._running_critics: dict[UUID, asyncio.Task[None]] = {}
        self._model_semaphores: dict[str, asyncio.Semaphore] = {
            model: asyncio.Semaphore(limit) for model, limit in (model_parallelism_limits or {}).items()
        }

    async def close(self) -> None:
        await self._executor.close()

    async def __aenter__(self) -> AgentRegistry:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.close()

    # --- Model Parallelism ---

    @contextlib.asynccontextmanager
    async def _model_slot(self, model: str) -> AsyncIterator[None]:
        """Acquire a parallelism slot for the given model, if a limit is configured.

        Blocks until a slot is available. No-op when no limit is configured for the model.
        Held for the entire duration of the agent container run to bound concurrent usage.
        """
        sem = self._model_semaphores.get(model)
        if sem is None:
            yield
        else:
            if sem.locked():
                logger.info("Model %s at capacity, queuing agent", model)
            async with sem:
                yield

    # --- Image Resolution ---

    async def _pull_image(self, oci_ref: str) -> str:
        """Ensure image is available to the executor, returning a runtime-specific image ID.

        oci_ref must be a fully-qualified OCI reference (authority/repository@digest),
        as returned by build_oci_reference().
        """
        return await self._executor.ensure_image(oci_ref)

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

    async def _create_container(self, agent_run_id: UUID, *, image: str, name: str) -> ContainerHandle:
        """Ensure image, create DB role, create and start an agent container."""
        image_id = await self._pull_image(image)
        logger.info("Using image %s from %s", image_id[:19], image)

        creds = await ensure_agent_role(self._db_config, agent_run_id)
        logger.info("Agent role ready: %s", creds.username)

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

    async def _collect_run(
        self, agent_run_id: UUID, handle: ContainerHandle, timeout_seconds: int | None
    ) -> AgentRunStatus:
        """Wait for container exit, capture logs, update DB status, return final status."""
        try:
            result: ContainerResult = await handle.wait(timeout_seconds=timeout_seconds)
        finally:
            try:
                await handle.kill_and_delete()
                logger.info("Deleted container")
            except Exception as e:
                logger.warning("Failed to delete container: %s", e)

        exit = result.exit
        log = logger.info if isinstance(exit, Exited) and exit.exit_code == 0 else logger.error
        log("Container %s stdout:\n%s", handle.name, result.stdout)
        if result.stderr:
            log("Container %s stderr:\n%s", handle.name, result.stderr)

        if isinstance(exit, TimedOut):
            status = AgentRunStatus.TIMED_OUT
            container_exit_code = None
        else:
            status = AgentRunStatus.EXITED
            container_exit_code = exit.exit_code

        with self._db.session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            if found_run.status != AgentRunStatus.IN_PROGRESS:
                raise RuntimeError(f"Agent run {agent_run_id} expected IN_PROGRESS but found {found_run.status}")
            found_run.status = status
            found_run.container_exit_code = container_exit_code
            session.commit()
            logger.info(f"Updated {agent_run_id} status to {status}")
        return status

    async def _start_agent(
        self, agent_run_id: UUID, *, image: str, timeout_seconds: int | None = None, name: str
    ) -> AgentRunHandle:
        """Create and start agent container, returning a handle that manages its lifecycle.

        The registry owns a background task that waits for exit, captures logs,
        and updates DB status. Await the returned handle to block until completion;
        call kill_and_delete() to stop early.
        """
        handle = await self._create_container(agent_run_id, image=image, name=name)
        task = asyncio.create_task(self._collect_run(agent_run_id, handle, timeout_seconds))
        return AgentRunHandle(task, handle.name, agent_run_id)

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
        agent_run_id: UUID,
        *,
        image: ResolvedImage,
        model: str,
        type_config: TypeConfig,
        budget_usd: float,
        parent_run_id: UUID | None = None,
        verify_snapshot: SnapshotSlug | None = None,
    ) -> None:
        """Create an agent_run DB record."""
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

    async def _create_and_start(
        self,
        agent_run_id: UUID,
        *,
        image: ResolvedImage,
        model: str,
        type_config: TypeConfig,
        budget_usd: float,
        parent_run_id: UUID | None = None,
        verify_snapshot: SnapshotSlug | None = None,
        container_name: str,
        timeout_seconds: int | None = None,
    ) -> AgentRunHandle:
        """Create DB record and start container, returning a handle."""
        self._create_run(
            agent_run_id,
            image=image,
            model=model,
            type_config=type_config,
            budget_usd=budget_usd,
            parent_run_id=parent_run_id,
            verify_snapshot=verify_snapshot,
        )
        return await self._start_agent(agent_run_id, image=image.oci_ref, name=container_name)

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
        """Run a critic agent. Blocks until the container exits. Returns agent run ID."""
        if parent_run_id is not None:
            self._validate_spawn_budget(parent_run_id, budget_usd)

        agent_run_id = uuid4()
        slug_seg = _slug_to_container_segment(example.snapshot_slug)
        container_name = f"critic-{slug_seg}-{str(agent_run_id)[:8]}"
        async with self._model_slot(model):
            handle = await self._create_and_start(
                agent_run_id,
                image=image,
                model=model,
                type_config=CriticTypeConfig(example=example),
                budget_usd=budget_usd,
                parent_run_id=parent_run_id,
                verify_snapshot=example.snapshot_slug,
                container_name=container_name,
                timeout_seconds=timeout_seconds,
            )
            await handle
        return agent_run_id

    async def start_critic(
        self,
        *,
        image: ResolvedImage,
        example: ExampleSpec,
        model: str,
        timeout_seconds: int,
        parent_run_id: UUID | None,
        budget_usd: float,
    ) -> UUID:
        """Start a critic agent in the background. Returns agent_run_id immediately.

        The container runs asynchronously. Poll /api/runs/{agent_run_id} or query the
        DB directly for status updates. Use wait_until_graded() after the run exits.
        """
        if parent_run_id is not None:
            self._validate_spawn_budget(parent_run_id, budget_usd)

        agent_run_id = uuid4()
        slug_seg = _slug_to_container_segment(example.snapshot_slug)
        container_name = f"critic-{slug_seg}-{str(agent_run_id)[:8]}"

        self._create_run(
            agent_run_id,
            image=image,
            model=model,
            type_config=CriticTypeConfig(example=example),
            budget_usd=budget_usd,
            parent_run_id=parent_run_id,
            verify_snapshot=example.snapshot_slug,
        )

        async def _run() -> None:
            try:
                async with self._model_slot(model):
                    handle = await self._start_agent(
                        agent_run_id, image=image.oci_ref, timeout_seconds=timeout_seconds, name=container_name
                    )
                    await handle
            except Exception:
                logger.exception("Unhandled error in background critic run %s", agent_run_id)

        task = asyncio.create_task(_run(), name=f"critic-{agent_run_id}")
        self._running_critics[agent_run_id] = task
        task.add_done_callback(lambda _: self._running_critics.pop(agent_run_id, None))
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
        agent_run_id = uuid4()
        container_name = f"critic-dev-opt-{str(agent_run_id)[:8]}"
        async with self._model_slot(optimizer_model):
            handle = await self._create_and_start(
                agent_run_id,
                image=image,
                model=optimizer_model,
                type_config=CriticDevOptimizeTypeConfig(
                    target_metric=target_metric, optimizer_model=optimizer_model, critic_model=critic_model
                ),
                budget_usd=budget,
                container_name=container_name,
                timeout_seconds=timeout_seconds,
            )
            await handle
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

        agent_run_id = uuid4()
        container_name = f"critic-dev-imp-{str(agent_run_id)[:8]}"
        async with self._model_slot(improvement_model):
            handle = await self._create_and_start(
                agent_run_id,
                image=image,
                model=improvement_model,
                type_config=CriticDevImproveTypeConfig(
                    baseline_image_digests=resolved_baselines,
                    allowed_examples=examples,
                    improvement_model=improvement_model,
                    critic_model=critic_model,
                ),
                budget_usd=budget_usd,
                container_name=container_name,
                timeout_seconds=timeout_seconds,
            )
            await handle
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

    async def start_snapshot_grader(
        self, *, image: ResolvedImage, snapshot_slug: SnapshotSlug, model: str
    ) -> AgentRunHandle:
        """Start a snapshot grader. Returns a handle that owns the run lifecycle.

        Await the handle to block until the grader exits. Call kill_and_delete()
        to stop it early. The registry's background task captures logs and updates
        DB status on exit regardless of how the grader is stopped.
        """
        agent_run_id = uuid4()
        slug_seg = _slug_to_container_segment(snapshot_slug)
        container_name = f"grader-{slug_seg}-{str(agent_run_id)[:8]}"
        return await self._create_and_start(
            agent_run_id,
            image=image,
            model=model,
            type_config=GraderTypeConfig(snapshot_slug=snapshot_slug),
            budget_usd=10_000.0,
            verify_snapshot=snapshot_slug,
            container_name=container_name,
        )
