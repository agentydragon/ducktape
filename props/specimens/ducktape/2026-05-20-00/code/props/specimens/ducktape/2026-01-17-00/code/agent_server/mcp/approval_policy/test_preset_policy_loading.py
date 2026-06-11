"""MCP tests for preset policy loading.

Tests that policy is correctly loaded from presets and accessible via MCP resources.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig

from agent_server.presets import create_agent_from_preset, discover_presets
from agent_server.testing.approval_policy_testdata import fetch_policy
from mcp_utils.resources import extract_single_text_content


class TestPresetPolicyDiscovery:
    """Tests for preset policy discovery."""

    def test_discover_presets_includes_default(self):
        """discover_presets always includes default preset."""
        presets = discover_presets()
        assert "default" in presets
        assert presets["default"].name == "default"

    def test_discover_presets_from_directory(self, tmp_path: Path):
        """discover_presets loads from specified directory."""
        # Create a test preset file
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


@pytest.mark.requires_docker
class TestPresetPolicyLoading:
    """Tests that preset policies are correctly loaded into PolicyEngine."""

    async def test_policy_engine_uses_provided_source(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine exposes the provided policy_source via resource."""
        engine = await make_approval_policy_server(policy_allow_all)

        async with Client(engine.reader) as sess:
            # Read the policy resource
            result = await sess.read_resource(engine.reader.active_policy_resource.uri)
            policy_text = extract_single_text_content(result)
            # Should contain the allow_all policy
            assert "approve_all" in policy_text.lower() or "allow" in policy_text.lower()

    async def test_policy_engine_returns_custom_policy(self, make_approval_policy_server):
        """PolicyEngine correctly returns custom policy source."""
        custom_policy = fetch_policy("const")

        engine = await make_approval_policy_server(custom_policy)

        async with Client(engine.reader) as sess:
            result = await sess.read_resource(engine.reader.active_policy_resource.uri)
            policy_text = extract_single_text_content(result)
            # const policy should be returned
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

        # Set a new valid policy (self_check runs, needs valid policy)
        new_version = engine.set_policy(policy_allow_all)

        assert engine.get_policy() == policy_allow_all
        assert new_version == 2
        assert engine._policy_version == 2

    async def test_load_policy_sets_without_incrementing(self, make_approval_policy_server, policy_allow_all):
        """PolicyEngine.load_policy sets policy at specific version (hydration)."""
        engine = await make_approval_policy_server("# initial")

        # load_policy is for hydration from persistence
        engine.load_policy(policy_allow_all, version=42)

        assert engine.get_policy() == policy_allow_all
        assert engine._policy_version == 42


class TestPresetPolicyResolution:
    """Tests for preset policy resolution logic."""

    async def test_agent_metadata_records_preset(self, sqlite_persistence):
        """Creating agent from preset records preset name in metadata."""
        agent_id, _config, _system = await create_agent_from_preset(
            persistence=sqlite_persistence, preset_name="default", base_mcp_config=MCPConfig(mcpServers={})
        )

        # Check that metadata has preset recorded
        row = await sqlite_persistence.get_agent(agent_id)
        assert row is not None
        assert row.metadata is not None
        assert row.metadata.preset == "default"
