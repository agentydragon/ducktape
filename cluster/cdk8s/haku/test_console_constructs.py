"""The deployed console deliberately runs no Recall integration (cluster/k8s/haku/console/
README.md): no index catalog, no `haku_index` server or profile grant, no embedder. The
schema and role stay while indexing is unwired; re-enabling it is one reviewed change."""

from __future__ import annotations

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.haku import console_constructs
from haku.console.settings import Settings
from util.settings_contract import env_name


def test_recall_stays_unwired(haku_console_manifests: dict[str, list[dict[str, Any]]]) -> None:
    objects = haku_console_manifests["haku-console"]
    config = yaml.safe_load(
        one(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "haku-console-config")["data"][
            "config.yaml"
        ]
    )
    assert "recall_indexes" not in config
    assert "haku_index" not in config["mcp"]["servers"]
    assert all("haku_index" not in policy.get("tools", {}) for policy in config["auto_approval_policies"])
    for profile in config["access_profiles"]:
        assert not profile.get("recall_index_ids")
        assert "haku_index" not in profile.get("in_process_server_ids", [])
    deployment = one(
        o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == console_constructs.NAME
    )
    server = one(deployment["spec"]["template"]["spec"]["containers"])
    embedder = f"{env_name(Settings, 'embedder')}__"
    assert not any(entry["name"].startswith(embedder) for entry in server["env"])


if __name__ == "__main__":
    pytest_bazel.main()
