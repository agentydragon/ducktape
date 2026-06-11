"""Shared fixtures for multi-model orchestration e2e tests.

These fixtures set up a full e2e test stack with:
- MultiModelFakeOpenAI server routing models to different mocks
- LLM proxy pointing to the fake server
- AgentRegistry for container orchestration
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import aiodocker
import uvicorn

from agent_core_testing.responses import PlayGen
from openai_utils.model import OpenAIModelProto
from props.backend.app import app as backend_app
from props.db.config import DatabaseConfig
from props.orchestration.agent_registry import AgentRegistry
from props.testing.fake_openai_server import MultiModelFakeOpenAI
from props.testing.mocks import GraderMock, PropsMock

logger = logging.getLogger(__name__)

# Model names for multi-model routing
ORCHESTRATION_OPTIMIZER_MODEL = "test-orchestration-optimizer"
ORCHESTRATION_CRITIC_MODEL = "test-orchestration-critic"
ORCHESTRATION_GRADER_MODEL = "test-orchestration-grader"

# Hostname for containers to reach host services (configurable for host networking)
E2E_HOST_HOSTNAME = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")

# Host gateway for container access to host services (only needed for bridge networking)
HOST_GATEWAY = {E2E_HOST_HOSTNAME: "host-gateway"} if E2E_HOST_HOSTNAME == "host.docker.internal" else {}


def make_orchestration_grader_mock() -> GraderMock:
    """Create grader mock that fills all pending edges with credit=0.

    The grader daemon runs in DAEMON mode which has no submit tool.
    It processes edges until the drift handler sees no pending drift and aborts.
    """

    @GraderMock.mock(check_consumed=False)  # Daemon may be aborted before consuming all
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system message)

        # Get all pending edges
        pending = yield from m.list_pending_roundtrip()
        logger.info(f"Grader mock: got {len(pending)} pending edges")

        if not pending:
            # No pending edges - shouldn't happen but handle gracefully
            logger.warning("Grader mock: no pending edges to process")
            return

        # Group by (run, issue_id) to batch fill_remaining calls
        by_issue: dict[tuple[UUID, str], int] = defaultdict(int)
        for edge in pending:
            key = (edge.critique_run_id, edge.critique_issue_id)
            by_issue[key] += 1

        # Fill each issue's remaining edges
        for (run_id, issue_id), count in by_issue.items():
            logger.info(f"Grader mock: filling {count} edges for {run_id}/{issue_id}")
            yield from m.fill_remaining_roundtrip(run_id, issue_id, count, "Mock: no GT matches")

        # After filling, the drift handler will see no drift and abort the loop
        logger.info("Grader mock: all edges filled, drift handler should abort")

    return mock


@asynccontextmanager
async def _run_proxy(upstream_url: str, host: str = "0.0.0.0") -> AsyncIterator[int]:
    """Start LLM proxy server pointing to upstream and yield the port."""
    # Configure proxy to use our fake upstream
    os.environ["OPENAI_UPSTREAM_URL"] = upstream_url
    os.environ["OPENAI_API_KEY"] = "test-key"

    config = uvicorn.Config(backend_app, host=host, port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.01)
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"Proxy server failed to start: {exc}")

    actual_port = None
    for srv in server.servers:
        for socket in srv.sockets:
            actual_port = socket.getsockname()[1]
            break
        break

    assert actual_port is not None
    logger.info("LLM proxy started on port %d, upstream=%s", actual_port, upstream_url)

    try:
        yield actual_port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("LLM proxy stopped")


@asynccontextmanager
async def multi_model_e2e_stack(
    optimizer_mock: PropsMock,
    critic_mock: PropsMock,
    db_config: DatabaseConfig,
    docker_client: aiodocker.Docker,
    grader_mock: GraderMock | None = None,
) -> AsyncIterator[AgentRegistry]:
    """Set up full e2e stack with multi-model routing for orchestration tests.

    Args:
        optimizer_mock: Mock for optimizer agent
        critic_mock: Mock for critic agent
        db_config: Database configuration
        docker_client: Docker client
        grader_mock: Optional mock for grader daemon. If provided, grader model is added to routing.
    """
    mocks: dict[str, OpenAIModelProto] = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
    }
    if grader_mock is not None:
        mocks[ORCHESTRATION_GRADER_MODEL] = grader_mock

    # Start multi-model fake OpenAI server
    fake_openai = MultiModelFakeOpenAI(mocks, host="0.0.0.0", port=0)
    await fake_openai.start()

    try:
        # Start LLM proxy pointing to fake server
        async with _run_proxy(fake_openai.url, host="0.0.0.0") as proxy_port:
            # Create registry with proxy URL accessible from containers
            proxy_url = f"http://{E2E_HOST_HOSTNAME}:{proxy_port}"
            registry = AgentRegistry(
                docker_client=docker_client, db_config=db_config, llm_proxy_url=proxy_url, extra_hosts=HOST_GATEWAY
            )

            try:
                yield registry
            finally:
                await registry.close()

    finally:
        await fake_openai.stop()
