"""The parity comparison must notice scope, subresource and named-credential drift."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel

from cluster.validation.agent_rbac import Permission, Rbac, namespace_readers, uncovered
from cluster.validation.k8s import K8sResource, RbacRoleRef, parse_k8s_resources


def test_rule_coverage_is_not_role_name_equality() -> None:
    narrow = Permission("testing", "", "pods/log", "get")
    assert not uncovered({narrow}, {Permission(None, "*", "*", "*")})
    assert uncovered({Permission(None, "", "pods/log", "get")}, {narrow})
    assert uncovered({Permission("staging", "", "pods/log", "get")}, {narrow})
    assert uncovered({narrow}, {Permission("testing", "", "pods", "get")})
    assert uncovered({Permission("testing", "", "pods/exec", "create")}, {narrow})
    assert not uncovered({narrow}, {Permission("testing", "", "*/log", "get")})
    # Kubernetes supports */subresource, NOT the shell-style pods/* pattern.
    assert uncovered({narrow}, {Permission("testing", "", "pods/*", "get")})


def test_resource_names_do_not_mean_unrestricted_secret_access() -> None:
    named = Permission("testing", "", "secrets", "get", "testing-login")
    for forbidden in (
        Permission("testing", "", "secrets", "get"),
        Permission("testing", "", "secrets", "get", "staging-login"),
        Permission("testing", "", "secrets", "list"),
    ):
        assert uncovered({forbidden}, {named})
    assert not uncovered({named}, {Permission("testing", "", "secrets", "get")})


def test_non_resource_url_prefixes() -> None:
    prefix = Permission(None, "", "", "get", url="/healthz/*")
    assert prefix.covers(Permission(None, "", "", "get", url="/healthz/ready"))
    assert not prefix.covers(Permission(None, "", "", "get", url="/healthz"))
    assert not prefix.covers(Permission(None, "", "", "post", url="/healthz/ready"))
    assert not prefix.covers(Permission(None, "", "pods", "get"))


def test_binding_scope_and_subjects_are_resolved_separately() -> None:
    rbac = Rbac(
        parse_k8s_resources(
            [
                {
                    "kind": "ClusterRole",
                    "metadata": {"name": "reader"},
                    "rules": [
                        {"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list"]},
                        {"nonResourceURLs": ["/healthz"], "verbs": ["get"]},
                    ],
                },
                {
                    "kind": "RoleBinding",
                    "metadata": {"name": "read", "namespace": "testing"},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "reader"},
                    "subjects": [{"kind": "Group", "name": "console"}],
                },
            ]
        )
    )
    console = rbac.identity("Group", "console")
    assert len(console) == 4
    assert not rbac.identity("Group", "oidc"), "Do not union separate agent access paths"
    assert Permission("testing", "", "pods/log", "get") in console
    assert not any(p.url for p in console)
    with pytest.raises(KeyError):
        rbac.rules(RbacRoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="reader"), "testing")


def test_managed_profiles_include_standing_serviceaccount_group_bindings() -> None:
    rbac = Rbac(
        parse_k8s_resources(
            [
                {
                    "kind": "Role",
                    "metadata": {"name": "reader", "namespace": "testing"},
                    "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}],
                },
                {
                    "kind": "RoleBinding",
                    "metadata": {"name": "group-read", "namespace": "testing"},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "reader"},
                    "subjects": [{"kind": "Group", "name": "system:serviceaccounts:testing"}],
                },
            ]
        )
    )
    config: dict[str, Any] = {"sandbox_presets": {"public": {"kubernetes_grants": []}}, "kubernetes_grants": {}}
    assert rbac.managed(config, "public", namespace="testing") == {Permission("testing", "", "pods", "get")}
    assert not rbac.managed(config, "public", namespace="staging")


def test_aggregation_is_not_silently_ignored() -> None:
    rbac = Rbac(
        parse_k8s_resources(
            [
                {
                    "kind": "ClusterRole",
                    "metadata": {"name": "aggregate"},
                    "aggregationRule": {"clusterRoleSelectors": []},
                }
            ]
        )
    )
    with pytest.raises(AssertionError, match="aggregated"):
        rbac.rules(RbacRoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="aggregate"), None)


def test_kyverno_expands_actual_subjects_and_labels_and_rejects_unhandled_conditions() -> None:
    policy: dict[str, Any] = {
        "metadata": {"name": "generate-agent-diagnostics-readers"},
        "spec": {
            "rules": [
                {
                    "name": "logs",
                    "match": {
                        "any": [{"resources": {"kinds": ["Namespace"], "selector": {"matchLabels": {"logs": "true"}}}}]
                    },
                    "generate": {
                        "kind": "RoleBinding",
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "name": "logs",
                        "namespace": "{{request.object.metadata.name}}",
                        "generateExisting": True,
                        "synchronize": True,
                        "data": {
                            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "logs"},
                            "subjects": [{"kind": "Group", "name": "console"}],
                        },
                    },
                }
            ]
        },
    }
    namespaces = [
        K8sResource.model_validate({"kind": "Namespace", "metadata": {"name": name, "labels": labels}})
        for name, labels in (("approved", {"logs": "true"}), ("metadata-only", {"metadata": "true"}))
    ]
    bindings = namespace_readers(policy, namespaces)
    assert [b.namespace for b in bindings] == ["approved"]
    assert bindings[0].subjects[0].name == "console"
    policy["spec"]["rules"][0]["exclude"] = {"any": []}
    with pytest.raises(AssertionError):
        namespace_readers(policy, namespaces)


if __name__ == "__main__":
    pytest_bazel.main()
