"""External MCP connection scenarios using the operator's consented Identity."""

from __future__ import annotations

import httpx
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.action_service.api.contracts import action_service_pb2
from agentplane.mcp_edge.api.connection import connection_service_pb2
from agentplane.net.peertls.config import MCP_EDGE_URI
from agentplane.system_tests.deployment import mcp_servers
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.mcp_connection import connected_session
from agentplane.system_tests.driver.operator_login import OperatorCredentials
from agentplane.system_tests.scenarios import mcp


async def uses_its_consented_identity(
    deployment: Stack, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> None:
    """A newly consented external MCP connection uses its selected Identity."""
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
