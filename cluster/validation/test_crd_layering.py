"""CRD layering follows provider dependencies, not the presence of any HelmRelease."""

from __future__ import annotations

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import validate_operator_dependencies
from cluster.validation.flux import DependsOn, FluxKustomizationSpec
from cluster.validation.k8s import K8sResource


def test_app_helmrelease_requires_all_its_crd_providers() -> None:
    cluster = ParsedCluster(
        flux_kustomizations={
            "app": FluxKustomizationSpec(depends_on=[DependsOn(name="cnpg"), DependsOn(name="observability")]),
            "observability": FluxKustomizationSpec(depends_on=[DependsOn(name="monitoring-crds")]),
            "cnpg": FluxKustomizationSpec(),
            "monitoring-crds": FluxKustomizationSpec(),
            "crossplane-provider-litellm": FluxKustomizationSpec(),
        },
        rendered_resources={
            "app": [
                K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                K8sResource(kind="Cluster", apiVersion="postgresql.cnpg.io/v1"),
                K8sResource(kind="ServiceMonitor", apiVersion="monitoring.coreos.com/v1"),
                K8sResource(kind="Key", apiVersion="litellm.m.crossplane.io/v1alpha1"),
                K8sResource(kind="Organization", apiVersion="litellm.m.crossplane.io/v1alpha1"),
            ]
        },
    )
    assert validate_operator_dependencies(cluster) == [
        "app uses Key resources but doesn't transitively depend on crossplane-provider-litellm"
    ]


def test_operator_cannot_satisfy_its_own_helm_install_dependency() -> None:
    cluster = ParsedCluster(
        flux_kustomizations={"test-operator": FluxKustomizationSpec()},
        rendered_resources={
            "test-operator": [
                K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
                K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
            ]
        },
    )
    errors = validate_operator_dependencies(cluster, {"TestInstance": "test-operator"})
    assert len(errors) == 1
    assert "test-operator" in errors[0]
    assert "TestInstance" in errors[0]


def test_custom_kind_without_provider_fails() -> None:
    cluster = ParsedCluster(
        flux_kustomizations={"test-app": FluxKustomizationSpec()},
        rendered_resources={
            "test-app": [
                K8sResource(kind="ConfigMap", apiVersion="v1"),
                K8sResource(kind="Deployment", apiVersion="apps/v1"),
                K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                K8sResource(kind="CiliumNetworkPolicy", apiVersion="cilium.io/v2"),
                K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
                K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
            ]
        },
    )
    errors = validate_operator_dependencies(cluster, {})
    assert len(errors) == 1
    assert "test-app" in errors[0]
    assert "TestInstance" in errors[0]
    assert "test.example/v1" in errors[0]


if __name__ == "__main__":
    pytest_bazel.main()
