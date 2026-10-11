"""Register the shared runner fixtures for runner tests."""

from agentplane.runner.testing.failure_journal import pytest_runtest_makereport
from agentplane.runner.testing.fixtures import (
    client,
    config,
    endpoint,
    harness,
    model,
    runner,
    runner_client_factory,
    spec,
    workspace,
)
