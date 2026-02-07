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
        backend_url="http://props-backend:8000",
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
            budget_usd=5.0,
        )
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

import aiodocker
import httpx

from props.agents.critic_dev.shared import TargetMetric
from props.core.agent_types import (
    AgentType,
    CriticDevImproveTypeConfig,
    CriticDevOptimizeTypeConfig,
    CriticTypeConfig,
    GraderTypeConfig,
)
from props.core.display import short_uuid
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import BUILTIN_TAG, RegistryProxyConfig, is_digest
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunBudgetStatus, AgentRunStatus, Snapshot
from props.orchestration.agent_credentials import ensure_agent_role
from props.orchestration.docker_env import PROPS_NETWORK_NAME

logger = logging.getLogger(__name__)


# --- Exceptions ---


class BudgetExceededError(Exception):
    """Raised when a child agent's requested budget exceeds parent's remaining budget."""


class ImageResolutionError(Exception):
    """Raised when an image reference cannot be resolved or pulled."""


# --- Container Result ---


@dataclass(frozen=True)
class ContainerResult:
    """Result of running an agent container. exit_code is None if the container timed out."""

    stdout: str
    stderr: str
    exit_code: int | None

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


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
        backend_url: str,
        agent_base_env: dict[str, str],
        registry_config: RegistryProxyConfig,
        extra_hosts: dict[str, str] | None = None,
    ) -> None:
        self._docker_client = docker_client
        self._db = db
        self._db_config = db_config
        self._backend_url = backend_url
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

    async def _pull_image(self, image: str) -> str:
        """Pull an OCI image to the local Docker daemon, returning its image ID."""
        full_ref = self._registry_config.normalize_image_ref(image)
        try:
            info = await self._docker_client.images.inspect(full_ref)
            image_id: str = info["Id"]
            logger.info("Using cached image %s for %s", image_id[:19], full_ref)
            return image_id
        except Exception:
            pass  # Not found locally, pull
        logger.info("Pulling image %s", full_ref)
        auth = {"username": self._db_config.user, "password": self._db_config.password}
        await self._docker_client.pull(full_ref, auth=auth)
        info = await self._docker_client.images.inspect(full_ref)
        image_id = info["Id"]
        logger.info("Pulled image %s for %s", image_id[:19], full_ref)
        return image_id

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

    async def _resolve_image(self, agent_type: AgentType, ref: str) -> tuple[str, str]:
        """Resolve image ref to (digest, full OCI reference)."""
        digest = await self._resolve_image_ref(agent_type, ref)
        oci_ref = self._registry_config.build_oci_reference(agent_type, digest)
        return digest, oci_ref

    async def _run_agent(self, agent_run_id: UUID, *, image: str, timeout_seconds: int | None = None) -> AgentRunStatus:
        """Run agent container, update DB status, return final status.

        Full lifecycle: resolve image → create DB role → start container →
        wait for exit → capture logs → update agent_runs status.
        timeout_seconds=None means no timeout (for daemons).
        """
        image_id = await self._pull_image(image)
        logger.info("Using image %s from %s", image_id[:19], image)

        creds = await ensure_agent_role(self._db_config, agent_run_id)
        logger.info("Agent role ready: %s", creds.username)

        container = None
        try:
            name = f"agent-{short_uuid(agent_run_id)}"

            backend_url = self._backend_url
            # OpenAI SDK sends api_key as Bearer token. The backend auth middleware
            # accepts Bearer tokens containing base64-encoded username:password.
            api_key = self._db_config.with_user(creds.username, creds.password).basic_auth_token
            env = {
                **self._agent_base_env,
                "PGUSER": creds.username,
                "PGPASSWORD": creds.password,
                "PROPS_BACKEND_URL": backend_url,
                "OPENAI_BASE_URL": f"{backend_url}/v1",
                "OPENAI_API_KEY": api_key,
            }

            host_config: dict[str, object] = {"NetworkMode": PROPS_NETWORK_NAME, "AutoRemove": False}
            if self._extra_hosts:
                host_config["ExtraHosts"] = [f"{host}:{ip}" for host, ip in self._extra_hosts.items()]

            container_config = {
                "Image": image_id,
                "Env": [f"{k}={v}" for k, v in env.items()],
                "HostConfig": host_config,
                "Labels": {"adgn.project": "props", "adgn.agent_run_id": str(agent_run_id)},
            }

            container = await self._docker_client.containers.create(
                container_config,  # type: ignore[arg-type]  # aiodocker JSONObject
                name=name,
            )
            logger.info("Created container %s", name)

            await container.start()
            logger.info("Started container %s", name)

            # Wait for container to exit (with optional timeout)
            result: ContainerResult
            try:
                if timeout_seconds is not None:
                    exit_info = await asyncio.wait_for(container.wait(), timeout=timeout_seconds)
                else:
                    exit_info = await container.wait()

                stdout = "".join(await container.log(stdout=True, stderr=False))
                stderr = "".join(await container.log(stdout=False, stderr=True))
                exit_code = exit_info.get("StatusCode", 1)
                result = ContainerResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
                logger.info("Container %s exited with code %d", name, exit_code)
            except TimeoutError:
                logger.error("Container %s timed out after %d seconds", name, timeout_seconds)
                try:
                    await container.kill()
                except Exception as e:
                    logger.warning("Failed to kill timed-out container: %s", e)
                stdout = "".join(await container.log(stdout=True, stderr=False))
                stderr = "".join(await container.log(stdout=False, stderr=True))
                result = ContainerResult(stdout=stdout, stderr=stderr, exit_code=None)

            # Log container output
            if result.exit_code == 0:
                logger.info("Container %s stdout:\n%s", name, result.stdout)
                if result.stderr:
                    logger.info("Container %s stderr:\n%s", name, result.stderr)
            else:
                logger.error("Container %s stdout:\n%s", name, result.stdout)
                logger.error("Container %s stderr:\n%s", name, result.stderr)

        finally:
            if container is not None:
                try:
                    await container.delete(force=True)
                    logger.info("Deleted container")
                except Exception as e:
                    logger.warning("Failed to delete container: %s", e)

        # Determine and persist status
        status = AgentRunStatus.TIMED_OUT if result.timed_out else AgentRunStatus.EXITED

        with self._db.session() as session:
            found_run = session.get(AgentRun, agent_run_id)
            assert found_run is not None, f"Agent run {agent_run_id} not found in database"
            if found_run.status == AgentRunStatus.IN_PROGRESS:
                found_run.status = status
                found_run.container_exit_code = result.exit_code
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

    async def run_critic(
        self,
        *,
        image_ref: str,
        example: ExampleSpec,
        model: str,
        timeout_seconds: int,
        parent_run_id: UUID | None,
        budget_usd: float,
    ) -> UUID:
        """Run a critic agent. Returns agent run ID (query DB for status)."""
        if parent_run_id is not None:
            self._validate_spawn_budget(parent_run_id, budget_usd)

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
                budget_usd=budget_usd,
            )
            session.add(agent_run)
            session.commit()

        await self._run_agent(agent_run_id, image=image, timeout_seconds=timeout_seconds)
        return agent_run_id

    async def run_critic_dev_optimize(
        self,
        *,
        budget: float,
        optimizer_model: str,
        critic_model: str,
        target_metric: TargetMetric,
        timeout_seconds: int,
    ) -> UUID:
        """Run a critic-dev optimizer agent. Returns agent run ID (query DB for status)."""
        agent_run_id = uuid4()
        image_digest, image = await self._resolve_image(AgentType.CRITIC_DEV_OPTIMIZE, BUILTIN_TAG)

        with self._db.session() as session:
            type_config = CriticDevOptimizeTypeConfig(
                target_metric=target_metric,
                optimizer_model=optimizer_model,
                critic_model=critic_model,
                budget_limit=budget,
            )

            agent_run = AgentRun(
                agent_run_id=agent_run_id,
                image_digest=image_digest,
                model=optimizer_model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=budget,
            )
            session.add(agent_run)
            session.commit()

        await self._run_agent(agent_run_id, image=image, timeout_seconds=timeout_seconds)
        return agent_run_id

    async def run_critic_dev_improve(
        self,
        *,
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

        run_id = uuid4()
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix=f"improve_agent_{str(run_id)[:8]}_"))

        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        image_digest, image = await self._resolve_image(AgentType.CRITIC_DEV_IMPROVE, BUILTIN_TAG)

        # Resolve baseline refs to digests (tags → sha256:...)
        resolved_baselines = [await self._resolve_image_ref(AgentType.CRITIC, ref) for ref in baseline_image_digests]

        type_config = CriticDevImproveTypeConfig(
            baseline_image_digests=resolved_baselines,
            allowed_examples=examples,
            improvement_model=improvement_model,
            critic_model=critic_model,
        )

        with self._db.session() as session:
            agent_run = AgentRun(
                agent_run_id=run_id,
                image_digest=image_digest,
                model=improvement_model,
                type_config=type_config,
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=budget_usd,
            )
            session.add(agent_run)
            session.commit()

        await self._run_agent(run_id, image=image, timeout_seconds=timeout_seconds)
        return run_id

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
                budget_usd=10_000.0,
            )
            session.add(agent_run)
            session.commit()

        await self._run_agent(agent_run_id, image=image)
        return agent_run_id
