"""Tests for Flux Kustomization path resolution."""

from pathlib import Path

import pytest_bazel

from cluster.validation.cluster import ParsedCluster, parse_cluster
from cluster.validation.flux import FluxKustomizationSpec
from cluster.validation.k8s import K8sMetadata, K8sResource


def test_both_manifest_roots_are_local(tmp_path: Path) -> None:
    for root in ("cluster/k8s", "cluster/generated"):
        assert FluxKustomizationSpec(path=f"./{root}/app").local_dir(tmp_path) == (tmp_path / root / "app").resolve()
    # A path in another source (an upstream GitRepository, a project's own deploy/) is not.
    assert FluxKustomizationSpec(path="./deploy").local_dir(tmp_path) is None


def test_only_resources_declare_flux_sources(tmp_path: Path) -> None:
    app = tmp_path / "cluster/k8s/app"
    app.mkdir(parents=True)
    (app / "Kustomization").write_text(
        "resources: [source.yml]\npatches: [{path: patch.yaml}]\n"
        "configMapGenerator: [{name: app-config, files: [config.yaml]}]\n"
        "secretGenerator: [{name: credentials, files: [token=secret.yaml], envs: [settings.env]}]\n"
        "configurations: [transform.yaml]\n"
    )
    for path in (app / "source.yml", app / "patch.yaml"):
        path.write_text(f"kind: GitRepository\nmetadata: {{namespace: app, name: {path.stem}}}\n")
    (app / "config.yaml").write_text("setting: !application value\n")
    cluster = parse_cluster(tmp_path)
    assert cluster.kustomize_files[app / "Kustomization"].all_referenced_files == {
        app / filename
        for filename in ("source.yml", "patch.yaml", "config.yaml", "secret.yaml", "settings.env", "transform.yaml")
    }
    assert cluster.flux_sources == {("GitRepository", "app", "source")}


def test_authorization_inventory_can_retain_suspended_owners_without_parked_workloads() -> None:
    cluster = ParsedCluster(
        flux_kustomizations={
            "running": FluxKustomizationSpec(),
            "held": FluxKustomizationSpec(suspend=True),
            "parked": FluxKustomizationSpec(suspend=True, parked=True),
        }
    )
    for name in cluster.flux_kustomizations:
        cluster.rendered_resources[name] = [
            K8sResource(
                kind="Role",
                api_version="rbac.authorization.k8s.io/v1",
                metadata=K8sMetadata(name="reader", namespace=name),
            )
        ]
    assert set(cluster.flux_kust_resources()) == {"running"}
    retained = cluster.flux_kust_resources(include_suspended=True)
    assert {resource.namespace for resources in retained.values() for resource in resources} == {"running", "held"}


if __name__ == "__main__":
    pytest_bazel.main()
