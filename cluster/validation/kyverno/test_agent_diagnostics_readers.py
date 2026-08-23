"""Tests for the label-driven agent diagnostics RoleBinding generators."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def agent_diagnostics_policy() -> Path:
    return policy("generate-agent-diagnostics-readers.yaml")


ALL_AGENT_SUBJECTS = [
    {"kind": "Group", "name": "oidc-ksbx-groups:haku", "apiGroup": "rbac.authorization.k8s.io"},
    {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
    {"kind": "ServiceAccount", "name": "haku-claude", "namespace": "haku-claude-sandbox"},
    {"kind": "Group", "name": "oidc-ksbx-groups:kubectl-sandbox-users", "apiGroup": "rbac.authorization.k8s.io"},
    {"kind": "ServiceAccount", "name": "public-coder-agent-reader", "namespace": "public-coder-agent"},
]


def bindings_by_name(result: Any) -> dict[str, dict[str, Any]]:
    return {
        resource["metadata"]["name"]: resource
        for resource in result.mutated_resources
        if resource.get("kind") == "RoleBinding"
    }


def test_agent_readable_metadata_label_grants_metadata(agent_diagnostics_policy: Path) -> None:
    result = apply_policy(agent_diagnostics_policy, manifest("namespace_agent_readable_metadata.yaml"))
    assert result.ok, result.stdout

    bindings = bindings_by_name(result)
    assert set(bindings) == {"agent-readable-metadata"}
    binding = bindings["agent-readable-metadata"]
    assert binding["metadata"]["namespace"] == "agent-readable-metadata-fixture"
    assert binding["metadata"]["labels"]["rbac.ducktape.io/access"] == "agent-readable-metadata"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "agent-readable-namespace-metadata",
    }
    assert binding["subjects"] == ALL_AGENT_SUBJECTS


def test_agent_readable_logs_label_grants_metadata_and_logs(agent_diagnostics_policy: Path) -> None:
    result = apply_policy(agent_diagnostics_policy, manifest("namespace_agent_readable_logs.yaml"))
    assert result.ok, result.stdout

    bindings = bindings_by_name(result)
    assert set(bindings) == {"agent-readable-metadata", "agent-readable-logs"}
    assert bindings["agent-readable-metadata"]["roleRef"]["name"] == "agent-readable-namespace-metadata"
    assert bindings["agent-readable-logs"]["roleRef"]["name"] == "agent-readable-namespace-logs"
    for binding in bindings.values():
        assert binding["metadata"]["namespace"] == "agent-readable-logs-fixture"
        assert binding["subjects"] == ALL_AGENT_SUBJECTS


def test_unlabeled_namespace_does_not_get_agent_reader(agent_diagnostics_policy: Path) -> None:
    result = apply_policy(agent_diagnostics_policy, manifest("namespace_without_agent_rbac.yaml"))
    assert result.ok, result.stdout
    assert not [resource for resource in result.mutated_resources if resource.get("kind") == "RoleBinding"]


if __name__ == "__main__":
    pytest_bazel.main()
