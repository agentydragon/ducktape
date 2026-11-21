"""Fixtures for tests that run the Go sampler against the scripted provider and a real Postgres."""

from collections.abc import AsyncIterator

import pytest

from agentplane.sampler.settings import Settings
from agentplane.sampler.testing import Endpoint, settings
from agentplane.sampler.testing_store import Store
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.sqlalchemy_engines import postgres_async_engine


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
