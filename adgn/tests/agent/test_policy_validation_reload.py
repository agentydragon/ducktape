"""Tests for policy validation and reload functionality."""

from docker import DockerClient
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig
from hamcrest import assert_that, contains_string, has_item, has_length
import pytest

from adgn.agent.approvals import ApprovalPolicyEngine, load_default_policy_source
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.mcp.approval_policy.server import (
    ApprovalPolicyAdminServer,
    ReloadPolicyArgs,
    ValidatePolicyArgs,
)
from adgn.mcp.testing.approval_policy_stubs import ApprovalPolicyAdminServerStub

pytestmark = pytest.mark.requires_docker


@pytest.fixture
async def engine(persistence: SQLitePersistence, docker_client: DockerClient):
    """Create engine for testing."""
    # Create agent
    agent_id = await persistence.create_agent(mcp_config=MCPConfig(), preset="test")

    # Create engine
    engine = ApprovalPolicyEngine(
        docker_client=docker_client,
        agent_id=agent_id,
        persistence=persistence,
        policy_source=load_default_policy_source(),
    )

    return engine


@pytest.fixture
async def admin_server(engine):
    """Create admin server for testing."""
    return ApprovalPolicyAdminServer(engine=engine)


@pytest.fixture
async def stub(admin_server):
    """Create stub for admin server with client session."""
    async with Client(admin_server) as session:
        yield ApprovalPolicyAdminServerStub.from_server(admin_server, session)


async def test_validate_policy_valid(stub, docker_client: DockerClient):
    """Test validating a valid policy."""
    result = await stub.validate_policy(ValidatePolicyArgs(source="print('hello')"))

    assert result.valid is True
    assert_that(result.errors, has_length(0))


async def test_validate_policy_syntax_error(stub):
    """Test validating a policy with syntax errors."""
    result = await stub.validate_policy(ValidatePolicyArgs(source="print('hello'"))

    assert result.valid is False
    assert_that(result.errors, has_item(contains_string("Syntax error")))


async def test_validate_policy_runtime_error(stub, docker_client: DockerClient):
    """Test validating a policy that fails at runtime."""
    result = await stub.validate_policy(ValidatePolicyArgs(source="import sys; sys.exit(1)"))

    assert result.valid is False
    assert_that(result.errors, has_item(contains_string("Runtime validation failed")))


async def test_reload_policy_from_persistence(engine, persistence, stub, docker_client: DockerClient):
    """Test reloading policy from persistence."""
    # Save a policy to persistence
    await persistence.set_policy(engine.agent_id, content="print('from persistence')")

    # Change engine's in-memory policy
    engine.set_policy("print('different')")

    # Reload from persistence
    await stub.reload_policy(ReloadPolicyArgs(source=None))

    # Engine should now have the persisted policy
    current_policy, _ = engine.get_policy()
    assert current_policy == "print('from persistence')"


async def test_reload_policy_from_source(engine, stub, docker_client: DockerClient):
    """Test reloading policy from provided source."""
    # Reload with provided source
    new_source = load_default_policy_source()
    await stub.reload_policy(ReloadPolicyArgs(source=new_source))

    # Engine should have the new source
    current_policy, _ = engine.get_policy()
    assert current_policy == new_source


async def test_reload_policy_validates_source(stub, docker_client: DockerClient):
    """Test that reload validates the source before setting."""
    # Try to reload with invalid source
    with pytest.raises(Exception):  # Should fail validation
        await stub.reload_policy(ReloadPolicyArgs(source="import sys; sys.exit(1)"))


async def test_reload_policy_no_persistence_raises(engine, persistence):
    """Test that reloading from empty persistence raises error."""
    # Create a new agent with no policy in persistence
    new_agent_id = await persistence.create_agent(mcp_config=MCPConfig(), preset="test")

    # Create new engine with no persisted policy
    new_engine = ApprovalPolicyEngine(
        docker_client=engine.docker_client,
        agent_id=new_agent_id,
        persistence=persistence,
        policy_source=load_default_policy_source(),
    )

    new_admin_server = ApprovalPolicyAdminServer(engine=new_engine)

    # Try to reload (should fail - no policy in persistence)
    async with Client(new_admin_server) as session:
        stub = ApprovalPolicyAdminServerStub.from_server(new_admin_server, session)
        with pytest.raises(ValueError, match="No policy found in persistence"):
            await stub.reload_policy(ReloadPolicyArgs(source=None))
