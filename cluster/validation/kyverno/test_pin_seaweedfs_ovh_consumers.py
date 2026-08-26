"""Tests for the pin-seaweedfs-ovh-consumers Kyverno policy.

`kyverno apply` unconditionally disables `context[].apiCall` loading outside `--cluster`
mode -- confirmed against the pinned CLI by pointing an apiCall at a PVC supplied as a
second `--resource` file: the lookup is never attempted (the engine logs "disabled loading
of APICall context entry" regardless), so the extra file has no effect. These tests instead
supply `storageClassName` -- the variable the policy's `context` entry binds the apiCall's
result to -- directly via `apply_policy`'s `set_vars` (`kyverno apply --set`), which stands
in for whatever a real PVC lookup would have returned. That exercises the policy's
precondition, `foreach`, and merge logic; the apiCall's `urlPath`/`jmesPath` extraction
itself is not covered here and needs a live cluster.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy, apply_twice, assert_not_mutated
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def seaweedfs_policy() -> Path:
    return policy("pin-seaweedfs-ovh-consumers.yaml")


@pytest.mark.parametrize("storage_class", ["seaweedfs-ovh", "seaweedfs-ovh-ssd"])
def test_seaweedfs_ovh_pvc_gets_pinned(seaweedfs_policy: Path, storage_class: str):
    """Either seaweedfs-ovh* class adds the hil-ovh zone selector."""
    result = apply_policy(
        seaweedfs_policy, manifest("seaweedfs_pod_with_pvc.yaml"), set_vars={"storageClassName": storage_class}
    )
    assert result.ok, result.stdout
    pod = result.mutated_resources[0]
    assert pod["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "hil-ovh"}


def test_unrelated_storage_class_untouched(seaweedfs_policy: Path):
    """Precondition should reject a non-seaweedfs-ovh* class."""
    assert_not_mutated(
        seaweedfs_policy, manifest("seaweedfs_pod_with_pvc.yaml"), set_vars={"storageClassName": "local-path-ovh"}
    )


def test_pod_without_any_pvc_volume_untouched(seaweedfs_policy: Path):
    """Regression test: `request.object.spec.volumes` is absent entirely (not just
    empty) here. Filtering it with `[?persistentVolumeClaim!=null]` directly -- without
    the `(... || `[]`)` guard the policy actually uses -- still runs the foreach body
    once and hits the disabled apiCall, failing with 'Unknown key "storageClassName"'
    instead of cleanly skipping.
    """
    assert_not_mutated(seaweedfs_policy, manifest("seaweedfs_pod_no_volumes.yaml"))


def test_existing_node_selector_is_merged_not_clobbered(seaweedfs_policy: Path):
    """codex/public-coder-agent hand-add topology.kubernetes.io/region: hil; this
    policy's zone key must land alongside it, not replace it.
    """
    result = apply_policy(
        seaweedfs_policy,
        manifest("seaweedfs_pod_existing_node_selector.yaml"),
        set_vars={"storageClassName": "seaweedfs-ovh"},
    )
    assert result.ok, result.stdout
    assert result.mutated_resources[0]["spec"]["nodeSelector"] == {
        "topology.kubernetes.io/region": "hil",
        "topology.kubernetes.io/zone": "hil-ovh",
    }


def test_idempotent_under_reinvocation(seaweedfs_policy: Path, tmp_path: Path):
    """patchStrategicMerge on a map (unlike the sibling inject-* policies' RFC 6902 list
    appends) means a second pass finds the key already satisfied instead of duplicating
    or erroring: the nodeSelector must come out identical, not merely present.
    """
    first, second = apply_twice(
        seaweedfs_policy,
        manifest("seaweedfs_pod_with_pvc.yaml"),
        tmp_path,
        set_vars={"storageClassName": "seaweedfs-ovh"},
    )
    assert second.mutated_resources[0]["spec"]["nodeSelector"] == first.mutated_resources[0]["spec"]["nodeSelector"]


if __name__ == "__main__":
    pytest_bazel.main()
