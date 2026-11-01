"""MCP Action execution, launcher ownership and OAuth on one k3s stack."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.harness_pair import run_harness_pair
from agentplane.system_tests.operator_login import OperatorCredentials
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scenarios import mcp
from agentplane.system_tests.scripted_upstream import ScriptedUpstream

# gazelle:include_dep @pypi//protobuf


@pytest.mark.scripted_model
async def test_agent_executes_mcp_action(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await run_harness_pair(
        client,
        lambda harness, offered_model: mcp.agent_executes_mcp_action(
            client, dispatcher, sandbox, harness, offered_model, scripted, policy_objects
        ),
    )


@pytest.mark.scripted_model
async def test_agent_mcp_bff_decision(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
) -> None:
    await run_harness_pair(
        client,
        lambda harness, offered_model: mcp.agent_mcp_bff_decision(
            operator_bff, operator_credentials, client, dispatcher, sandbox, harness, offered_model, scripted
        ),
    )


@pytest.mark.scripted_model
async def test_launcher_drives_only_the_sandbox_it_launched(
    cluster: Cluster,
    operator_bff: httpx.AsyncClient,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await run_harness_pair(
        client,
        lambda harness, offered_model: mcp.launcher_drives_only_the_sandbox_it_launched(
            cluster, operator_bff, client, dispatcher, sandbox, harness, offered_model, scripted, policy_objects
        ),
    )


# Both harnesses change the same operator/server OAuth linkage, so keep these cases sequential.
@pytest.mark.scripted_model
async def test_operator_links_oauth_mcp_server(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await mcp.operator_links_oauth_mcp_server(
        operator_bff,
        operator_credentials,
        client,
        dispatcher,
        sandbox,
        harness,
        offered_model,
        scripted,
        policy_objects,
    )


if __name__ == "__main__":
    pytest_bazel.main()
