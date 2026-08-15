"""Tests for the label-driven agent diagnostics RoleBinding generators."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def agent_diagnostics_policy() -> Path:
    return policy("generate-agent-diagnostics-readers.yaml")


AGENT_SUBJECTS = [
    {"kind": "Group", "name": "oidc-ksbx-groups:haku", "apiGroup": "rbac.authorization.k8s.io"},
    {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
    {"kind": "ServiceAccount", "name": "haku-claude", "namespace": "haku-claude-sandbox"},
    {"kind": "Group", "name": "oidc-ksbx-groups:kubectl-sandbox-users", "apiGroup": "rbac.authorization.k8s.io"},
]


def test_namespace_diagnostics_labeled_namespace_gets_shared_reader(agent_diagnostics_policy: Path) -> None:
    result = apply_policy(agent_diagnostics_policy, manifest("namespace_agent_namespace_diagnostics.yaml"))
    assert result.ok, result.stdout

    [binding] = [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]
    assert binding["metadata"]["name"] == "agent-namespace-diagnostics-reader"
    assert binding["metadata"]["namespace"] == "agent-namespace-diagnostics-fixture"
    assert binding["roleRef"]["name"] == "namespace-diagnostics-reader"
    assert binding["subjects"] == AGENT_SUBJECTS


def test_unlabeled_namespace_does_not_get_agent_reader(agent_diagnostics_policy: Path) -> None:
    result = apply_policy(agent_diagnostics_policy, manifest("namespace_without_agent_rbac.yaml"))
    assert result.ok, result.stdout
    assert not [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]


if __name__ == "__main__":
    pytest_bazel.main()
