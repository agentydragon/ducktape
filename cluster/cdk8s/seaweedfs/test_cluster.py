"""Every pod-spawning SeaweedFS component opts into the `stateful-infra` PriorityClass.

The descheduler's eviction threshold is that class's priority (both rendered from
cluster/cdk8s/stateful_infra.py), so a component without the class is evictable: a
filer restart desynchronizes every FUSE client's cached chunk locations, after which
git dies of SIGBUS on mmap'd packfiles cluster-wide.
"""

from __future__ import annotations

from typing import Any

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import stateful_infra
from cluster.cdk8s.seaweedfs import cluster


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
    seaweed = one(doc for doc in Cdk8sTesting.synth(cluster.chart(Cdk8sTesting.app())) if doc["kind"] == "Seaweed")
    components = _pod_spawning_components(seaweed["spec"])

    assert components, "found no pod-spawning components in the SeaweedFS CR"
    assert {path: component.get("priorityClassName") for path, component in components.items()} == dict.fromkeys(
        components, stateful_infra.NAME
    )


if __name__ == "__main__":
    pytest_bazel.main()
