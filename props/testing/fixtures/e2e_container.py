"""E2E test fixtures for container-based agent tests.

These fixtures set up the full e2e testing stack:
- Raw Docker registry (testcontainers, from e2e_infra)
- Real backend app (FastAPI - same as production)
- Real registry proxy app (FastAPI - same as production)
- Real LLM proxy app (FastAPI - same as production)
- Fake OpenAI server (returns scripted responses)
- AgentRegistry (orchestrates containers)

The container communicates through the real LLM proxy to the fake OpenAI server
and through the real backend for orchestration, exercising auth, request logging,
and registry proxy definition recording.

Environment variables:
    PROPS_E2E_HOST_HOSTNAME: Hostname for containers to reach host services.
        - Default: "host.docker.internal" (Docker bridge networking)
        - Set to "127.0.0.1" for host networking (e.g., CI with --network=host)

Usage:
    async def test_critic_completes(e2e_stack, all_files_scope, critic_image):
        mock = make_critic_mock()
        async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
            image = await stack.registry.resolve_image(AgentType.CRITIC, BUILTIN_TAG)
            run_id = await stack.registry.run_critic(
                image=image,
                example=all_files_scope,
                model=stack.model,
                timeout_seconds=60,
                parent_run_id=None,
                budget_usd=5.0,
            )
            # Assert on database state

    # Multi-model usage:
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
from fastapi import FastAPI

from openai_utils.model import OpenAIModelProto
from openai_utils.model_metadata import get_model_metadata
from props.backend.app import BackendDeps, create_app
from props.config import CustomModelConfig, DockerExecutorConfig, PropsConfig, UpstreamConfig
from props.core.agent_types import AgentType
from props.core.oci_utils import BUILTIN_TAG, RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.llm_proxy.app import create_app as create_llm_proxy_app  # alias: backend.app also defines create_app
from props.orchestration.agent_registry import AgentRegistry, ResolvedImage
from props.orchestration.docker_env import PROPS_NETWORK_NAME
from props.orchestration.docker_executor import DockerExecutor
from props.registry_proxy.app import create_app as create_registry_proxy_app
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fake_openai_server import FakeOpenAIServer
from util.net import pick_free_port
from util.oci import BazelImage, bazel_crane, push_bazel_image

logger = logging.getLogger(__name__)

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
    resolved_images: dict[str, ResolvedImage]  # repo_name → ResolvedImage

    @property
    def image_digests(self) -> dict[str, str]:
        """repo_name → digest (sha256:...), for backward compatibility."""
        return {name: img.digest for name, img in self.resolved_images.items()}


def _build_agent_base_env(db_config: DatabaseConfig) -> dict[str, str]:
    """Build agent_base_env for e2e tests.

    Agents reach postgres through E2E_HOST_HOSTNAME at the mapped port.
    """
    return {"PGHOST": E2E_HOST_HOSTNAME, "PGPORT": str(db_config.port), "PGDATABASE": db_config.database}


@asynccontextmanager
async def _serve_app(app: FastAPI, port: int, *, name: str, host: str = "0.0.0.0") -> AsyncIterator[None]:
    """Run a FastAPI app under uvicorn for the lifetime of the context."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)
        if task.done():
            raise RuntimeError(f"{name} failed to start on port {port}: {task.exception()}")

    logger.info("%s started on port %d", name, port)

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
        logger.info("%s stopped", name)


def _set_backend_env(monkeypatch: pytest.MonkeyPatch, db_config: DatabaseConfig, e2e_registry_url: str) -> None:
    """Set env vars needed by the backend's lifespan."""
    monkeypatch.setenv("PROPS_REGISTRY_UPSTREAM_URL", e2e_registry_url)
    monkeypatch.setenv("FAKE_OPENAI_API_KEY", "test-key")
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
    models: Sequence[str],
    images: Sequence[BazelImage] = (),
) -> AsyncIterator[E2EStack]:
    """Shared stack setup: start fake OpenAI, backend, proxies, registry, push images."""
    await fake_openai.start()

    try:
        _set_backend_env(monkeypatch, db.config, e2e_registry_url)
        backend_port = pick_free_port()
        llm_proxy_port = pick_free_port()
        registry_proxy_port = pick_free_port()
        registry_proxy_config = RegistryProxyConfig(host="localhost", port=registry_proxy_port)
        backend_url = f"http://{E2E_HOST_HOSTNAME}:{backend_port}"
        agent_registry_url = f"http://{E2E_HOST_HOSTNAME}:{registry_proxy_port}"
        # Agents talk to the standalone LLM proxy (the backend dropped its /v1
        # mount in #1894); the proxy reuses the backend's llm router + auth against
        # the same DB and forwards to the fake OpenAI upstream above.
        llm_proxy_url = f"http://{E2E_HOST_HOSTNAME}:{llm_proxy_port}"
        agent_base_env = _build_agent_base_env(db.config)

        config = PropsConfig(
            backend_url=backend_url,
            agent_env=agent_base_env,
            executor=DockerExecutorConfig(extra_hosts=HOST_GATEWAY),
            llm_proxy_url=llm_proxy_url,
            upstreams={"fake-openai": UpstreamConfig(url=f"{fake_openai.url}/v1", api_key_env="FAKE_OPENAI_API_KEY")},
            models=[
                CustomModelConfig(
                    name=model_id,
                    upstream="fake-openai",
                    upstream_model=model_id,
                    **get_model_metadata(model_id).model_dump(),
                )
                for model_id in models
            ],
        )
        with db.session() as session:
            sync_model_metadata_with_session(session, config)
            session.commit()
        deps = BackendDeps(config=config, registry_proxy_config=registry_proxy_config, backend_url=backend_url)

        async with (
            ensure_agent_network(async_docker_client),
            _serve_app(create_app(deps=deps), backend_port, name="backend"),
            _serve_app(create_llm_proxy_app(db=db, config=config), llm_proxy_port, name="llm-proxy"),
            _serve_app(create_registry_proxy_app(db=db), registry_proxy_port, name="registry-proxy"),
        ):
            executor = DockerExecutor(
                async_docker_client,
                network_name=PROPS_NETWORK_NAME,
                extra_hosts=HOST_GATEWAY,
                pull_auth={"username": db.config.user, "password": db.config.password},
            )
            registry = AgentRegistry(
                executor=executor,
                db=db,
                db_config=db.config,
                backend_url=backend_url,
                agent_base_env=agent_base_env,
                registry_config=registry_proxy_config,
                llm_base_url=llm_proxy_url,
                agent_registry_url=agent_registry_url,
            )

            try:
                proxy_url = f"localhost:{registry_proxy_port}"
                crane = bazel_crane(registry=proxy_url, username=db.config.user, password=db.config.password)
                resolved_images: dict[str, ResolvedImage] = {}
                for image in images:
                    digest = await push_bazel_image(crane, image, proxy_url, BUILTIN_TAG, insecure=True)
                    oci_ref = registry_proxy_config.build_oci_reference(AgentType(image.repo_name), digest)
                    resolved_images[image.repo_name] = ResolvedImage(digest=digest, oci_ref=oci_ref)
                yield E2EStack(registry=registry, model=model, resolved_images=resolved_images)
            finally:
                await registry.close()

    finally:
        await fake_openai.stop()


@pytest_asyncio.fixture
async def e2e_stack(
    synced_db: Database, async_docker_client: aiodocker.Docker, e2e_registry_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Fixture factory for creating e2e test stacks.

    Accepts a dict of mocks keyed by model name. Requests are routed
    to the mock matching the `model` field in the request body.

    Images pushed during setup are pre-resolved and available via
    stack.resolved_images["repo_name"].

    Usage:
        async def test_something(e2e_stack, critic_image):
            mock = make_my_mock()
            async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
                run_id = await stack.registry.run_critic(
                    image=stack.resolved_images["critic"], ...
                )

        # Multi-model
        async def test_orchestration(e2e_stack, ...):
            mocks = {"optimizer-model": opt_mock, "critic-model": crit_mock}
            async with e2e_stack(mocks, images=[...]) as stack:
                ...
    """

    @asynccontextmanager
    async def _factory(
        mocks: Mapping[str, OpenAIModelProto], model: str = DEFAULT_TEST_MODEL, *, images: Sequence[BazelImage] = ()
    ) -> AsyncIterator[E2EStack]:
        fake_openai = FakeOpenAIServer(dict(mocks), host="0.0.0.0", port=0)
        async with _make_stack(
            fake_openai,
            synced_db,
            async_docker_client,
            e2e_registry_url,
            monkeypatch,
            model=model,
            models=list(mocks),
            images=images,
        ) as stack:
            yield stack

    yield _factory
