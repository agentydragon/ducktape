"""External MCP connection scenarios using the operator's consented Identity."""

from __future__ import annotations

import httpx
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.mcp_edge.api.connection import connection_service_pb2
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.mcp_connection import connected_session
from agentplane.system_tests.driver.operator_login import OperatorCredentials


async def uses_its_consented_identity(
    deployment: Stack, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> None:
    """A newly consented external MCP connection uses its selected Identity."""
    assert deployment.mcp_fixtures is not None
    response = await operator_bff.get("/upstream/connections/v1/operator/connection-identities")
    assert response.status_code == 200
    inventory = ParseDict(response.json(), connection_service_pb2.ListConnectionIdentitiesResponse())
    identity = one(candidate for candidate in inventory.identities if candidate.name == "mcp-edge-test-caller")
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
