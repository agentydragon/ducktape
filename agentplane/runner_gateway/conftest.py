"""Native Runner fixtures used by the Runner client acceptance tests."""

from pathlib import Path

import pytest

from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import config, endpoint, harness, model, runner, spec, workspace


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    return tmp_path / "runner-token"
