"""Fixtures for tests that run the Go sampler against the scripted provider and a real Postgres.

Register per module (`pytest_plugins`): it starts the typed execution proxy and a Postgres container.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from python.runfiles import Runfiles

from agentplane.sampler.settings import Settings
from agentplane.sampler.testing import Endpoint, settings
from agentplane.sampler.testing_store import Store
from agentplane.testing.codex_fixtures import ExecutorServer, code_mode_host, codex_binary, database_url
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.postgres_fixtures import postgres_admin_url, postgres_container, postgres_image
from testing.sqlalchemy_engines import postgres_async_engine

# These imported fixtures are part of this plugin's public pytest surface.
__all__ = [
    "code_mode_host",
    "codex_binary",
    "database_url",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
]


@pytest.fixture
async def endpoint() -> AsyncIterator[Endpoint]:
    async with Endpoint() as endpoint:
        yield endpoint


@pytest.fixture
def config(
    database_url: str, endpoint: Endpoint, execution_target: str, codex_binary: str, code_mode_host: str
) -> Settings:
    """Settings that sample from `endpoint` and execute through the shared adapter."""
    return settings(
        database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
    )


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with postgres_async_engine(database_url) as engine:
        yield Store(engine)


@pytest.fixture
async def executor_server() -> AsyncIterator[ExecutorServer]:
    """Serve the native execution backend through Runtime's typed proxy."""
    binary = (
        Runfiles.CreateOrRaise().root()
        / "ducktape/agentplane/env/runtime/execution/testing/typedproxy/typedproxy_bin_/typedproxy_bin"
    )
    with (undeclared_outputs_dir() / "sampler-native-executor.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            binary, "--listen", "127.0.0.1:0", stdout=asyncio.subprocess.PIPE, stderr=log
        )
        try:
            assert process.stdout is not None
            target = (await asyncio.wait_for(process.stdout.readline(), timeout=10)).decode().strip()
            if not target.startswith("127.0.0.1:"):
                raise RuntimeError(f"typed execution proxy did not publish its loopback target: {target!r}")
            yield ExecutorServer(target, process)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.fixture
async def execution_target(executor_server: ExecutorServer) -> str:
    return executor_server.url
