"""The three MCP service scenarios, isolated from the central sampler cases."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.harness_pair import run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.operator_login import OperatorCredentials
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import dcr, external_connection, mcp


async def test_deployed_discovery_and_registration(pytestconfig: pytest.Config, deployment: Stack) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    assert deployment.mcp_fixtures is not None
    await dcr.deployed_discovery_and_registration(deployment.mcp_fixtures.mcp_edge_url)


async def test_external_mcp_connection_uses_its_consented_identity(
    deployment: Stack, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> None:
    await external_connection.uses_its_consented_identity(deployment, operator_bff, operator_credentials)


@pytest.mark.scripted_model
async def test_policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await run_harness_pair_in_own_sandboxes(
        cluster,
        client,
        lambda harness, offered_model, sandbox: mcp.policy_binding_auto_approves_the_bound_sandbox(
            operator_bff, client, dispatcher, sandbox, harness, offered_model, scripted, policy_objects
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
