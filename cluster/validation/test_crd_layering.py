"""CRD layering follows provider dependencies, not the presence of any HelmRelease."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import validate_operator_dependencies
from cluster.validation.flux import DependsOn, FluxKustomizationSpec
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
@pytest.mark.parametrize(
    ("kind", "api_version", "provider"),
    [
        ("ExternalSecret", "external-secrets.io/v1", "external-secrets-operator"),
        ("Cluster", "postgresql.cnpg.io/v1", "cnpg"),
        ("ServiceMonitor", "monitoring.coreos.com/v1", "monitoring-crds"),
        ("Bundle", "trust.cert-manager.io/v1alpha1", "cert-manager-trust"),
    ],
)
def test_app_helmrelease_can_share_operator_instances(
    tmp_path: Path, kind: str, api_version: str, provider: str, transitive: bool
) -> None:
    cluster = ParsedCluster(
        flux_kustomizations={
            "test-app": FluxKustomizationSpec(
                path="./cluster/k8s/test-app",
                depends_on=[DependsOn(name="test-prerequisites" if transitive else provider)],
            ),
            "test-prerequisites": FluxKustomizationSpec(depends_on=[DependsOn(name=provider)]),
            provider: FluxKustomizationSpec(),
        },
        build_results=[
            KustomizeBuildResult(
                kustomization_path=tmp_path / "test-app/kustomization.yaml",
                resources=[
                    K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                    K8sResource(kind=kind, apiVersion=api_version),
                ],
            )
        ],
    )
    assert validate_operator_dependencies(cluster, tmp_path) == []

    cluster.graph.remove_edge("test-app", "test-prerequisites" if transitive else provider)
    errors = validate_operator_dependencies(cluster, tmp_path)
    assert len(errors) == 1
    assert f"test-app uses {kind}" in errors[0]
    assert f"depend on {provider}" in errors[0]


@pytest.mark.parametrize("subdir", ["test-operator", "cert-manager/app", "test/overlays/staging"])
def test_operator_cannot_satisfy_its_own_helm_install_dependency(tmp_path: Path, subdir: str) -> None:
    cluster = ParsedCluster(
        flux_kustomizations={"test-operator": FluxKustomizationSpec(path=f"./cluster/k8s/{subdir}")},
        build_results=[
            KustomizeBuildResult(
                kustomization_path=tmp_path / subdir / "kustomization.yaml",
                resources=[
                    K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                    K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
                    K8sResource(kind="TestInstance", apiVersion="test.example/v1"),
                ],
            )
        ],
    )
    errors = validate_operator_dependencies(cluster, tmp_path, {"TestInstance": "test-operator"})
    assert len(errors) == 1
    assert "test-operator installs its operator through Helm" in errors[0]
    assert "TestInstance" in errors[0]
    assert "separate Kustomization" in errors[0]


def test_directly_applied_provider_has_no_helm_install_boundary(tmp_path: Path) -> None:
    cluster = ParsedCluster(
        flux_kustomizations={"test-provider": FluxKustomizationSpec(path="./cluster/k8s/test-provider")},
        build_results=[
            KustomizeBuildResult(
                kustomization_path=tmp_path / "test-provider/kustomization.yaml",
                resources=[K8sResource(kind="TestInstance", apiVersion="test.example/v1")],
            )
        ],
    )
    assert validate_operator_dependencies(cluster, tmp_path, {"TestInstance": "test-provider"}) == []


if __name__ == "__main__":
    pytest_bazel.main()
