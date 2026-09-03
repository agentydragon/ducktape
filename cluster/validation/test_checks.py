"""Tests for non-graph validation checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_egress_bindings_name_granter,
    check_egress_bindings_resolve_policies,
    check_external_credential_ownership,
    check_forgejo_image_namespace_reflection,
)
from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import DependsOn, FluxKustomizationSpec
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult


def _cluster_with(doc: dict) -> ParsedCluster:
    return ParsedCluster(source_resources={Path("policy.yaml"): parse_k8s_resources([doc])})


def _external_creds_cluster(
    k8s_dir: Path,
    supplier_docs: list[dict],
    consumer_docs: list[dict],
    store_docs: list[dict] | None = None,
    consumer_depends_on_supplier: bool = True,
) -> ParsedCluster:
    consumer_dependencies = [DependsOn(name="external-secrets-config")]
    if consumer_depends_on_supplier:
        consumer_dependencies.append(DependsOn(name="external-creds"))
    return ParsedCluster(
        flux_kustomizations={
            "claude-rbac": FluxKustomizationSpec(path="./cluster/k8s/claude-rbac"),
            "external-secrets-config": FluxKustomizationSpec(path="./cluster/k8s/external-secrets/config"),
            "external-creds": FluxKustomizationSpec(
                path="./cluster/k8s/external-creds", depends_on=[DependsOn(name="claude-rbac")]
            ),
            "consumer": FluxKustomizationSpec(path="./cluster/k8s/consumer", depends_on=consumer_dependencies),
        },
        build_results=[
            KustomizeBuildResult(
                kustomization_path=k8s_dir / "external-creds/kustomization.yaml",
                resources=parse_k8s_resources(supplier_docs),
            ),
            KustomizeBuildResult(
                kustomization_path=k8s_dir / "external-secrets/config/kustomization.yaml",
                resources=parse_k8s_resources(store_docs or [_central_store()]),
            ),
            KustomizeBuildResult(
                kustomization_path=k8s_dir / "consumer/kustomization.yaml", resources=parse_k8s_resources(consumer_docs)
            ),
        ],
    )


def _source_role() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "credential-reader", "namespace": "ducktape-flux"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["credential"], "verbs": ["get"]}],
    }


def _source_binding() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "credential-consumer-reader", "namespace": "ducktape-flux"},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "credential-reader"},
        "subjects": [{"kind": "ServiceAccount", "name": "external-creds-reader", "namespace": "consumer"}],
    }


def _central_store(service_account_namespace: str | None = None, namespaces: list[str] | None = None) -> dict:
    service_account = {"name": "external-creds-reader"}
    if service_account_namespace is not None:
        service_account["namespace"] = service_account_namespace
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ClusterSecretStore",
        "metadata": {"name": "kubernetes-external-creds-secret-store"},
        "spec": {
            "conditions": [{"namespaces": namespaces or ["consumer"]}],
            "provider": {
                "kubernetes": {"auth": {"serviceAccount": service_account}, "remoteNamespace": "ducktape-flux"}
            },
        },
    }


def _consumer_external_secret() -> dict:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": "credential", "namespace": "consumer"},
        "spec": {"secretStoreRef": {"kind": "ClusterSecretStore", "name": "kubernetes-external-creds-secret-store"}},
    }


def _consumer_store() -> dict:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "SecretStore",
        "metadata": {"name": "credential", "namespace": "consumer"},
        "spec": {
            "provider": {
                "kubernetes": {
                    "auth": {"serviceAccount": {"name": "external-creds-reader"}},
                    "remoteNamespace": "ducktape-flux",
                }
            }
        },
    }


def _forgejo_secret(allowed: str, auto: str | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "forgejo-images-creds",
            "namespace": "forgejo-images",
            "annotations": {
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": allowed,
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": auto or allowed,
            },
        },
    }


def _forgejo_deployment(namespace: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "worker", "namespace": namespace},
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "worker", "image": "git.allegedly.works/ducktape-ci/worker:tag"}]}
            }
        },
    }


@pytest.mark.parametrize("kind", ["CiliumNetworkPolicy", "CiliumClusterwideNetworkPolicy"])
def test_all_empty_rule_sections_flagged(kind: str) -> None:
    """`ingress: []` is schema-valid but Cilium rejects the rule — the #4923 shape."""
    doc = {
        "apiVersion": "cilium.io/v2",
        "kind": kind,
        "metadata": {"name": "deny-all"},
        "spec": {"endpointSelector": {}, "ingress": []},
    }
    [error] = check_cilium_policy_rules_nonempty(_cluster_with(doc))
    assert "deny-all" in error


def test_empty_rule_element_passes() -> None:
    """`ingress: [{}]` is the valid default-deny spelling, not an empty section."""
    doc = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "deny-all"},
        "spec": {"endpointSelector": {}, "ingress": [{}]},
    }
    assert not check_cilium_policy_rules_nonempty(_cluster_with(doc))


def test_specs_rules_checked() -> None:
    """Rules under `specs` are held to the same requirement as `spec`."""
    doc = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "multi"},
        "specs": [{"endpointSelector": {}, "egress": []}],
    }
    [error] = check_cilium_policy_rules_nonempty(_cluster_with(doc))
    assert "multi" in error


def _egress_policy(namespace: str, name: str) -> dict:
    return {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "EgressPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"rules": [{"hosts": ["example.test"]}]},
    }


def _egress_binding(namespace: str, policies: list[str], granted_by: str | None = "flux") -> dict:
    labels = {"agentplane.allegedly.works/granted-by": granted_by} if granted_by else {}
    return {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "EgressBinding",
        "metadata": {"name": "binding", "namespace": namespace, "labels": labels},
        "spec": {"subjects": [{"sandbox": {"name": "box"}}], "policies": policies},
    }


def _egress_cluster(docs: list[dict]) -> ParsedCluster:
    return ParsedCluster(
        build_results=[
            KustomizeBuildResult(
                kustomization_path=Path("/k8s/egress/kustomization.yaml"), resources=parse_k8s_resources(docs)
            )
        ]
    )


def test_egress_binding_resolves_policies_in_its_namespace() -> None:
    cluster = _egress_cluster([_egress_policy("staging", "github"), _egress_binding("staging", ["github"])])
    assert check_egress_bindings_resolve_policies(cluster) == []


def test_egress_binding_policy_missing_or_in_other_namespace_is_flagged() -> None:
    # A policy of the right name in another namespace does not count.
    cluster = _egress_cluster(
        [_egress_policy("production", "github"), _egress_binding("staging", ["github", "gitlab"])]
    )
    errors = check_egress_bindings_resolve_policies(cluster)
    assert [error.split("'")[3] for error in errors] == ["github", "gitlab"]


def test_egress_binding_names_its_granter() -> None:
    assert check_egress_bindings_name_granter(_egress_cluster([_egress_binding("staging", ["github"])])) == []
    errors = check_egress_bindings_name_granter(
        _egress_cluster([_egress_binding("staging", ["github"], granted_by=None)])
    )
    assert len(errors) == 1


def test_forgejo_image_namespace_must_be_reflected() -> None:
    cluster = ParsedCluster(
        source_resources={
            Path("registry-creds.yaml"): parse_k8s_resources([_forgejo_secret("worker")]),
            Path("deployment.yaml"): parse_k8s_resources([_forgejo_deployment("worker")]),
        }
    )
    assert check_forgejo_image_namespace_reflection(cluster) == []


@pytest.mark.parametrize(
    "missing_annotation",
    [
        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces",
        "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces",
    ],
)
def test_forgejo_image_namespace_missing_from_either_reflector_list_is_flagged(missing_annotation: str) -> None:
    secret = _forgejo_secret("worker")
    secret["metadata"]["annotations"][missing_annotation] = "other"
    cluster = ParsedCluster(
        source_resources={
            Path("registry-creds.yaml"): parse_k8s_resources([secret]),
            Path("deployment.yaml"): parse_k8s_resources([_forgejo_deployment("worker")]),
        }
    )
    errors = check_forgejo_image_namespace_reflection(cluster)
    assert len(errors) == 1
    assert missing_annotation in errors[0]


def test_external_credential_central_store_and_source_approval_pass(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path,
        [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "credential", "namespace": "ducktape-flux"}},
            _source_role(),
            _source_binding(),
        ],
        [_consumer_external_secret()],
    )
    assert check_external_credential_ownership(cluster, tmp_path) == []


def test_external_credential_supplier_rejects_consumer_store(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(tmp_path, [_source_role(), _source_binding(), _central_store()], [])
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("consumer-owned ClusterSecretStore" in error for error in errors)


def test_external_credential_store_requires_referent_identity(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path,
        [_source_role(), _source_binding()],
        [_consumer_external_secret()],
        store_docs=[_central_store(service_account_namespace="approved")],
    )
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any(
        "must omit the ServiceAccount namespace so ESO uses referent authentication" in error for error in errors
    )


def test_external_credential_namespace_store_is_rejected(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(tmp_path, [_source_role(), _source_binding()], [_consumer_store()])
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("use external-secrets-config's shared ClusterSecretStore" in error for error in errors)


def test_external_credential_store_conditions_match_source_approvals(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path,
        [_source_role(), _source_binding()],
        [_consumer_external_secret()],
        store_docs=[_central_store(namespaces=["consumer", "unapproved"])],
    )
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("namespace conditions must equal the source-approved namespaces" in error for error in errors)


def test_external_credential_store_requires_supplier_dependency(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path, [_source_role(), _source_binding()], [_consumer_external_secret()], consumer_depends_on_supplier=False
    )
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("does not depend on external-creds" in error for error in errors)


if __name__ == "__main__":
    pytest_bazel.main()
