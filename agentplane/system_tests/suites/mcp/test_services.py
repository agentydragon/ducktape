"""MCP discovery, policy binding and Pod central sampler on one k3s stack."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.action_service.api.contracts import action_service_pb2
from agentplane.app.testing.client import Client
from agentplane.mcp_edge.api.connection import connection_service_pb2
from agentplane.net.peertls.config import MCP_EDGE_URI
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import mcp_servers
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.harness_pair import run_harness_pair_in_own_sandboxes
from agentplane.system_tests.driver.mcp_connection import connected_session
from agentplane.system_tests.driver.operator_login import OperatorCredentials
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
    response = await operator_bff.get("/upstream/connections/v1/operator/connection-identities")
    assert response.status_code == 200
    inventory = ParseDict(response.json(), connection_service_pb2.ListConnectionIdentitiesResponse())
    identity = one(candidate for candidate in inventory.identities if candidate.name == "mcp-edge-test-caller")
    response = await operator_bff.get("/upstream/connections/v1/operator/connections")
    assert response.status_code == 200
    connections_before = ParseDict(response.json(), connection_service_pb2.ListConnectionsResponse())
    connection_ids_before = {connection.id for connection in connections_before.connections}
    async with connected_session(
        server=deployment.mcp_fixtures.mcp_edge_url,
        operator=operator_bff,
        credentials=operator_credentials,
        identity=identity,
        ca_bundle=deployment.mcp_fixtures.ca_bundle,
    ) as session:
        catalog = await session.list_tools()
        assert "get_action_policy" in {tool.name for tool in catalog.tools}
        policy = await session.call_tool("get_action_policy", arguments={"include_fields": ["subject"]})
        assert not policy.is_error
        assert policy.structured_content == {"subject": {"name": identity.name, "uid": identity.uid}}

        response = await operator_bff.get("/upstream/connections/v1/operator/connections")
        assert response.status_code == 200
        connections_after = ParseDict(response.json(), connection_service_pb2.ListConnectionsResponse())
        connection = one(
            candidate for candidate in connections_after.connections if candidate.id not in connection_ids_before
        )
        assert connection.bound_identity == identity
        grant = one(
            candidate
            for candidate in connection.grants
            if candidate.status == connection_service_pb2.GRANT_STATUS_ACTIVE
        )

        action_marker = f"external-MCP-{connection.id}"
        submitted = await session.call_tool(
            "request_action",
            arguments={
                "request": {
                    "idempotency_key": f"system-test-{connection.id}",
                    "action": {"group": mcp_servers.EVERYTHING_GROUP, "name": mcp_servers.ECHO},
                    "arguments": {"message": action_marker},
                    "title": "Record an external MCP receipt",
                },
                "respond_with": "receipt",
                "include_fields": ["id"],
            },
        )
        assert not submitted.is_error
        assert isinstance(submitted.structured_content, dict)
        request_id = submitted.structured_content.get("id")
        assert isinstance(request_id, str)

        # Keep the case self-contained: deny the pending request before checking its retained receipt.
        await mcp._decide_pending(operator_bff, request_id, action_service_pb2.VERDICT_DENY)
        receipt_response = await operator_bff.get(f"/upstream/actions/v1/action-requests/{request_id}")
        assert receipt_response.status_code == 200
        receipt = mcp._bff_request(receipt_response)
        assert receipt.state == action_service_pb2.ACTION_STATE_DENIED
        assert receipt.action.group == mcp_servers.EVERYTHING_GROUP
        assert receipt.action.name == mcp_servers.ECHO
        assert receipt.arguments.fields["message"].string_value == action_marker
        assert receipt.HasField("caller_identity")
        assert not receipt.HasField("caller")
        assert receipt.caller_identity == identity

        assert receipt.delegating_actor.WhichOneof("actor") == "service_spiffe_id"
        assert receipt.delegating_actor.service_spiffe_id == MCP_EDGE_URI

        assert receipt.WhichOneof("provenance") == "connection_provenance"
        assert receipt.connection_provenance.connection_id == connection.id
        assert receipt.connection_provenance.grant_id == grant.id


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
