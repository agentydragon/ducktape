"""Pins DaemonSet `maxUnavailable` to the roaming-node count.

An offline roaming node's DaemonSet pod is deleted at rollout start and can never
terminate — there is no kubelet to complete deletion — so it holds the unavailable
budget permanently. If `maxUnavailable` is not greater than the number of roaming
nodes, the rollout deadlocks and every node silently keeps the old config while
Helm and Flux both report success.

Found 2026-07-31 with `maxUnavailable: 1` and two roaming nodes offline:
`0 out of 9 new pods have been updated`, for an hour, with no failing signal
anywhere. See cluster/docs/lessons_learned/2026_07_31_promtail_daemonset_roaming_deadlock.md.

This replaces the SYNC(roaming-node-count) comments' honour system with an actual
assertion, so adding a third laptop fails here instead of silently breaking the
next promtail rollout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path

# Roaming k8s nodes are exactly the `laptop`-role hosts in the mesh roster: they
# join and leave the cluster, unlike `wyrm2`, which is NixOS but always-on and so
# carries role `worker`. `non-k8s` hosts (atlas, pixel6) are not cluster nodes.
_ROAMING_ROLE = "laptop"

# HelmReleases rendering a DaemonSet that schedules onto roaming nodes. Add new
# ones here — a roaming DaemonSet missing from this list is unprotected, which is
# the one gap this test cannot close on its own.
_ROAMING_DAEMONSET_RELEASES = (
    "cluster/k8s/monitoring/loki/promtail-helmrelease.yaml",
    "cluster/k8s/monitoring/loki/promtail-journal-helmrelease.yaml",
)


@pytest.fixture(scope="module")
def roaming_node_count() -> int:
    mesh = nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))
    return sum(1 for host in mesh.hosts.values() if host.role == _ROAMING_ROLE)


def test_roaming_nodes_exist(roaming_node_count: int) -> None:
    """Guards the fixture itself: a schema change renaming the role would
    silently zero the count and make every assertion below vacuous."""
    assert roaming_node_count > 0, (
        f"no hosts with role={_ROAMING_ROLE!r} in nebula-mesh.json — if the roster "
        "schema changed, update _ROAMING_ROLE rather than deleting this test"
    )


@pytest.mark.parametrize("release_path", _ROAMING_DAEMONSET_RELEASES)
def test_max_unavailable_exceeds_roaming_nodes(release_path: str, roaming_node_count: int) -> None:
    doc = yaml.safe_load(Path(get_required_path(f"_main/{release_path}")).read_text())
    strategy = doc["spec"]["values"]["updateStrategy"]["rollingUpdate"]
    max_unavailable = strategy["maxUnavailable"]

    assert isinstance(max_unavailable, int), (
        f"{release_path}: maxUnavailable is {max_unavailable!r}; this test only "
        "reasons about integers. A percentage may well be correct — if you switch "
        "to one, extend this assertion rather than dropping it."
    )
    assert max_unavailable > roaming_node_count, (
        f"{release_path}: maxUnavailable={max_unavailable} does not exceed the "
        f"{roaming_node_count} roaming node(s) in nebula-mesh.json. With every "
        "roaming node offline, the rollout deadlocks and no node receives the new "
        "config — while Helm and Flux both report success."
    )


if __name__ == "__main__":
    pytest_bazel.main()
