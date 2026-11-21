"""Tests for non-graph validation checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from cluster.validation.checks import (
    check_cilium_policy_rules_nonempty,
    check_external_credential_ownership,
    check_external_secret_store_wiring,
    check_forgejo_image_namespace_reflection,
    check_goldilocks_namespace_labels,
)
from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomizationSpec
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult


def _cluster_with(*docs: dict) -> ParsedCluster:
    return ParsedCluster(source_resources={Path("policy.yaml"): parse_k8s_resources(docs)})


def _external_creds_cluster(repo_root: Path, consumer_docs: list[dict]) -> ParsedCluster:
    return ParsedCluster(
        flux_kustomizations={
            "external-secrets-config": FluxKustomizationSpec(path="./cluster/k8s/external-secrets/config"),
            "consumer": FluxKustomizationSpec(path="./cluster/k8s/consumer"),
        },
        build_results=[
            KustomizeBuildResult(
                kustomization_path=repo_root / "cluster/k8s/external-secrets/config/kustomization.yaml",
                resources=parse_k8s_resources([_central_store()]),
            ),
            KustomizeBuildResult(
                kustomization_path=repo_root / "cluster/k8s/consumer/kustomization.yaml",
                resources=parse_k8s_resources(consumer_docs),
            ),
        ],
    )


def _central_store() -> dict:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ClusterSecretStore",
        "metadata": {"name": "kubernetes-external-creds-secret-store"},
        "spec": {"provider": {"kubernetes": {"remoteNamespace": "ducktape-flux"}}},
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
        "spec": {"provider": {"kubernetes": {"remoteNamespace": "ducktape-flux"}}},
    }


def _forgejo_creds_eso(namespace: str) -> dict:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": "forgejo-images-creds", "namespace": namespace},
        "spec": {"secretStoreRef": {"kind": "ClusterSecretStore", "name": "kubernetes-forgejo-images-secret-store"}},
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


def test_forgejo_image_namespace_has_own_external_secret() -> None:
    cluster = ParsedCluster(
        source_resources={
            Path("eso.yaml"): parse_k8s_resources([_forgejo_creds_eso("worker")]),
            Path("deployment.yaml"): parse_k8s_resources([_forgejo_deployment("worker")]),
        }
    )
    assert check_forgejo_image_namespace_reflection(cluster) == []


def test_forgejo_image_namespace_missing_external_secret_is_flagged() -> None:
    cluster = ParsedCluster(
        source_resources={
            Path("eso.yaml"): parse_k8s_resources([_forgejo_creds_eso("other")]),
            Path("deployment.yaml"): parse_k8s_resources([_forgejo_deployment("worker")]),
        }
    )
    [error] = check_forgejo_image_namespace_reflection(cluster)
    assert "worker" in error


@pytest.mark.parametrize(
    ("api_version", "spec", "image_names"),
    [
        pytest.param(
            "extensions.agents.x-k8s.io/v1beta1",
            {"podTemplate": {"spec": {"containers": [{"image": "git.allegedly.works/ducktape-ci/upstream:tag"}]}}},
            ["upstream"],
            id="upstream-pod",
        ),
        pytest.param(
            "agentplane.allegedly.works/v1alpha1",
            {"pod": {"spec": {"initContainers": [{"image": "git.allegedly.works/ducktape-ci/init:tag"}]}}},
            ["init"],
            id="agentplane-pod",
        ),
        pytest.param(
            "agentplane.allegedly.works/v1alpha1",
            {
                "vm": {
                    "template": {
                        "spec": {"volumes": [{"containerDisk": {"image": "git.allegedly.works/ducktape-ci/disk:tag"}}]}
                    }
                }
            },
            ["disk"],
            id="agentplane-vm",
        ),
        pytest.param(
            "agentplane.allegedly.works/v1alpha1",
            {
                "firecracker": {
                    "pod": {"spec": {"containers": [{"image": "git.allegedly.works/ducktape-ci/launcher:tag"}]}},
                    "bootImage": "git.allegedly.works/ducktape-ci/boot:tag",
                    "runnerImage": "git.allegedly.works/ducktape-ci/runner:tag",
                    "memoryMiB": 256,
                    "vcpuCount": 1,
                }
            },
            ["launcher", "boot", "runner"],
            id="agentplane-firecracker",
        ),
    ],
)
def test_sandbox_template_images_require_namespace_credentials(
    api_version: str, spec: dict, image_names: list[str]
) -> None:
    template = {
        "apiVersion": api_version,
        "kind": "SandboxTemplate",
        "metadata": {"namespace": "worker", "name": "sandbox"},
        "spec": spec,
    }
    resources = parse_k8s_resources([template, _forgejo_creds_eso("other")])
    cluster = ParsedCluster(source_resources={Path("sandbox.yaml"): resources})

    [error] = check_forgejo_image_namespace_reflection(cluster)
    assert "SandboxTemplate 'worker/sandbox'" in error
    for name in image_names:
        assert f"git.allegedly.works/ducktape-ci/{name}:tag" in error

    resources.extend(parse_k8s_resources([_forgejo_creds_eso("worker")]))
    assert check_forgejo_image_namespace_reflection(cluster) == []


def test_external_credential_central_store_passes(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(tmp_path, [_consumer_external_secret()])
    assert check_external_credential_ownership(cluster, tmp_path) == []


def test_external_secret_store_conditions_and_referent_service_account() -> None:
    store: dict[str, Any] = {
        "kind": "ClusterSecretStore",
        "metadata": {"name": "shared"},
        "spec": {
            "conditions": [{"namespaces": ["allowed"]}],
            "provider": {"kubernetes": {"auth": {"serviceAccount": {"name": "reader"}}}},
        },
    }
    secret: dict[str, Any] = {
        "kind": "ExternalSecret",
        "metadata": {"name": "credential", "namespace": "denied"},
        "spec": {
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": "shared"},
            "data": [{"remoteRef": {"key": "credential"}, "secretKey": "credential"}],
        },
    }
    cluster = _cluster_with(store, secret)
    errors = check_external_secret_store_wiring(cluster)
    assert len(errors) == 2
    assert any("denied by ClusterSecretStore 'shared'" in error for error in errors)
    assert any("'denied/reader' missing" in error for error in errors)

    secret["metadata"]["namespace"] = "allowed"
    account = {"kind": "ServiceAccount", "metadata": {"name": "reader", "namespace": "allowed"}}
    assert check_external_secret_store_wiring(_cluster_with(store, secret, account)) == []


def test_cluster_external_secret_selectors_and_per_entry_store_refs() -> None:
    namespace = {"kind": "Namespace", "metadata": {"name": "consumer", "labels": {"tenant": "active"}}}
    store: dict[str, Any] = {
        "kind": "ClusterSecretStore",
        "metadata": {"name": "selected"},
        "spec": {
            "conditions": [{"namespaceSelector": {"matchLabels": {"tenant": "active"}}}],
            "provider": {"kubernetes": {"auth": {"serviceAccount": {"name": "central", "namespace": "eso"}}}},
        },
    }
    secret = {
        "kind": "ClusterExternalSecret",
        "metadata": {"name": "credential"},
        "spec": {
            "namespaceSelectors": [{"matchExpressions": [{"key": "tenant", "operator": "In", "values": ["active"]}]}],
            "externalSecretSpec": {
                # The per-entry source replaces this otherwise missing default store.
                "secretStoreRef": {"kind": "ClusterSecretStore", "name": "unused"},
                "dataFrom": [{"sourceRef": {"storeRef": {"kind": "ClusterSecretStore", "name": "selected"}}}],
            },
        },
    }
    assert check_external_secret_store_wiring(_cluster_with(namespace, store, secret)) == []
    store["spec"]["conditions"] = [{"namespaceSelector": {"matchLabels": {"tenant": "different"}}}]
    [error] = check_external_secret_store_wiring(_cluster_with(namespace, store, secret))
    assert "denied by ClusterSecretStore 'selected'" in error


def test_store_refs_use_namespace_and_per_entry_overrides() -> None:
    secret: dict[str, Any] = {
        "kind": "ExternalSecret",
        "metadata": {"name": "credential", "namespace": "consumer"},
        "spec": {
            "secretStoreRef": {"name": "default"},
            "data": [
                {"remoteRef": {"key": "a"}, "secretKey": "a"},
                {"remoteRef": {"key": "b"}, "secretKey": "b", "sourceRef": {"storeRef": {"name": "override"}}},
            ],
            "dataFrom": [{"sourceRef": {"generatorRef": {"kind": "Password", "name": "generator"}}}],
        },
    }
    stores: list[dict[str, Any]] = [
        {"kind": "SecretStore", "metadata": {"name": name, "namespace": "consumer"}} for name in ("default", "override")
    ]
    assert check_external_secret_store_wiring(_cluster_with(secret, *stores)) == []
    stores[1]["metadata"]["namespace"] = "other"
    [error] = check_external_secret_store_wiring(_cluster_with(secret, *stores))
    assert "missing SecretStore 'override'" in error
    del secret["spec"]["secretStoreRef"]
    errors = check_external_secret_store_wiring(_cluster_with(secret, *stores))
    assert any("without a store reference" in error for error in errors)


@pytest.mark.parametrize(
    ("conditions", "allowed"),
    [
        ([{"namespaceSelector": {}}], True),
        ([{"namespaceSelector": {"matchLabels": {"team": "other"}}}], False),
        (
            [{"namespaceSelector": {"matchExpressions": [{"key": "team", "operator": "NotIn", "values": ["other"]}]}}],
            True,
        ),
        ([{"namespaceSelector": {"matchExpressions": [{"key": "missing", "operator": "Exists"}]}}], False),
        ([{"namespaceSelector": {"matchExpressions": [{"key": "missing", "operator": "DoesNotExist"}]}}], True),
        ([{"namespaces": ["other"]}, {"namespaceRegexes": ["^consumer$"]}], True),
    ],
)
def test_cluster_store_namespace_conditions(conditions: list[dict], allowed: bool) -> None:
    namespace = {"kind": "Namespace", "metadata": {"name": "consumer", "labels": {"team": "active"}}}
    store = {"kind": "ClusterSecretStore", "metadata": {"name": "shared"}, "spec": {"conditions": conditions}}
    secret = {
        "kind": "ExternalSecret",
        "metadata": {"name": "credential", "namespace": "consumer"},
        "spec": {
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": "shared"},
            "data": [{"secretKey": "a", "remoteRef": {"key": "a"}}],
        },
    }
    errors = check_external_secret_store_wiring(_cluster_with(namespace, store, secret))
    assert bool(errors) is not allowed


def test_cluster_external_secret_literal_namespace_and_unknown_labels() -> None:
    store: dict[str, Any] = {
        "kind": "ClusterSecretStore",
        "metadata": {"name": "shared"},
        "spec": {"conditions": [{"namespaceSelector": {"matchLabels": {"runtime": "yes"}}}]},
    }
    secret = {
        "kind": "ClusterExternalSecret",
        "metadata": {"name": "credential"},
        "spec": {
            "namespaces": ["runtime-created"],
            "externalSecretSpec": {
                "secretStoreRef": {"kind": "ClusterSecretStore", "name": "shared"},
                "data": [{"secretKey": "a", "remoteRef": {"key": "a"}}],
            },
        },
    }
    # The namespace's runtime labels aren't in the render, so do not claim denial.
    assert check_external_secret_store_wiring(_cluster_with(store, secret)) == []
    store["spec"]["conditions"] = [{"namespaces": ["other"]}]
    [error] = check_external_secret_store_wiring(_cluster_with(store, secret))
    assert "runtime-created" in error
    assert "denied" in error


def test_external_credential_namespace_store_is_rejected(tmp_path: Path) -> None:
    cluster = _external_creds_cluster(tmp_path, [_consumer_store()])
    errors = check_external_credential_ownership(cluster, tmp_path)
    assert any("use external-secrets-config's shared ClusterSecretStore" in error for error in errors)


_VPA_UPDATE_MODE = "goldilocks.fairwinds.com/vpa-update-mode"
_GOLDILOCKS_ENABLED = "goldilocks.fairwinds.com/enabled"


def _namespace(labels: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "test-ns", "labels": labels}}


@pytest.mark.parametrize(
    ("labels", "flagged"),
    [
        ({_VPA_UPDATE_MODE: "auto"}, False),
        ({_VPA_UPDATE_MODE: "auto", _GOLDILOCKS_ENABLED: "true"}, False),
        ({_VPA_UPDATE_MODE: "auto", _GOLDILOCKS_ENABLED: "false"}, True),
    ],
)
def test_update_mode_contradicts_only_an_opt_out(labels: dict[str, str], flagged: bool) -> None:
    assert bool(check_goldilocks_namespace_labels(_cluster_with(_namespace(labels)))) is flagged


if __name__ == "__main__":
    pytest_bazel.main()
