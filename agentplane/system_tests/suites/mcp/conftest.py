"""Operator login and policy resources shared by the MCP suites."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.k3s.stack import Stack
from agentplane.system_tests.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    login_operator,
    read_operator_credentials,
    verify_action_federation,
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
        origin = str(app_origin(base_url)).rstrip("/")
        async with httpx.AsyncClient(base_url=origin, timeout=30, follow_redirects=False) as http:
            try:
                await login_operator(http, operator_credentials)
            except LoginBlockedError as exc:
                pytest.fail(str(exc), pytrace=False)
            except httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError:
                pytest.fail("BLOCKED: OIDC transport or response invalid; auth details withheld", pytrace=False)
            http.headers["Origin"] = origin
            await verify_action_federation(http)
            yield http
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)
    finally:
        logging.disable(previous_logging)
