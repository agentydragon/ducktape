"""Firecracker runtime fixtures for guest deployment suites."""

import pytest

from agentplane.system_tests.deployment.virtualization import RuntimeFactory
from agentplane.system_tests.firecracker.runtime import runtime

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


@pytest.fixture(scope="session")
def firecracker_runtime_factory() -> RuntimeFactory:
    return runtime
