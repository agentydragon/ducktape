"""Tests for the label-driven Haku diagnostics RoleBinding generators."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def haku_diagnostics_policy() -> Path:
    return policy("generate-haku-diagnostics-readers.yaml")


def test_logs_labeled_namespace_gets_haku_reader(haku_diagnostics_policy: Path) -> None:
    result = apply_policy(haku_diagnostics_policy, manifest("namespace_haku_logs.yaml"))
    assert result.ok, result.stdout

    [binding] = [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]
    assert binding["metadata"]["name"] == "haku-logs-configmaps-reader"
    assert binding["metadata"]["namespace"] == "haku-logs-fixture"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "logs-configmaps-reader",
    }
    assert binding["subjects"] == [
        {"kind": "Group", "name": "oidc-ksbx-groups:haku", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
        {"kind": "ServiceAccount", "name": "haku-claude", "namespace": "haku-claude-sandbox"},
    ]


def test_namespace_diagnostics_labeled_namespace_gets_haku_reader(
    haku_diagnostics_policy: Path,
) -> None:
    result = apply_policy(
        haku_diagnostics_policy,
        manifest("namespace_haku_namespace_diagnostics.yaml"),
    )
    assert result.ok, result.stdout

    [binding] = [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]
    assert binding["metadata"]["name"] == "haku-namespace-diagnostics-reader"
    assert binding["metadata"]["namespace"] == "haku-namespace-diagnostics-fixture"
    assert binding["roleRef"]["name"] == "namespace-diagnostics-reader"
    assert binding["subjects"] == [
        {"kind": "Group", "name": "oidc-ksbx-groups:haku", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
        {"kind": "ServiceAccount", "name": "haku-claude", "namespace": "haku-claude-sandbox"},
    ]


def test_unlabeled_namespace_does_not_get_haku_reader(haku_diagnostics_policy: Path) -> None:
    result = apply_policy(haku_diagnostics_policy, manifest("namespace_without_haku_logs.yaml"))
    assert result.ok, result.stdout
    assert not [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]


if __name__ == "__main__":
    pytest_bazel.main()
