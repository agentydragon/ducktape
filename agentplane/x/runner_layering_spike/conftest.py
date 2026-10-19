"""Register runner subprocess fixtures for the image-layering experiment."""

import pytest

from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.fixtures import endpoint, gateway, harness, model, spec, workspace
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import start_runner


@pytest.fixture
def driver(gateway: Gateway) -> RunnerDriver:
    return RunnerDriver(gateway)
