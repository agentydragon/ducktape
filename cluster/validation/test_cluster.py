"""Tests for Flux Kustomization path resolution."""

from pathlib import Path

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomizationSpec
from cluster.validation.k8s import K8sMetadata, K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def test_both_manifest_roots_are_local(tmp_path: Path) -> None:
    for root in ("cluster/k8s", "cluster/generated"):
        assert FluxKustomizationSpec(path=f"./{root}/app").local_dir(tmp_path) == (tmp_path / root / "app").resolve()
    # A path in another source (an upstream GitRepository, a project's own deploy/) is not.
    assert FluxKustomizationSpec(path="./deploy").local_dir(tmp_path) is None


def test_authorization_inventory_can_retain_suspended_owners_without_parked_workloads(tmp_path: Path) -> None:
    cluster = ParsedCluster(
        flux_kustomizations={
            "running": FluxKustomizationSpec(path="./cluster/k8s/running"),
            "held": FluxKustomizationSpec(path="./cluster/k8s/held", suspend=True),
            "parked": FluxKustomizationSpec(path="./cluster/k8s/parked", suspend=True, parked=True),
        }
    )
    for name in cluster.flux_kustomizations:
        cluster.build_results.append(
            KustomizeBuildResult(
                kustomization_path=tmp_path / "cluster/k8s" / name / "kustomization.yaml",
                resources=[
                    K8sResource(
                        kind="Role",
                        api_version="rbac.authorization.k8s.io/v1",
                        metadata=K8sMetadata(name="reader", namespace=name),
                    )
                ],
            )
        )
    assert set(cluster.flux_kust_resources(tmp_path)) == {"running"}
    retained = cluster.flux_kust_resources(tmp_path, include_suspended=True)
    assert {resource.namespace for resources in retained.values() for resource in resources} == {"running", "held"}


if __name__ == "__main__":
    pytest_bazel.main()
