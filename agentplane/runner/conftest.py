"""Register the shared runner fixtures for runner tests."""

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
