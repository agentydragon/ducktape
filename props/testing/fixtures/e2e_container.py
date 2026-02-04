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
    async def test_critic_completes(e2e_stack, all_files_scope):
        mock = make_critic_mock()
        async with e2e_stack(mock) as stack:
            run_id = await stack.registry.run_critic(
                image_ref="builtin",
                example=all_files_scope,
                model=stack.model,
                timeout_seconds=60,
                parent_run_id=None,
                budget_usd=None,
            )
            # Assert on database state
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
from props.testing.fake_openai_server import FakeOpenAIServer, MultiModelFakeOpenAI
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
    _proxy_port: int

    def push_image(self, image: BazelImage, tag: str = BUILTIN_TAG) -> None:
        """Push an OCI image layout through the backend proxy (records agent_definition)."""
        proxy_url = f"localhost:{self._proxy_port}"
        crane_push(image, proxy_url, tag)

    def push_images(self, *images: BazelImage, tag: str = BUILTIN_TAG) -> None:
        """Push multiple images through the backend proxy."""
        for image in images:
            self.push_image(image, tag=tag)


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
async def run_backend(deps: BackendDeps, port: int, host: str = "0.0.0.0") -> AsyncIterator[int]:
    """Start the real backend app with uvicorn, yield the port."""
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
        yield port
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


# Type alias for the fixture factory
E2EStackFactory = Callable[[OpenAIModelProto], AbstractAsyncContextManager[E2EStack]]


@pytest_asyncio.fixture
async def e2e_stack(
    synced_db: Database, async_docker_client: aiodocker.Docker, e2e_registry_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[E2EStackFactory]:
    """Fixture factory for creating e2e test stacks.

    Sets up env vars via monkeypatch and yields a factory that creates E2EStack instances.
    RegistryProxyConfig is constructed directly from the backend's port (determined upfront).

    Usage:
        async def test_something(e2e_stack, all_files_scope, critic_image):
            mock = make_my_mock()
            async with e2e_stack(mock, images=[critic_image]) as stack:
                run_id = await stack.registry.run_critic(...)
    """
    _set_backend_env(monkeypatch, synced_db.config, e2e_registry_url)

    @asynccontextmanager
    async def _factory(
        mock: OpenAIModelProto, model: str = TEST_MODEL, *, images: Sequence[BazelImage] = ()
    ) -> AsyncIterator[E2EStack]:
        fake_openai = FakeOpenAIServer(mock, host="0.0.0.0", port=0)
        await fake_openai.start()

        try:
            monkeypatch.setenv("OPENAI_UPSTREAM_URL", fake_openai.url)

            backend_port = pick_free_port()
            registry_proxy_config = RegistryProxyConfig(host="localhost", port=backend_port)
            agent_base_env = _build_agent_base_env(synced_db.config, backend_port)

            deps = BackendDeps(
                config=PropsConfig(agent_env=agent_base_env), registry_proxy_config=registry_proxy_config
            )

            async with ensure_agent_network(async_docker_client), run_backend(deps, port=backend_port) as _port:
                registry = AgentRegistry(
                    docker_client=async_docker_client,
                    db=synced_db,
                    db_config=synced_db.config,
                    agent_base_env=agent_base_env,
                    registry_config=registry_proxy_config,
                    extra_hosts=HOST_GATEWAY,
                )

                try:
                    stack = E2EStack(registry=registry, model=model, _proxy_port=backend_port)
                    stack.push_images(*images)
                    yield stack
                finally:
                    await registry.close()

        finally:
            await fake_openai.stop()

    yield _factory


@asynccontextmanager
async def multi_model_e2e_stack(
    mocks: Mapping[str, OpenAIModelProto],
    db: Database,
    async_docker_client: aiodocker.Docker,
    e2e_registry_url: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    images: Sequence[BazelImage] = (),
) -> AsyncIterator[E2EStack]:
    """Set up full e2e stack with multi-model routing.

    Each key in `mocks` is a model name routed to the corresponding mock.
    """
    fake_openai = MultiModelFakeOpenAI(dict(mocks), host="0.0.0.0", port=0)
    await fake_openai.start()

    try:
        _set_backend_env(monkeypatch, db.config, e2e_registry_url)
        monkeypatch.setenv("OPENAI_UPSTREAM_URL", fake_openai.url)

        backend_port = pick_free_port()
        registry_proxy_config = RegistryProxyConfig(host="localhost", port=backend_port)
        agent_base_env = _build_agent_base_env(db.config, backend_port)

        deps = BackendDeps(config=PropsConfig(agent_env=agent_base_env), registry_proxy_config=registry_proxy_config)

        async with ensure_agent_network(async_docker_client), run_backend(deps, port=backend_port) as _port:
            registry = AgentRegistry(
                docker_client=async_docker_client,
                db=db,
                db_config=db.config,
                agent_base_env=agent_base_env,
                registry_config=registry_proxy_config,
                extra_hosts=HOST_GATEWAY,
            )

            try:
                stack = E2EStack(registry=registry, model="", _proxy_port=backend_port)
                stack.push_images(*images)
                yield stack
            finally:
                await registry.close()

    finally:
        await fake_openai.stop()
