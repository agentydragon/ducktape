"""Tests for PolicyEngine: validation, preset loading, and version management."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig

from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.presets import create_agent_from_preset, discover_presets
from agent_server.testing.approval_policy_testdata import fetch_policy
from mcp_utils.resources import extract_single_text_content


@pytest.fixture
async def policy_engine(sqlite_persistence, async_docker_client, runtime_image) -> PolicyEngine:
    """PolicyEngine instance for validation tests."""
    return PolicyEngine(
        agent_id="testagent",
        persistence=sqlite_persistence,
        policy_source="# placeholder",
        docker_client=async_docker_client,
    )


@pytest.fixture
def failing_policy() -> str:
    """Policy source with failing tests."""
    result: str = fetch_policy("failing_tests")
    return result


# -- Preset discovery (no docker) --


class TestPresetPolicyDiscovery:
    """Tests for preset policy discovery."""

    def test_discover_presets_includes_default(self):
        """discover_presets always includes default preset."""
        presets = discover_presets()
        assert "default" in presets
        assert presets["default"].name == "default"

    def test_discover_presets_from_directory(self, tmp_path: Path):
        """discover_presets loads from specified directory."""
        preset_file = tmp_path / "custom.yaml"
        preset_file.write_text("""
name: custom
description: A custom preset
approval_policy: |
    # Custom policy
    from agent_server.policies.policy_types import PolicyRequest, PolicyResponse, ApprovalDecision
    from agent_server.policies.scaffold import run

    def decide(req: PolicyRequest) -> PolicyResponse:
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="custom")

    if __name__ == "__main__":
        raise SystemExit(run(decide))
""")

        presets = discover_presets(override_dir=tmp_path)
        assert "custom" in presets
        assert presets["custom"].approval_policy is not None
        assert "custom" in presets["custom"].approval_policy.lower()


# -- Policy validation (docker) --


@pytest.mark.requires_docker
class TestPolicyValidation:
    """Tests for policy validation via MCP admin tools."""

    async def test_set_policy_rejects_failing_tests(self, policy_engine, failing_policy):
        """Setting policy with failing tests raises an error."""
        async with Client(policy_engine.admin) as sess:
            result = await sess.call_tool("set_policy", {"source": failing_policy}, raise_on_error=False)
            assert result.is_error, "Expected error for failing tests policy"

    async def test_set_policy_accepts_valid_policy(self, policy_engine, policy_allow_all):
        """Setting valid policy succeeds."""
        async with Client(policy_engine.admin) as sess:
            result = await sess.call_tool("set_policy", {"source": policy_allow_all})
            assert not result.is_error

    async def test_create_proposal_validates_policy(self, policy_engine, failing_policy):
        """Creating proposal with failing tests returns error."""
        async with Client(policy_engine.proposer) as sess:
            result = await sess.call_tool("create_proposal", {"content": failing_policy}, raise_on_error=False)
            assert result.is_error, "Expected error for policy with failing tests"

    async def test_self_check_directly(self, policy_engine, failing_policy):
        """PolicyEngine.self_check raises for invalid policy."""
        with pytest.raises(RuntimeError, match="policy eval failed"):
            await policy_engine.self_check(failing_policy)

    async def test_self_check_passes_valid(self, policy_engine, policy_allow_all):
        """PolicyEngine.self_check passes for valid policy."""
        await policy_engine.self_check(policy_allow_all)


# -- Preset loading and version management (docker) --


@pytest.mark.requires_docker
class TestPresetPolicyLoading:
    """Tests that preset policies are correctly loaded into PolicyEngine."""

    async def test_policy_engine_uses_provided_source(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine exposes the provided policy_source via resource."""
        engine = await make_approval_policy_server(policy_allow_all)

        async with Client(engine.reader) as sess:
            result = await sess.read_resource(engine.reader.active_policy_resource.uri)
            policy_text = extract_single_text_content(result)
            assert "approve_all" in policy_text.lower() or "allow" in policy_text.lower()

    async def test_policy_engine_returns_custom_policy(self, make_approval_policy_server):
        """PolicyEngine correctly returns custom policy source."""
        custom_policy = fetch_policy("const")

        engine = await make_approval_policy_server(custom_policy)

        async with Client(engine.reader) as sess:
            result = await sess.read_resource(engine.reader.active_policy_resource.uri)
            policy_text = extract_single_text_content(result)
            assert "const" in policy_text.lower() or "PolicyResponse" in policy_text

    async def test_get_policy_returns_source(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine.get_policy returns policy source."""
        engine = await make_approval_policy_server(policy_allow_all)

        source = engine.get_policy()
        assert source == policy_allow_all
        assert engine._policy_version == 1  # Initial version

    async def test_set_policy_increments_version(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine.set_policy increments version."""
        engine = await make_approval_policy_server("# initial")

        assert engine._policy_version == 1

        new_version = engine.set_policy(policy_allow_all)

        assert engine.get_policy() == policy_allow_all
        assert new_version == 2
        assert engine._policy_version == 2

    async def test_load_policy_sets_without_incrementing(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine.load_policy sets policy at specific version (hydration)."""
        engine = await make_approval_policy_server("# initial")

        engine.load_policy(policy_allow_all, version=42)

        assert engine.get_policy() == policy_allow_all
        assert engine._policy_version == 42


# -- Preset resolution --


class TestPresetPolicyResolution:
    """Tests for preset policy resolution logic."""

    async def test_agent_metadata_records_preset(self, sqlite_persistence):
        """Creating agent from preset records preset name in metadata."""
        agent_id, _config, _system = await create_agent_from_preset(
            persistence=sqlite_persistence, preset_name="default", base_mcp_config=MCPConfig(mcpServers={})
        )

        row = await sqlite_persistence.get_agent(agent_id)
        assert row is not None
        assert row.metadata is not None
        assert row.metadata.preset == "default"


if __name__ == "__main__":
    pytest_bazel.main()
