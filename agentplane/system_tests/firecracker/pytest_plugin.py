"""Statically select the real Firecracker suite's lifecycle extension."""

import pytest

from agentplane.system_tests.deployment.virtualization import VirtualizationFactory
from agentplane.system_tests.firecracker.runtime import Runtime


@pytest.fixture(scope="session")
def virtualization_factory() -> VirtualizationFactory:
    return Runtime
