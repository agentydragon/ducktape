"""Tests for the inject-mitmproxy Kyverno policy.

Snapshot-backed: the interesting output is a whole injected sidecar plus volumes,
so spelling every field out inline would obscure rather than clarify. The class
and method names below are the snapshot keys in
`__snapshots__/test_inject_mitmproxy.ambr` — renaming either orphans a snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml
from syrupy.assertion import SnapshotAssertion

from cluster.validation.kyverno.apply import KyvernoApplyResult, apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def mitmproxy_policy() -> Path:
    return policy("inject-mitmproxy.yaml")


def _mutated_yaml(result: KyvernoApplyResult) -> str:
    """Serialize mutated resources to YAML for snapshot comparison."""
    return yaml.dump_all(result.mutated_resources, default_flow_style=False, sort_keys=True)


class TestInjectMitmproxyProxy:
    """Tests for the inject-mitmproxy-proxy ClusterPolicy."""

    def test_pod_without_init_containers(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """init-container rule is skipped when pod has no initContainers."""
        result = apply_policy(mitmproxy_policy, manifest("pod_no_init_containers.yaml"))
        assert result.ok
        assert result.passed == 2, f"Expected volume + container rules to pass\n{result.stdout}"
        assert result.skipped == 1, f"Expected init-container rule to be skipped\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_with_init_containers(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """All three rules apply when pod has initContainers."""
        result = apply_policy(mitmproxy_policy, manifest("pod_with_init_containers.yaml"))
        assert result.ok
        assert result.passed == 3, f"Expected all three rules to pass\n{result.stdout}"
        assert result.skipped == 0, f"Expected no rules skipped\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_in_other_namespace_not_mutated(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """Policy does not match pods outside target namespaces."""
        result = apply_policy(mitmproxy_policy, manifest("pod_other_namespace.yaml"))
        assert result.ok
        assert result.passed == 0, f"Expected no rules to match\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_with_existing_volumes(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """Injected volume is appended, not merged into existing volumes.

        Regression test: patchStrategicMerge caused Kyverno autogen to merge
        the mitmproxy-ca-cert configMap into existing volume entries, producing
        invalid volumes with two types. JSON patches avoid this by appending.
        """
        result = apply_policy(mitmproxy_policy, manifest("pod_with_existing_volumes.yaml"))
        assert result.ok
        assert _mutated_yaml(result) == snapshot
        # Structural check: each volume has exactly one type (no merge corruption)
        for vol in result.mutated_resources[0]["spec"]["volumes"]:
            volume_types = [k for k in vol if k != "name"]
            assert len(volume_types) == 1, f"Volume {vol['name']!r} has multiple types: {volume_types}"


if __name__ == "__main__":
    pytest_bazel.main()
