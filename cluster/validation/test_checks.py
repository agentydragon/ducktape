"""Tests for non-graph validation checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
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
    k8s_dir: Path, supplier_docs: list[dict], consumer_docs: list[dict], consumer_depends_on_supplier: bool = True
) -> ParsedCluster:
    consumer_dependencies = [DependsOn(name="external-creds")] if consumer_depends_on_supplier else []
    return ParsedCluster(
        flux_kustomizations={
            "claude-rbac": FluxKustomizationSpec(path="./cluster/k8s/claude-rbac"),
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
        "subjects": [{"kind": "ServiceAccount", "name": "credential-reader", "namespace": "consumer"}],
    }


def _consumer_store(service_account_namespace: str | None = None) -> dict:
    service_account = {"name": "credential-reader"}
    if service_account_namespace is not None:
        service_account["namespace"] = service_account_namespace
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "SecretStore",
        "metadata": {"name": "credential", "namespace": "consumer"},
        "spec": {
            "provider": {
                "kubernetes": {"auth": {"serviceAccount": service_account}, "remoteNamespace": "ducktape-flux"}
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


def test_external_credential_supplier_and_consumer_split_passes(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path,
        [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "credential", "namespace": "ducktape-flux"}},
            _source_role(),
            _source_binding(),
        ],
        [_consumer_store()],
    )
    assert check_external_credential_ownership(cluster, tmp_path) == []


def test_external_credential_supplier_rejects_consumer_store(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(tmp_path, [_source_role(), _source_binding(), _consumer_store()], [])
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("consumer-owned SecretStore" in error for error in errors)


def test_external_credential_store_cannot_borrow_cross_namespace_identity(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path, [_source_role(), _source_binding()], [_consumer_store(service_account_namespace="approved")]
    )
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("must omit the ServiceAccount namespace" in error for error in errors)


def test_external_credential_store_requires_supplier_dependency(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(
        tmp_path, [_source_role(), _source_binding()], [_consumer_store()], consumer_depends_on_supplier=False
    )
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("does not depend on external-creds" in error for error in errors)


if __name__ == "__main__":
    pytest_bazel.main()
