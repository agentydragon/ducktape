from __future__ import annotations

import pytest

from adgn.mcp._shared.constants import APPROVAL_POLICY_RESOURCE_URI
from adgn.mcp.approval_policy.server import ApprovalPolicyServer


@pytest.mark.requires_docker
async def test_resources_list_and_read_policy(make_typed_mcp, approval_engine):
    """List and read resources directly from the server without a compositor."""
    server = ApprovalPolicyServer(approval_engine)

    async with make_typed_mcp(server, "approval_policy") as (client, _sess):
        # Approval policy server exposes a single canonical resource for the active policy
        items = await client.list_resources()
        assert isinstance(items, list)
        # Only the approval policy server is present, so exactly one resource
        assert len(items) == 1
        it = items[0]
        assert str(it.uri) == str(APPROVAL_POLICY_RESOURCE_URI)
        assert it.name == "policy.py"
        assert it.mimeType == "text/x-python"

        # Read the resource content and ensure it contains the policy class
        contents = await client.read_resource(str(APPROVAL_POLICY_RESOURCE_URI))
        text_parts = [p for p in contents if getattr(p, "mimeType", None) == "text/x-python"]
        assert any("class ApprovalPolicy" in getattr(p, "text", "") for p in text_parts)
