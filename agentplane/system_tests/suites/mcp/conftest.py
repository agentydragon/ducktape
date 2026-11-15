"""Policy resources shared by native and App-backed MCP suites."""

from collections.abc import AsyncIterator

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.policy_objects import PolicyObjects


@pytest.fixture
async def policy_objects(cluster: Cluster) -> AsyncIterator[PolicyObjects]:
    async with PolicyObjects(cluster) as objects:
        yield objects
