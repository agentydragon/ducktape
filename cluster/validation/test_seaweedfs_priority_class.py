"""Every pod-spawning SeaweedFS component opts into the `stateful-infra` PriorityClass.

The descheduler's eviction threshold is that class's priority (both rendered from
cluster/cdk8s/stateful_infra.py), so a component without the class is evictable: a
filer restart desynchronizes every FUSE client's cached chunk locations, after which
git dies of SIGBUS on mmap'd packfiles cluster-wide.
"""

from __future__ import annotations

from typing import Any

import pytest_bazel
import yaml

from cluster.cdk8s import stateful_infra
from util.bazel.runfiles import get_required_path

_SEAWEEDFS_CR = "_main/cluster/k8s/seaweedfs/cluster/seaweed.yaml"


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


def test_seaweedfs_components_carry_the_protected_priority_class() -> None:
    (seaweed,) = [
        doc for doc in yaml.safe_load_all(get_required_path(_SEAWEEDFS_CR).read_text()) if doc["kind"] == "Seaweed"
    ]
    components = _pod_spawning_components(seaweed["spec"])

    assert components, "found no pod-spawning components in the SeaweedFS CR"
    assert {path: component.get("priorityClassName") for path, component in components.items()} == dict.fromkeys(
        components, stateful_infra.NAME
    )


if __name__ == "__main__":
    pytest_bazel.main()
