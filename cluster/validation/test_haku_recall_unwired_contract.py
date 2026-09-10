"""Contract for the deliberately disabled Haku Console Recall integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml


def test_haku_recall_is_unwired_but_database_is_retained(k8s_dir: Path) -> None:
    console_dir = k8s_dir / "haku" / "console"
    config: dict[str, Any] = yaml.safe_load((console_dir / "config.yaml").read_text(encoding="utf-8"))

    assert "recall_indexes" not in config
    assert "haku_index" not in config["mcp"]["servers"]
    for policy in config["auto_approval_policies"]:
        assert "haku_index" not in policy.get("tools", {})
    for profile in config["access_profiles"]:
        assert not profile.get("recall_index_ids")
        assert "haku_index" not in profile.get("in_process_server_ids", [])

    deployment = yaml.safe_load((console_dir / "deployment.yaml").read_text(encoding="utf-8"))
    server = next(
        container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == "server"
    )
    assert not any(entry["name"].startswith("HAKU_CONSOLE__EMBEDDER__") for entry in server["env"])

    assert not list(console_dir.glob("indexer-chunk-*-deployment.yaml"))
    assert not list(console_dir.glob("indexer-embed-deployment.yaml"))
    assert not list(console_dir.glob("indexer-chunk-*-config.yaml"))
    kustomization = yaml.safe_load((console_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    assert not any(
        "indexer-chunk" in resource or "indexer-embed" in resource for resource in kustomization["resources"]
    )
    assert not any("indexer-chunk" in generator["name"] for generator in kustomization["configMapGenerator"])

    # The data-bearing database remains declaratively managed; this PR only removes its Recall
    # readers and writers. A later migration can retire the schema after an explicit data decision.
    database_kustomization = yaml.safe_load((console_dir / "db" / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "approval-store-database.yaml" in database_kustomization["resources"]
    assert "postgres-cluster.yaml" in database_kustomization["resources"]


if __name__ == "__main__":
    pytest_bazel.main()
