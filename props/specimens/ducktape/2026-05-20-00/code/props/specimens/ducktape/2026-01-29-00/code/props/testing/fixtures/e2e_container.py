"""E2E test fixtures for container-based agent tests.

These fixtures set up the full e2e testing stack:
- Fake OpenAI server (returns scripted responses)
- LLM proxy (validates auth, logs requests)
- AgentRegistry (orchestrates containers)

The container communicates through the real LLM proxy to the fake OpenAI server,
exercising the full production code path including auth and request logging.

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
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import aiodocker
import pytest_asyncio
import uvicorn
from fastapi import FastAPI

from openai_utils.model import OpenAIModelProto
from props.backend.auth import AuthMiddleware
from props.backend.routes import llm
from props.db.config import DatabaseConfig
from props.orchestration.agent_registry import AgentRegistry
from props.testing.fake_openai_server import FakeOpenAIServer


def create_test_proxy_app() -> FastAPI:
    """Create a minimal FastAPI app with just the LLM proxy for e2e tests.

    This is a lightweight version of the full backend that only includes
    the LLM proxy routes. It doesn't start grader daemons or other services
    that are not needed for e2e container tests.
    """
    app = FastAPI(title="Test LLM Proxy")
    app.add_middleware(AuthMiddleware)
    app.include_router(llm.router, tags=["llm_proxy"])
    return app


logger = logging.getLogger(__name__)

# Default model name for tests
TEST_MODEL = "test-model"

# Hostname for containers to reach host services.
# - Default "host.docker.internal" works with Docker bridge networking
# - Set to "127.0.0.1" or "localhost" when using host networking (e.g., CI environments)
E2E_HOST_HOSTNAME = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")

# Host gateway for container access to host services (only needed for bridge networking)
HOST_GATEWAY = {E2E_HOST_HOSTNAME: "host-gateway"} if E2E_HOST_HOSTNAME == "host.docker.internal" else {}


@dataclass
class E2EStack:
    """Running e2e test stack with all services."""

    fake_openai: FakeOpenAIServer
    proxy_port: int
    registry: AgentRegistry
    model: str

    @property
    def proxy_url(self) -> str:
        """LLM proxy URL accessible from containers."""
        return f"http://{E2E_HOST_HOSTNAME}:{self.proxy_port}"


class _ProxyServer:
    """LLM proxy server wrapper for testing."""

    def __init__(self, upstream_url: str, host: str = "0.0.0.0", port: int = 0) -> None:
        self._upstream_url = upstream_url
        self._host = host
        self._port = port
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._actual_port: int | None = None

    @property
    def port(self) -> int:
        if self._actual_port is None:
            raise RuntimeError("Server not started")
        return self._actual_port

    @property
    def url(self) -> str:
        if self._actual_port is None:
            raise RuntimeError("Server not started")
        return f"http://{self._host}:{self._actual_port}"

    async def start(self) -> None:
        # Configure proxy to use our fake upstream
        os.environ["OPENAI_UPSTREAM_URL"] = self._upstream_url
        # Use a dummy API key (fake server doesn't validate it)
        os.environ["OPENAI_API_KEY"] = "test-key"

        # Create test-specific app with just LLM proxy routes
        test_app = create_test_proxy_app()
        config = uvicorn.Config(test_app, host=self._host, port=self._port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        while not self._server.started:
            await asyncio.sleep(0.01)
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"Proxy server failed to start: {exc}")

        for server in self._server.servers:
            for socket in server.sockets:
                self._actual_port = socket.getsockname()[1]
                break
            break

        logger.info("LLM proxy started on port %d, upstream=%s", self._actual_port, self._upstream_url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                try:
                    await asyncio.wait_for(self._task, timeout=5.0)
                except TimeoutError:
                    self._task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._task
            logger.info("LLM proxy stopped")


@asynccontextmanager
async def e2e_stack_context(
    mock: OpenAIModelProto, db_config: DatabaseConfig, docker_client: aiodocker.Docker, model: str = TEST_MODEL
) -> AsyncIterator[E2EStack]:
    """Create and manage the full e2e test stack.

    Sets up:
    1. Fake OpenAI server with the provided mock
    2. LLM proxy pointing to fake server
    3. AgentRegistry configured to use the proxy

    The fake OpenAI server and proxy bind to 0.0.0.0 so they're accessible
    from Docker containers via host.docker.internal.

    Args:
        mock: Mock implementing OpenAIModelProto (e.g., PropsMock, StepRunner)
        db_config: Database configuration for agent runs
        docker_client: Docker client for running containers
        model: Model name to use for agent runs (default: "test-model")

    Yields:
        E2EStack with all services running
    """
    # Start fake OpenAI server (bind to 0.0.0.0 for container access)
    fake_openai = FakeOpenAIServer(mock, host="0.0.0.0", port=0)
    await fake_openai.start()

    try:
        # Start LLM proxy pointing to fake server
        proxy = _ProxyServer(upstream_url=fake_openai.url, host="0.0.0.0", port=0)
        await proxy.start()

        try:
            # Create registry with proxy URL accessible from containers
            proxy_url = f"http://{E2E_HOST_HOSTNAME}:{proxy.port}"
            registry = AgentRegistry(
                docker_client=docker_client, db_config=db_config, llm_proxy_url=proxy_url, extra_hosts=HOST_GATEWAY
            )

            yield E2EStack(fake_openai=fake_openai, proxy_port=proxy.port, registry=registry, model=model)

            await registry.close()

        finally:
            await proxy.stop()

    finally:
        await fake_openai.stop()


# Type alias for the fixture factory
E2EStackFactory = Callable[[OpenAIModelProto], AbstractAsyncContextManager[E2EStack]]


@pytest_asyncio.fixture
async def e2e_stack(
    synced_test_db: DatabaseConfig, async_docker_client: aiodocker.Docker
) -> AsyncIterator[Callable[[OpenAIModelProto], AbstractAsyncContextManager[E2EStack]]]:
    """Fixture factory for creating e2e test stacks.

    Usage:
        async def test_something(e2e_stack, all_files_scope):
            mock = make_my_mock()
            async with e2e_stack(mock) as stack:
                run_id = await stack.registry.run_critic(...)
    """

    @asynccontextmanager
    async def _factory(mock: OpenAIModelProto, model: str = TEST_MODEL) -> AsyncIterator[E2EStack]:
        async with e2e_stack_context(mock, synced_test_db, async_docker_client, model) as stack:
            yield stack

    yield _factory
