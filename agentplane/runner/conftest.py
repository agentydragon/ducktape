"""Register the shared runner fixtures for runner tests."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentplane.runner.journal import Journal
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import (
    config,
    driver,
    endpoint,
    gateway,
    harness,
    model,
    runner,
    spec,
    workspace,
)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as opened:
        yield opened
