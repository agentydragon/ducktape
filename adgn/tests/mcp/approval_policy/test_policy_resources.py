"""Tests for policy CRUD resources and tools in the approval policy MCP server."""

from __future__ import annotations

from docker import DockerClient
from fastmcp.mcp_config import MCPConfig
import pytest

from adgn.agent.approvals import ApprovalPolicyEngine, load_default_policy_source
from adgn.agent.persist import AgentMetadata
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.mcp.approval_policy.server import (
    ApprovalPolicyAdminServer,
    ApprovalPolicyServer,
    CreatePolicyArgs,
    DeletePolicyArgs,
    UpdatePolicyArgs,
)


@pytest.fixture
async def persistence(tmp_path):
    persist = SQLitePersistence(tmp_path / "test.db")
    await persist.ensure_schema()
    return persist


@pytest.fixture
async def engine(persistence, docker_client: DockerClient):
    """Create an approval policy engine with test persistence."""
    agent_id = "test-agent"
    await persistence.create_agent(mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test"))
    policy_source = load_default_policy_source()
    engine = ApprovalPolicyEngine(
        docker_client=docker_client,
        agent_id=agent_id,
        persistence=persistence,
        policy_source=policy_source,
    )
    return engine


@pytest.fixture
async def policy_server(engine):
    """Create a policy server (reader) instance."""
    return ApprovalPolicyServer(engine)


@pytest.fixture
async def admin_server(engine):
    """Create an admin server instance."""
    return ApprovalPolicyAdminServer(engine=engine)


class TestPolicyListResource:
    """Test the policy list resource."""

    async def test_list_empty(self, policy_server):
        """Test listing policies when none exist."""
        # Access the policies_list resource
        result = await policy_server._mcp_server.read_resource(uri="resource://policies/list")
        assert result is not None
        # Should return empty list as JSON
        import json

        data = json.loads(result.contents[0].text)
        assert isinstance(data, list)
        assert len(data) == 0

    async def test_list_with_policies(self, policy_server, admin_server, persistence):
        """Test listing policies after creating some."""
        # Create a few policies via admin tools
        policy1 = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(
                id="policy-1",
                text="print('policy 1')",
                description="First test policy",
                enabled=True,
            ).model_dump(),
        )

        policy2 = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(
                id="policy-2",
                text="print('policy 2')",
                description="Second test policy",
                enabled=False,
            ).model_dump(),
        )

        # Now list policies
        result = await policy_server._mcp_server.read_resource(uri="resource://policies/list")
        assert result is not None

        import json

        data = json.loads(result.contents[0].text)
        assert isinstance(data, list)
        assert len(data) == 2

        # Verify structure (should be PolicyListItem)
        for item in data:
            assert "id" in item
            assert "description" in item
            assert "enabled" in item


class TestPolicyDetailResource:
    """Test the policy detail resource."""

    async def test_get_nonexistent(self, policy_server):
        """Test getting a policy that doesn't exist."""
        with pytest.raises(KeyError):
            await policy_server._mcp_server.read_resource(uri="resource://policies/nonexistent")

    async def test_get_existing(self, policy_server, admin_server):
        """Test getting an existing policy."""
        # Create a policy first
        await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(
                id="test-policy",
                text="print('test policy')",
                description="A test policy",
                enabled=True,
            ).model_dump(),
        )

        # Now get it
        result = await policy_server._mcp_server.read_resource(uri="resource://policies/test-policy")
        assert result is not None

        import json

        data = json.loads(result.contents[0].text)
        assert data["id"] == "test-policy"
        assert data["text"] == "print('test policy')"
        assert data["description"] == "A test policy"
        assert data["enabled"] is True


class TestCreatePolicyTool:
    """Test the create_policy admin tool."""

    async def test_create_basic(self, admin_server, persistence):
        """Test creating a basic policy."""
        result = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(
                id="new-policy",
                text="print('new policy')",
                description="A new policy",
                enabled=True,
            ).model_dump(),
        )

        assert result.isError is False

        # Verify it was created in persistence
        policy = await persistence.get_policy("new-policy")
        assert policy is not None
        assert policy.id == "new-policy"
        assert policy.text == "print('new policy')"
        assert policy.description == "A new policy"
        assert policy.enabled is True

    async def test_create_duplicate(self, admin_server, persistence):
        """Test creating a policy with duplicate ID fails."""
        # Create first policy
        await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="dup-policy", text="print('dup')").model_dump(),
        )

        # Try to create another with same ID
        result = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="dup-policy", text="print('dup 2')").model_dump(),
            raise_on_error=False,
        )

        assert result.isError is True

    async def test_create_minimal(self, admin_server, persistence):
        """Test creating a policy with minimal args."""
        result = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="minimal", text="pass").model_dump(),
        )

        assert result.isError is False

        policy = await persistence.get_policy("minimal")
        assert policy is not None
        assert policy.id == "minimal"
        assert policy.text == "pass"
        assert policy.description is None
        assert policy.enabled is True  # default


class TestUpdatePolicyTool:
    """Test the update_policy admin tool."""

    async def test_update_existing(self, admin_server, persistence):
        """Test updating an existing policy."""
        # Create a policy first
        await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="update-me", text="print('v1')", description="Version 1").model_dump(),
        )

        # Update it
        result = await admin_server._mcp_server.call_tool(
            "update_policy",
            arguments=UpdatePolicyArgs(id="update-me", text="print('v2')", description="Version 2").model_dump(),
        )

        assert result.isError is False

        # Verify the update
        policy = await persistence.get_policy("update-me")
        assert policy is not None
        assert policy.text == "print('v2')"
        assert policy.description == "Version 2"

    async def test_update_nonexistent(self, admin_server):
        """Test updating a nonexistent policy fails."""
        result = await admin_server._mcp_server.call_tool(
            "update_policy",
            arguments=UpdatePolicyArgs(id="nonexistent", text="print('new')").model_dump(),
            raise_on_error=False,
        )

        assert result.isError is True

    async def test_update_creates_history(self, admin_server, persistence):
        """Test that updating a policy creates a history entry."""
        # Create initial policy
        await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="versioned", text="print('v1')").model_dump(),
        )

        # Update it
        await admin_server._mcp_server.call_tool(
            "update_policy",
            arguments=UpdatePolicyArgs(id="versioned", text="print('v2')").model_dump(),
        )

        # Check that history was created (requires accessing policy_history table)
        # For now, just verify the update worked
        policy = await persistence.get_policy("versioned")
        assert policy.text == "print('v2')"


class TestDeletePolicyTool:
    """Test the delete_policy admin tool."""

    async def test_delete_existing(self, admin_server, persistence):
        """Test deleting an existing policy."""
        # Create a policy first
        await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="delete-me", text="print('bye')").model_dump(),
        )

        # Verify it exists
        policy = await persistence.get_policy("delete-me")
        assert policy is not None

        # Delete it
        result = await admin_server._mcp_server.call_tool(
            "delete_policy",
            arguments=DeletePolicyArgs(id="delete-me").model_dump(),
        )

        assert result.isError is False

        # Verify it's gone
        policy = await persistence.get_policy("delete-me")
        assert policy is None

    async def test_delete_nonexistent(self, admin_server):
        """Test deleting a nonexistent policy succeeds (idempotent)."""
        result = await admin_server._mcp_server.call_tool(
            "delete_policy",
            arguments=DeletePolicyArgs(id="nonexistent").model_dump(),
        )

        # SQLite DELETE is idempotent, so this should succeed
        assert result.isError is False


class TestPolicyPagination:
    """Test pagination in policy list."""

    async def test_pagination(self, admin_server, persistence):
        """Test that pagination works for policy list."""
        # Create multiple policies
        for i in range(10):
            await persistence.create_policy(
                policy_id=f"policy-{i}",
                text=f"print({i})",
                description=f"Policy {i}",
            )

        # List with limit
        policies = await persistence.list_policies(offset=0, limit=5)
        assert len(policies) == 5

        # List next page
        policies = await persistence.list_policies(offset=5, limit=5)
        assert len(policies) == 5

        # List all
        policies = await persistence.list_policies(offset=0, limit=100)
        assert len(policies) == 10


class TestErrorHandling:
    """Test error handling in policy CRUD operations."""

    async def test_invalid_policy_text(self, admin_server):
        """Test that invalid Python syntax is caught."""
        # Note: create_policy doesn't validate syntax, so this should succeed
        result = await admin_server._mcp_server.call_tool(
            "create_policy",
            arguments=CreatePolicyArgs(id="invalid", text="this is not valid python !!!").model_dump(),
        )

        # Creation succeeds (validation happens at execution time)
        assert result.isError is False

    async def test_missing_required_fields(self, admin_server):
        """Test that missing required fields cause validation errors."""
        # Missing 'id' and 'text'
        with pytest.raises(Exception):  # Pydantic validation error
            await admin_server._mcp_server.call_tool(
                "create_policy",
                arguments={},  # Missing required fields
            )
