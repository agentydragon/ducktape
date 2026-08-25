"""The descheduler must not be able to evict the SeaweedFS storage layer.

A SeaweedFS filer restart desynchronizes every FUSE client's cached chunk
locations, after which git dies of SIGBUS on mmap'd packfiles cluster-wide.
The only lever that makes those pods ineligible for eviction is
`DefaultEvictor.priorityThreshold`, which must sit at or below the priority the
storage pods actually carry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_DESCHEDULER_HELMRELEASE = "_main/cluster/k8s/descheduler/helmrelease.yaml"
_SEAWEEDFS_PRIORITYCLASS = "_main/cluster/k8s/seaweedfs/cluster/priorityclass.yaml"
_SEAWEEDFS_CR = "_main/cluster/k8s/seaweedfs/cluster/seaweed.yaml"


def _load_docs(runfile: str) -> list[dict[str, Any]]:
    path: Path = get_required_path(runfile)
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _one(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [doc for doc in docs if doc.get("kind") == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _plugin_args(helmrelease: dict[str, Any], plugin: str) -> dict[str, Any]:
    profiles = helmrelease["spec"]["values"]["deschedulerPolicy"]["profiles"]
    configs: list[dict[str, Any]] = [
        config for profile in profiles for config in profile["pluginConfig"] if config["name"] == plugin
    ]
    assert len(configs) == 1, f"expected exactly one {plugin} pluginConfig, got {len(configs)}"
    args: dict[str, Any] = configs[0]["args"]
    return args


def _pod_spawning_components(node: Any, path: str = "") -> dict[str, dict[str, Any]]:
    """Every SeaweedFS CR component that spawns pods, keyed by its dotted path.

    Components nest unevenly — the volume servers sit under `volumeTopology` —
    so recurse rather than assuming a flat `spec`. `replicas` is what marks a
    node as a workload rather than a config block.
    """
    if not isinstance(node, dict):
        return {}
    if "replicas" in node:
        return {path: node}
    found: dict[str, dict[str, Any]] = {}
    for key, value in node.items():
        found |= _pod_spawning_components(value, f"{path}.{key}" if path else key)
    return found


def test_storage_pods_are_above_the_descheduler_eviction_threshold() -> None:
    threshold = _plugin_args(_one(_load_docs(_DESCHEDULER_HELMRELEASE), "HelmRelease"), "DefaultEvictor")[
        "priorityThreshold"
    ]["value"]
    storage_priority = _one(_load_docs(_SEAWEEDFS_PRIORITYCLASS), "PriorityClass")["value"]

    assert storage_priority >= threshold, (
        f"SeaweedFS pods run at priority {storage_priority}, below the descheduler's "
        f"eviction threshold of {threshold}, so the descheduler may evict them"
    )


def test_seaweedfs_components_carry_the_protected_priority_class() -> None:
    """A threshold protects nothing if the pods do not opt into the class."""
    protected = _one(_load_docs(_SEAWEEDFS_PRIORITYCLASS), "PriorityClass")["metadata"]["name"]
    components = _pod_spawning_components(_one(_load_docs(_SEAWEEDFS_CR), "Seaweed")["spec"])

    assert components, "found no pod-spawning components in the SeaweedFS CR"
    assert {path: component.get("priorityClassName") for path, component in components.items()} == dict.fromkeys(
        components, protected
    )


if __name__ == "__main__":
    pytest_bazel.main()
