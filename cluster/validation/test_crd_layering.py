"""CRD layering follows provider dependencies, not the presence of any HelmRelease."""

from __future__ import annotations

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import validate_operator_dependencies
from cluster.validation.flux import DependsOn, FluxKustomizationSpec
from cluster.validation.k8s import K8sResource


@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
@pytest.mark.parametrize(
    ("kind", "api_version", "provider"),
    [
        ("ExternalSecret", "external-secrets.io/v1", "external-secrets-operator"),
        ("Cluster", "postgresql.cnpg.io/v1", "cnpg"),
        ("ServiceMonitor", "monitoring.coreos.com/v1", "monitoring-crds"),
        ("Provider", "pkg.crossplane.io/v1", "crossplane"),
        ("Key", "litellm.m.crossplane.io/v1alpha1", "crossplane-provider-litellm"),
        ("Organization", "litellm.m.crossplane.io/v1alpha1", "crossplane-provider-litellm"),
    ],
)
def test_app_helmrelease_can_share_operator_instances(
    kind: str, api_version: str, provider: str, transitive: bool
) -> None:
    cluster = ParsedCluster(
        flux_kustomizations={
            "test-app": FluxKustomizationSpec(
                depends_on=[DependsOn(name="test-prerequisites" if transitive else provider)]
            ),
            "test-prerequisites": FluxKustomizationSpec(depends_on=[DependsOn(name=provider)]),
            provider: FluxKustomizationSpec(),
        },
        rendered_resources={
            "test-app": [
                K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                K8sResource(kind=kind, apiVersion=api_version),
            ]
        },
    )
    assert validate_operator_dependencies(cluster) == []

    cluster.graph.remove_edge("test-app", "test-prerequisites" if transitive else provider)
    errors = validate_operator_dependencies(cluster)
    assert len(errors) == 1
    assert "test-app" in errors[0]
    assert kind in errors[0]
    assert provider in errors[0]


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
