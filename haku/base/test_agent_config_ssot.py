"""SSOT drift guard: the Haku agent fields duplicated across the cloud (TF) and
self-hosted (`ant` YAML) surfaces must match haku/base/agent_shared.yaml.

Both surfaces are parsed structurally — the self-hosted YAML with PyYAML, the
cloud HCL with pygohcl (HashiCorp's parser) — not by regex over the text. Edit
agent_shared.yaml, then update both surfaces, or this fails.
"""

import pygohcl
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_SHARED = yaml.safe_load(
    get_required_path("_main/haku/base/agent_shared.yaml").read_text()
)
_SELF_HOSTED = yaml.safe_load(
    get_required_path(
        "_main/haku/runtime/managed_agent/self_hosted/haku.agent.yaml"
    ).read_text()
)
_CLOUD = pygohcl.loads(
    get_required_path("_main/tf/gitops/haku-cloud-agent/main.tf").read_text()
)["resource"]["claude-managed-agents_agent"]["haku_cloud"]


def _urls(agent: dict) -> dict[str, str]:
    return {s["name"]: s["url"] for s in agent["mcp_servers"]}


def _toolset_policies(agent: dict) -> dict[str, str]:
    return {
        t["mcp_server_name"]: t["default_config"]["permission_policy"]["type"]
        for t in agent["tools"]
        if t["type"] == "mcp_toolset"
    }


def test_model_matches_ssot():
    assert _SELF_HOSTED["model"] == _SHARED["model"]
    assert _CLOUD["model"] == _SHARED["model"]


def test_shared_mcp_servers_match_ssot():
    self_hosted_urls, cloud_urls = _urls(_SELF_HOSTED), _urls(_CLOUD)
    self_hosted_pol, cloud_pol = _toolset_policies(_SELF_HOSTED), _toolset_policies(_CLOUD)
    for name, spec in _SHARED["mcp_servers"].items():
        assert self_hosted_urls[name] == spec["url"]
        assert cloud_urls[name] == spec["url"]
        assert self_hosted_pol[name] == spec["toolset_permission_policy"]
        assert cloud_pol[name] == spec["toolset_permission_policy"]


if __name__ == "__main__":
    pytest_bazel.main()
