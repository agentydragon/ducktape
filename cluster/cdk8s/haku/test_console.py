"""The deployed console deliberately runs no Recall integration (cluster/k8s/haku/console/
README.md): no index catalog, no `haku_index` server or profile grant, no embedder. The
schema and role stay while indexing is unwired; re-enabling it is one reviewed change."""

from __future__ import annotations

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.haku import console
from haku.console.settings import Settings
from util.settings_contract import env_name


def test_recall_stays_unwired(haku_console_manifests: list[dict[str, Any]]) -> None:
    objects = haku_console_manifests
    config = yaml.safe_load(
        one(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "config")["data"]["config.yaml"]
    )
    assert "recall_indexes" not in config
    assert "haku_index" not in config["mcp"]["servers"]
    assert all("haku_index" not in policy.get("tools", {}) for policy in config["auto_approval_policies"])
    for profile in config["access_profiles"]:
        assert not profile.get("recall_index_ids")
        assert "haku_index" not in profile.get("in_process_server_ids", [])
    deployment = one(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == console.NAME)
    server = one(deployment["spec"]["template"]["spec"]["containers"])
    embedder = f"{env_name(Settings, 'embedder')}__"
    assert not any(entry["name"].startswith(embedder) for entry in server["env"])


def test_sandbox_stays_unwired(haku_console_manifests: list[dict[str, Any]]) -> None:
    """Retired Sandbox MCP tools have no deployed server, profile grant, or approval path."""
    objects = haku_console_manifests
    config = yaml.safe_load(
        one(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "config")["data"]["config.yaml"]
    )
    assert "agent_sandbox" not in config
    assert "sandbox" not in config["mcp"]["servers"]
    for profile in config["access_profiles"]:
        assert "sandbox" not in profile.get("in_process_server_ids", [])
    policy_ids = {policy["id"] for policy in config["auto_approval_policies"]}
    assert "haku_sandbox_control" not in policy_ids
    for policy in config["auto_approval_policies"]:
        assert "sandbox" not in policy.get("tools", {})
        assert "haku_sandbox_control" not in policy.get("policies", [])


def test_deployment_disables_console_mcp_endpoint(haku_console_manifests: list[dict[str, Any]]) -> None:
    objects = haku_console_manifests
    config = yaml.safe_load(
        one(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "config")["data"]["config.yaml"]
    )
    assert config["mcp_server_enabled"] is False


if __name__ == "__main__":
    pytest_bazel.main()
