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


def _normalize(surface: dict) -> dict:
    """Re-express a parsed surface (YAML or HCL) in agent_shared.yaml's shape. An
    mcp_toolset is keyed by its MCP server name, the built-in toolset by its type."""
    return {
        "model": surface["model"],
        "toolset_policies": {
            (t["mcp_server_name"] if t["type"] == "mcp_toolset" else t["type"]): t["default_config"][
                "permission_policy"
            ]["type"]
            for t in surface["tools"]
        },
        "mcp_urls": {s["name"]: s["url"] for s in surface["mcp_servers"]},
    }


@pytest.mark.parametrize("surface", _SURFACES.values(), ids=list(_SURFACES))
def test_surface_matches_ssot(surface):
    assert _normalize(surface) == _SHARED


if __name__ == "__main__":
    pytest_bazel.main()
