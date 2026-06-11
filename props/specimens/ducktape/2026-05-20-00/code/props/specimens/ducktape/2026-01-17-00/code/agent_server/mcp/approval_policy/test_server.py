from __future__ import annotations

import pytest

from mcp_utils.resources import extract_single_text_content


@pytest.mark.requires_docker
async def test_resources_list_and_read_policy(make_typed_mcp, approval_policy_server):
    """List and read resources directly from the server without a compositor."""
    server = approval_policy_server.reader

    async with make_typed_mcp(server) as (_, sess):
        # Approval policy server exposes resources for policy and pending calls
        items = await sess.list_resources()
        assert isinstance(items, list)
        # Find the policy.py resource
        policy_resource = next((r for r in items if r.name == "policy.py"), None)
        assert policy_resource is not None
        assert str(policy_resource.uri) == str(server.active_policy_resource.uri)
        assert policy_resource.mimeType == "text/x-python"

        # Read the resource content and ensure it contains the policy code
        result = await sess.read_resource(server.active_policy_resource.uri)
        policy_text = extract_single_text_content(result)
        assert "def decide" in policy_text
        assert "PolicyResponse" in policy_text
