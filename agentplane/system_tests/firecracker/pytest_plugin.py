"""Firecracker runtime fixtures for guest deployment suites."""

import pytest

from agentplane.system_tests.deployment.virtualization import RuntimeFactory
from agentplane.system_tests.firecracker.runtime import runtime


@pytest.fixture(scope="session")
def firecracker_runtime_factory() -> RuntimeFactory:
    return runtime
