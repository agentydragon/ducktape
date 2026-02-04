"""E2E test fixtures for container-based agent tests.

These fixtures set up the full e2e testing stack:
- Raw Docker registry (testcontainers, from e2e_infra)
- Real backend app (FastAPI - same as production)
- Fake OpenAI server (returns scripted responses)
- AgentRegistry (orchestrates containers)

The container communicates through the real backend to the fake OpenAI server,
exercising the full production code path including auth, request logging, and
registry proxy (which records agent_definitions on push).

Environment variables:
    PROPS_E2E_HOST_HOSTNAME: Hostname for containers to reach host services.
        - Default: "host.docker.internal" (Docker bridge networking)
        - Set to "127.0.0.1" for host networking (e.g., CI with --network=host)

Usage:
    @pytest.mark.requires_docker
    @pytest.mark.requires_postgres
    async def test_critic_completes(e2e_stack, all_files_scope, critic_image):
        mock = make_critic_mock()
        async with e2e_stack(mock, images=[critic_image]) as stack:
            run_id = await stack.registry.run_critic(
                image_ref=BUILTIN_TAG,
                example=all_files_scope,
                model=stack.model,
                timeout_seconds=60,
                parent_run_id=None,
                budget_usd=None,
            )
            # Assert on database state

    # Multi-model usage (pass dict of mocks):
    async def test_orchestration(e2e_stack, ...):
        mocks = {"optimizer-model": opt_mock, "critic-model": crit_mock}
        async with e2e_stack(mocks, images=[...]) as stack:
            ...
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiodocker
import pytest
import pytest_asyncio
import uvicorn

from net_util.net import pick_free_port
from openai_utils.model import OpenAIModelProto
from props.backend.app import BackendDeps, create_app
from props.config import PropsConfig
from props.core.docker_env import PROPS_NETWORK_NAME
from props.core.oci_utils import BUILTIN_TAG, RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.orchestration.agent_registry import AgentRegistry
from props.testing.fake_openai_server import FakeOpenAIServer
from test_util.oci import BazelImage, crane_push

logger = logging.getLogger(__name__)

# Default model name for tests
TEST_MODEL = "test-model"

# Hostname for containers to reach host services.
# - Default "host.docker.internal" works with Docker bridge networking
# - Set to "127.0.0.1" or "localhost" when using host networking (e.g., CI environments)
E2E_HOST_HOSTNAME = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")

# Host gateway for container access to host services (only needed for bridge networking)
HOST_GATEWAY = {E2E_HOST_HOSTNAME: "host-gateway"} if E2E_HOST_HOSTNAME == "host.docker.internal" else {}


@asynccontextmanager
async def ensure_agent_network(docker_client: aiodocker.Docker) -> AsyncIterator[None]:
    """Ensure the agent Docker network exists, creating it if needed.

    For "host" networking (CI/Firecracker), this is a no-op.
    For bridge networking, creates the network and removes it on cleanup.
    """
    if PROPS_NETWORK_NAME == "host":
        yield
        return

    # Check if network already exists
    existing = await docker_client.networks.list(filters={"name": [PROPS_NETWORK_NAME]})
    # Filter for exact name match (Docker list uses substring matching)
    already_exists = any(n["Name"] == PROPS_NETWORK_NAME for n in existing)

    if already_exists:
        logger.info("Docker network %s already exists", PROPS_NETWORK_NAME)
        yield
        return

    logger.info("Creating Docker network %s", PROPS_NETWORK_NAME)
    network = await docker_client.networks.create({"Name": PROPS_NETWORK_NAME, "Driver": "bridge"})
    try:
        yield
    finally:
        try:
            logger.info("Removing Docker network %s", PROPS_NETWORK_NAME)
            await network.delete()
        except RuntimeError:
            # Docker session may already be closed during test teardown
            logger.debug("Could not remove network %s (session closed)", PROPS_NETWORK_NAME)


@dataclass
class E2EStack:
    """Running e2e test stack with all services."""

    registry: AgentRegistry
    model: str


def _build_agent_base_env(db_config: DatabaseConfig, backend_port: int) -> dict[str, str]:
    """Build agent_base_env for e2e tests.

    Agents reach postgres and backend through E2E_HOST_HOSTNAME at the mapped ports.
    """
    return {
        "PGHOST": E2E_HOST_HOSTNAME,
        "PGPORT": str(db_config.port),
        "PGDATABASE": db_config.database,
        "PROPS_BACKEND_URL": f"http://{E2E_HOST_HOSTNAME}:{backend_port}",
    }


@asynccontextmanager
async def run_backend(deps: BackendDeps, port: int, host: str = "0.0.0.0") -> AsyncIterator[None]:
    """Start the real backend app with uvicorn."""
    app = create_app(deps=deps)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"Backend server failed to start: {exc}")

    logger.info("Backend started on port %d", port)

    try:
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("Backend stopped")


def _set_backend_env(monkeypatch: pytest.MonkeyPatch, db_config: DatabaseConfig, e2e_registry_url: str) -> None:
    """Set env vars needed by the backend's lifespan."""
    monkeypatch.setenv("PROPS_REGISTRY_UPSTREAM_URL", e2e_registry_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for key, value in db_config.to_env_dict().items():
        monkeypatch.setenv(key, value)


@asynccontextmanager
async def _make_stack(
    fake_openai: FakeOpenAIServer,
    db: Database,
    async_docker_client: aiodocker.Docker,
    e2e_registry_url: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str,
    images: Sequence[BazelImage] = (),
) -> AsyncIterator[E2EStack]:
    """Shared stack setup: start fake OpenAI, backend, registry, push images."""
    await fake_openai.start()

    try:
        _set_backend_env(monkeypatch, db.config, e2e_registry_url)
        monkeypatch.setenv("OPENAI_UPSTREAM_URL", fake_openai.url)

        backend_port = pick_free_port()
        registry_proxy_config = RegistryProxyConfig(host="localhost", port=backend_port)
        agent_base_env = _build_agent_base_env(db.config, backend_port)

        deps = BackendDeps(config=PropsConfig(agent_env=agent_base_env), registry_proxy_config=registry_proxy_config)

        async with ensure_agent_network(async_docker_client), run_backend(deps, port=backend_port):
            registry = AgentRegistry(
                docker_client=async_docker_client,
                db=db,
                db_config=db.config,
                agent_base_env=agent_base_env,
                registry_config=registry_proxy_config,
                extra_hosts=HOST_GATEWAY,
            )

            try:
                proxy_url = f"localhost:{backend_port}"
                for image in images:
                    crane_push(image, proxy_url, BUILTIN_TAG)
                yield E2EStack(registry=registry, model=model)
            finally:
                await registry.close()

    finally:
        await fake_openai.stop()


@pytest_asyncio.fixture
async def e2e_stack(
    synced_db: Database, async_docker_client: aiodocker.Docker, e2e_registry_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Fixture factory for creating e2e test stacks.

    Accepts a single mock (all requests go to it) or a dict of mocks
    (routes by model name in the request body).

    Usage:
        # Single mock
        async def test_something(e2e_stack, critic_image):
            mock = make_my_mock()
            async with e2e_stack(mock, images=[critic_image]) as stack:
                run_id = await stack.registry.run_critic(...)

        # Multi-model
        async def test_orchestration(e2e_stack, ...):
            mocks = {"optimizer-model": opt_mock, "critic-model": crit_mock}
            async with e2e_stack(mocks, images=[...]) as stack:
                ...
    """

    @asynccontextmanager
    async def _factory(
        mock: OpenAIModelProto | Mapping[str, OpenAIModelProto],
        model: str = TEST_MODEL,
        *,
        images: Sequence[BazelImage] = (),
    ) -> AsyncIterator[E2EStack]:
        if isinstance(mock, Mapping):
            fake_openai = FakeOpenAIServer(dict(mock), host="0.0.0.0", port=0)
        else:
            fake_openai = FakeOpenAIServer(mock, host="0.0.0.0", port=0)
        async with _make_stack(
            fake_openai, synced_db, async_docker_client, e2e_registry_url, monkeypatch, model=model, images=images
        ) as stack:
            yield stack

    yield _factory
