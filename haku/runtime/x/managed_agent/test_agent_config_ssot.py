"""SSOT guard: both parked Haku managed-agent surfaces — cloud (TF/HCL) and self-hosted
(`ant` YAML) — must match agent_shared.yaml on model + full toolset.
Matching a shared SSOT is what keeps the two identical ("one brain, two substrates").

Both surfaces are parsed structurally — the self-hosted YAML with PyYAML, the
cloud HCL with pygohcl (HashiCorp's parser) — not by regex over the text. Edit
agent_shared.yaml, then update both surfaces, or this fails.
"""

import pygohcl
import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles


@pytest.fixture(scope="module")
def surfaces(runfiles: Runfiles) -> tuple[dict, dict[str, dict]]:
    root = runfiles.root()
    shared = yaml.safe_load((root / "ducktape/haku/runtime/x/managed_agent/agent_shared.yaml").read_text())
    self_hosted = yaml.safe_load(
        (root / "ducktape/haku/runtime/x/managed_agent/self_hosted/haku.agent.yaml").read_text()
    )
    cloud = pygohcl.loads(
        (root / "ducktape/haku/runtime/x/managed_agent/anthropic_hosted/terraform/main.tf").read_text()
    )["resource"]["claude-managed-agents_agent"]["haku_cloud"]
    return shared, {"self_hosted": self_hosted, "cloud": cloud}


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


@pytest.mark.parametrize("surface_name", ["self_hosted", "cloud"])
def test_surface_matches_ssot(surface_name: str, surfaces: tuple[dict, dict[str, dict]]) -> None:
    shared, by_name = surfaces
    assert _normalize(by_name[surface_name]) == shared


if __name__ == "__main__":
    pytest_bazel.main()
