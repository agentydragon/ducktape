"""Operator login and policy resources shared by the MCP suites."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    operator_bff_client,
    read_operator_credentials,
)
from agentplane.system_tests.scenarios import mcp


@pytest.fixture
async def policy_objects(cluster: Cluster) -> AsyncIterator[mcp.PolicyObjects]:
    objects = mcp.PolicyObjects(cluster=cluster)
    try:
        yield objects
    finally:
        await objects.delete_all()


@pytest.fixture
def operator_credentials(pytestconfig: pytest.Config, deployment: Stack) -> OperatorCredentials:
    if deployment.mcp_fixtures is None:
        raise ValueError("operator login requires MCP fixtures in this deployment")
    if pytestconfig.option.showlocals:
        pytest.fail("BLOCKED: disable pytest local-variable dumps before reading operator credentials", pytrace=False)
    try:
        return read_operator_credentials(deployment.cluster, secret_name=deployment.mcp_fixtures.operator_secret)
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture
async def operator_bff(base_url: str, operator_credentials: OperatorCredentials) -> AsyncIterator[httpx.AsyncClient]:
    """A real app-issued session. No credential injection, fabricated cookies, or DB writes."""
    # HTTP debug logging includes cookie/token headers and callback query strings. Suppress it
    # throughout this client lifetime (including failed assertions/teardown), even under --log-cli-level.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with operator_bff_client(base_url, operator_credentials) as http:
            yield http
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)
    finally:
        logging.disable(previous_logging)
