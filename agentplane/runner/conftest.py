"""Register the shared runner fixtures for runner tests."""

from agentplane.runner.testing.executor_companion import executor_companion
from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import (
    config,
    driver,
    endpoint,
    gateway,
    harness,
    model,
    runner,
    runner_gateway,
    spec,
    workspace,
)
from agentplane.runner.testing.process_fixtures import start_runner
