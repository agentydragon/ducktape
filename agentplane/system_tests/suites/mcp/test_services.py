"""MCP discovery, policy binding and Pod central sampler on one k3s stack."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.app.testing.client import Client
from agentplane.mcp_edge import connection_service_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.executor import Executor
from agentplane.system_tests.harness_pair import run_harness_pair
from agentplane.system_tests.k3s.stack import Stack
from agentplane.system_tests.mcp_connection import connected_session
from agentplane.system_tests.operator_login import OperatorCredentials
from agentplane.system_tests.sandbox import POD_TEMPLATE, Sandboxes
from agentplane.system_tests.scenarios import central_sampler, dcr, mcp
from agentplane.system_tests.scripted_upstream import ScriptedUpstream


async def test_deployed_discovery_and_registration(pytestconfig: pytest.Config, deployment: Stack) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    assert deployment.mcp_fixtures is not None
    await dcr.deployed_discovery_and_registration(deployment.mcp_fixtures.mcp_edge_url)


async def test_external_mcp_connection_uses_its_consented_account(
    deployment: Stack, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> None:
    assert deployment.mcp_fixtures is not None
    response = await operator_bff.get("/upstream/connections/v1/operator/caller-service-accounts")
    assert response.status_code == 200
    inventory = ParseDict(response.json(), connection_service_pb2.ListCallerServiceAccountsResponse())
    account = one(inventory.service_accounts)
    async with connected_session(
        server=deployment.mcp_fixtures.mcp_edge_url,
        operator=operator_bff,
        credentials=operator_credentials,
        account=account,
        ca_bundle=deployment.mcp_fixtures.ca_bundle,
    ) as session:
        catalog = await session.list_tools()
        assert "get_action_policy" in {tool.name for tool in catalog.tools}
        policy = await session.call_tool("get_action_policy", arguments={"include_fields": ["subject"]})
        assert not policy.is_error
        assert policy.structured_content == {"subject": {"namespace": account.namespace, "name": account.name}}


@pytest.mark.scripted_model
async def test_policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await run_harness_pair(
        client,
        lambda harness, offered_model: mcp.policy_binding_auto_approves_the_bound_sandbox(
            operator_bff, client, dispatcher, sandbox, harness, offered_model, scripted, policy_objects
        ),
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_dispatcher_central_setup_tool_and_retained_answer(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.dispatcher_central_setup_tool_and_retained_answer(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-acceptance")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_pod_central_sampler_exec_resumes_after_sandbox_suspend_resume(
    cluster: Cluster, client: Client, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.pod_exec_session_rebinds_after_sandbox_suspend_resume(
        cluster, client, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-resume")
    )


if __name__ == "__main__":
    pytest_bazel.main()
