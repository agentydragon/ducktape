"""Statically select the real Firecracker suite's lifecycle extension."""

import pytest

from agentplane.system_tests.k3s.firecracker_runtime import FirecrackerRuntime
from agentplane.system_tests.k3s.virtualization import VirtualizationFactory


@pytest.fixture(scope="session")
def virtualization_factory() -> VirtualizationFactory:
    return FirecrackerRuntime
