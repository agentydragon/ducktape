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
import yaml

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def seaweedfs_policy() -> Path:
    return policy("pin-seaweedfs-ovh-consumers.yaml")


class TestPinSeaweedfsOvhConsumers:
    """Tests for the pin-seaweedfs-ovh-consumers ClusterPolicy."""

    @pytest.mark.parametrize("storage_class", ["seaweedfs-ovh", "seaweedfs-ovh-ssd"])
    def test_seaweedfs_ovh_pvc_gets_pinned(self, seaweedfs_policy: Path, storage_class: str):
        """Either seaweedfs-ovh* class adds the hil-ovh zone selector."""
        result = apply_policy(
            seaweedfs_policy, manifest("seaweedfs_pod_with_pvc.yaml"), set_vars={"storageClassName": storage_class}
        )
        assert result.ok, result.stdout
        pod = result.mutated_resources[0]
        assert pod["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "hil-ovh"}

    def test_unrelated_storage_class_untouched(self, seaweedfs_policy: Path):
        result = apply_policy(
            seaweedfs_policy, manifest("seaweedfs_pod_with_pvc.yaml"), set_vars={"storageClassName": "local-path-ovh"}
        )
        assert result.ok, result.stdout
        assert result.skipped == 1, f"Precondition should reject a non-seaweedfs-ovh* class\n{result.stdout}"
        assert "nodeSelector" not in result.mutated_resources[0]["spec"]

    def test_pod_without_any_pvc_volume_untouched(self, seaweedfs_policy: Path):
        """Regression test: `request.object.spec.volumes` is absent entirely (not just
        empty) here. Filtering it with `[?persistentVolumeClaim!=null]` directly -- without
        the `(... || `[]`)` guard the policy actually uses -- still runs the foreach body
        once and hits the disabled apiCall, failing with 'Unknown key "storageClassName"'
        instead of cleanly skipping.
        """
        result = apply_policy(seaweedfs_policy, manifest("seaweedfs_pod_no_volumes.yaml"))
        assert result.ok, result.stdout
        assert result.skipped == 1, f"No PVC volumes to iterate; rule should just skip\n{result.stdout}"

    def test_existing_node_selector_is_merged_not_clobbered(self, seaweedfs_policy: Path):
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

    def test_idempotent_under_reinvocation(self, seaweedfs_policy: Path, tmp_path: Path):
        """Kyverno's reinvocationPolicy: IfNeeded can re-run this policy within the same
        CREATE if a later-ordered webhook mutates the pod first (see inject-mitmproxy.yaml's
        header for the real outage this caused via RFC 6902 list appends). This policy uses
        patchStrategicMerge on a map instead, which a second pass should find already
        satisfied rather than duplicating or erroring.
        """
        first = apply_policy(
            seaweedfs_policy, manifest("seaweedfs_pod_with_pvc.yaml"), set_vars={"storageClassName": "seaweedfs-ovh"}
        )
        assert first.ok, first.stdout

        once = tmp_path / "pod-once.yaml"
        once.write_text(yaml.safe_dump(first.mutated_resources[0]))
        second = apply_policy(seaweedfs_policy, once, set_vars={"storageClassName": "seaweedfs-ovh"})

        assert second.ok, second.stdout
        assert second.mutated_resources[0]["spec"]["nodeSelector"] == first.mutated_resources[0]["spec"]["nodeSelector"]


if __name__ == "__main__":
    pytest_bazel.main()
