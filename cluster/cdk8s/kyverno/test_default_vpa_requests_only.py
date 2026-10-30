"""Tests for the default-vpa-requests-only Kyverno policy.

Goldilocks VPA scales limits in proportion to requests, so a low steady-state
recommendation drags the limit down with it. On 2026-08-09 that admitted both
LiteLLM replicas at a 150m CPU limit against a declared 1; cold start could not
finish inside the startup probe budget and both crashlooped for six hours. The
policy defaults every auto-mode namespace to controlledValues: RequestsOnly so
VPA never sets a limit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.test_utils import manifest, policy
from cluster.cdk8s.namespaces import VPA_RESOURCE_POLICY_ANNOTATION


@pytest.fixture
def vpa_policy(scratch_app: App, tmp_path: Path) -> Path:
    return policy(scratch_app, "default-vpa-requests-only", tmp_path)


def _declared_policy(ns: dict) -> dict | None:
    raw = (ns["metadata"].get("annotations") or {}).get(VPA_RESOURCE_POLICY_ANNOTATION)
    return json.loads(raw) if raw else None


class TestPolicyBehaviour:
    def test_auto_namespace_gets_requests_only(self, vpa_policy: Path):
        result = apply_policy(vpa_policy, manifest("namespace_vpa_auto.yaml"))
        assert result.ok, result.stdout
        [ns] = result.mutated_resources
        declared = _declared_policy(ns)
        assert declared is not None, f"auto-mode namespace was not annotated\n{result.stdout}"
        assert [c["controlledValues"] for c in declared["containerPolicies"]] == ["RequestsOnly"]
        assert [c["containerName"] for c in declared["containerPolicies"]] == ["*"]

    def test_own_policy_is_never_overwritten(self, vpa_policy: Path):
        """The +() anchor adds only when absent, so an explicit policy survives."""
        result = apply_policy(vpa_policy, manifest("namespace_vpa_auto_with_own_policy.yaml"))
        assert result.ok, result.stdout
        for ns in result.mutated_resources:
            declared = _declared_policy(ns)
            assert declared == {
                "containerPolicies": [{"containerName": "pinned", "controlledValues": "RequestsAndLimits"}]
            }, f"the namespace's own policy was modified\n{result.stdout}"

    def test_non_auto_namespace_untouched(self, vpa_policy: Path):
        """VPA never mutates running pods outside auto mode, so there is nothing to default."""
        result = apply_policy(vpa_policy, manifest("namespace_vpa_initial_mode.yaml"))
        assert result.ok, result.stdout
        for ns in result.mutated_resources:
            assert _declared_policy(ns) is None, f"non-auto namespace was annotated\n{result.stdout}"


if __name__ == "__main__":
    pytest_bazel.main()
