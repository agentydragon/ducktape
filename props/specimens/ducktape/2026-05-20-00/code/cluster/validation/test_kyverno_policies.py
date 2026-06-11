"""Tests for Kyverno policies using the kyverno CLI.

TODO: Add tests for require-gitops and restrict-agent-kustomization-patch policies.
These are validation (not mutation) policies with background: false that use
request.userInfo for admission context. Testing them requires passing --userinfo
flags or mock admission contexts to the kyverno CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml
from syrupy.assertion import SnapshotAssertion

from cluster.validation.kyverno import KyvernoApplyResult, apply_policy
from util.bazel.runfiles import get_required_path


def _testdata(name: str) -> Path:
    return get_required_path(f"_main/cluster/validation/testdata/kyverno/{name}")


def _policy_path(rel: str) -> Path:
    return get_required_path(f"_main/{rel}")


def _mutated_yaml(result: KyvernoApplyResult) -> str:
    """Serialize mutated resources to YAML for snapshot comparison."""
    return yaml.dump_all(result.mutated_resources, default_flow_style=False, sort_keys=True)


@pytest.fixture
def mitmproxy_policy() -> Path:
    return _policy_path("cluster/k8s/kyverno/policies/inject-mitmproxy.yaml")


class TestInjectMitmproxyProxy:
    """Tests for the inject-mitmproxy-proxy ClusterPolicy."""

    def test_pod_without_init_containers(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """init-container rule is skipped when pod has no initContainers."""
        result = apply_policy(mitmproxy_policy, _testdata("pod_no_init_containers.yaml"))
        assert result.ok
        assert result.passed == 2, f"Expected volume + container rules to pass\n{result.stdout}"
        assert result.skipped == 1, f"Expected init-container rule to be skipped\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_with_init_containers(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """All three rules apply when pod has initContainers."""
        result = apply_policy(mitmproxy_policy, _testdata("pod_with_init_containers.yaml"))
        assert result.ok
        assert result.passed == 3, f"Expected all three rules to pass\n{result.stdout}"
        assert result.skipped == 0, f"Expected no rules skipped\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_in_other_namespace_not_mutated(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """Policy does not match pods outside target namespaces."""
        result = apply_policy(mitmproxy_policy, _testdata("pod_other_namespace.yaml"))
        assert result.ok
        assert result.passed == 0, f"Expected no rules to match\n{result.stdout}"
        assert _mutated_yaml(result) == snapshot

    def test_pod_with_existing_volumes(self, mitmproxy_policy: Path, snapshot: SnapshotAssertion):
        """Injected volume is appended, not merged into existing volumes.

        Regression test: patchStrategicMerge caused Kyverno autogen to merge
        the mitmproxy-ca-cert configMap into existing volume entries, producing
        invalid volumes with two types. JSON patches avoid this by appending.
        """
        result = apply_policy(mitmproxy_policy, _testdata("pod_with_existing_volumes.yaml"))
        assert result.ok
        assert _mutated_yaml(result) == snapshot
        # Structural check: each volume has exactly one type (no merge corruption)
        for vol in result.mutated_resources[0]["spec"]["volumes"]:
            volume_types = [k for k in vol if k != "name"]
            assert len(volume_types) == 1, f"Volume {vol['name']!r} has multiple types: {volume_types}"


if __name__ == "__main__":
    pytest_bazel.main()
