"""MCP discovery, policy binding and Pod central sampler on one k3s stack."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.harness_pair import run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.operator_login import OperatorCredentials
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler, dcr, mcp


async def test_deployed_discovery_and_registration(pytestconfig: pytest.Config, deployment: Stack) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    assert deployment.mcp_fixtures is not None
    await dcr.deployed_discovery_and_registration(deployment.mcp_fixtures.mcp_edge_url)


async def test_external_mcp_connection_uses_its_consented_identity(
    deployment: Stack, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> None:
    assert deployment.mcp_fixtures is not None
    await mcp.external_mcp_connection_uses_its_consented_identity(
        operator_bff,
        operator_credentials,
        server=deployment.mcp_fixtures.mcp_edge_url,
        ca_bundle=deployment.mcp_fixtures.ca_bundle,
    )


@pytest.mark.scripted_model
async def test_policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    await run_harness_pair_in_own_sandboxes(
        cluster,
        client,
        lambda harness, offered_model, sandbox: mcp.policy_binding_auto_approves_the_bound_sandbox(
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
async def test_central_sampler_action_wait_keeps_other_code_mode_cells_live(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream, operator_bff: httpx.AsyncClient
) -> None:
    await central_sampler.delegated_action_wait_keeps_code_mode_live(
        cluster,
        tmp_path,
        scripted,
        Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-actions"),
        operator_bff,
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_pod_central_sampler_exec_resumes_after_sandbox_suspend_resume(
    cluster: Cluster, client: Client, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.pod_exec_session_rebinds_after_sandbox_suspend_resume(
        cluster, client, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-resume")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_roll_keeps_live_central_read_and_turn(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_roll_keeps_live_central_read_and_turn(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-roll")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_exec_server_loss_notifies_active_central_turn(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_outage_restarts_exec_server_then_notifies_model(
        cluster,
        tmp_path,
        scripted,
        Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-native-loss"),
    )


if __name__ == "__main__":
    pytest_bazel.main()
