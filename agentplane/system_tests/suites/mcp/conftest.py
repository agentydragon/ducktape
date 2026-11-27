"""Policy resources and operator credentials shared by the MCP suites."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.suites.mcp.operator_fixtures import operator_bff, operator_credentials


@pytest.fixture
async def policy_objects(cluster: Cluster) -> AsyncIterator[PolicyObjects]:
    async with PolicyObjects(cluster) as objects:
        yield objects
