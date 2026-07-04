"""SSOT guard: both Haku managed-agent surfaces — cloud (TF/HCL) and self-hosted
(`ant` YAML) — must match haku/base/agent_shared.yaml on model + full toolset.
Matching a shared SSOT is what keeps the two identical ("one brain, two substrates").

Both surfaces are parsed structurally — the self-hosted YAML with PyYAML, the
cloud HCL with pygohcl (HashiCorp's parser) — not by regex over the text. Edit
agent_shared.yaml, then update both surfaces, or this fails.
"""

import pygohcl
import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_SHARED = yaml.safe_load(get_required_path("_main/haku/base/agent_shared.yaml").read_text())
_SELF_HOSTED = yaml.safe_load(
    get_required_path("_main/haku/runtime/managed_agent/self_hosted/haku.agent.yaml").read_text()
)
_CLOUD = pygohcl.loads(get_required_path("_main/tf/gitops/haku-cloud-agent/main.tf").read_text())["resource"][
    "claude-managed-agents_agent"
]["haku_cloud"]

_SURFACES = {"self_hosted": _SELF_HOSTED, "cloud": _CLOUD}


def _mcp_urls(agent: dict) -> dict[str, str]:
    return {s["name"]: s["url"] for s in agent["mcp_servers"]}


def _toolset_policies(agent: dict) -> dict[str, str]:
    """mcp_server_name -> permission-policy type, for every mcp_toolset."""
    return {
        t["mcp_server_name"]: t["default_config"]["permission_policy"]["type"]
        for t in agent["tools"]
        if t["type"] == "mcp_toolset"
    }


def _builtin_toolsets(agent: dict) -> dict[str, str]:
    """non-mcp toolset type -> permission-policy type (e.g. agent_toolset_20260401)."""
    return {
        t["type"]: t["default_config"]["permission_policy"]["type"]
        for t in agent["tools"]
        if t["type"] != "mcp_toolset"
    }


@pytest.mark.parametrize("surface", _SURFACES.values(), ids=list(_SURFACES))
def test_surface_matches_ssot(surface):
    assert surface["model"] == _SHARED["model"]
    assert _mcp_urls(surface) == {n: s["url"] for n, s in _SHARED["mcp_servers"].items()}
    assert _toolset_policies(surface) == {n: s["toolset_permission_policy"] for n, s in _SHARED["mcp_servers"].items()}
    # The built-in agent_toolset is shared too (always_allow in both).
    assert _builtin_toolsets(surface) == {_SHARED["agent_toolset"]: "always_allow"}


if __name__ == "__main__":
    pytest_bazel.main()
