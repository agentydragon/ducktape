"""Tests for non-graph validation checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.checks import check_cilium_policy_rules_nonempty, check_forgejo_image_namespace_reflection
from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import parse_k8s_resources


def _cluster_with(doc: dict) -> ParsedCluster:
    return ParsedCluster(source_resources={Path("policy.yaml"): parse_k8s_resources([doc])})


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


if __name__ == "__main__":
    pytest_bazel.main()
