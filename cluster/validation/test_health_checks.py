"""Unit tests for controller resource health check validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomization, FluxKustomizationSpec, HealthCheck
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeFile


def _make_cluster(
    k8s_dir: Path,
    *,
    resource_kind: str = "HelmRelease",
    resource_api_version: str = "helm.toolkit.fluxcd.io/v2",
    health_check_kind: str | None = None,
) -> ParsedCluster:
    """Build a minimal ParsedCluster with one resource and optional healthCheck."""
    resource_file = k8s_dir / "test-app" / "resource.yaml"
    kust_file = k8s_dir / "test-app" / "kustomization.yaml"
    flux_file = k8s_dir / "test-app" / "flux-kustomization.yaml"

    return ParsedCluster(
        kustomize_files={kust_file: KustomizeFile(path=kust_file, resources=[resource_file])},
        flux_kustomizations={
            "test-app": FluxKustomization(
                name="test-app",
                file_path=flux_file,
                spec=FluxKustomizationSpec(
                    path="./cluster/k8s/test-app",
                    health_checks=[HealthCheck(kind=health_check_kind, name="test-app", namespace="test-app")]
                    if health_check_kind
                    else [],
                ),
            )
        },
        source_resources={resource_file: [K8sResource(kind=resource_kind, apiVersion=resource_api_version)]},
    )


class TestControllerResourceHealthChecks:
    @pytest.fixture
    def repo_root(self, tmp_path: Path) -> Path:
        (tmp_path / "cluster" / "k8s").mkdir(parents=True)
        return tmp_path

    @pytest.fixture
    def k8s_dir(self, repo_root: Path) -> Path:
        return repo_root / "cluster" / "k8s"

    @pytest.mark.parametrize(
        ("resource_kind", "resource_api_version", "health_check_kind"),
        [
            ("HelmRelease", "helm.toolkit.fluxcd.io/v2", "HelmRelease"),
            ("Terraform", "infra.contrib.fluxcd.io/v1alpha2", "Terraform"),
        ],
    )
    def test_no_error_with_matching_healthcheck(
        self, k8s_dir: Path, repo_root: Path, resource_kind: str, resource_api_version: str, health_check_kind: str
    ) -> None:
        cluster = _make_cluster(
            k8s_dir,
            resource_kind=resource_kind,
            resource_api_version=resource_api_version,
            health_check_kind=health_check_kind,
        )
        assert check_controller_health_checks(cluster, k8s_dir, repo_root) == []

    @pytest.mark.parametrize(
        ("resource_kind", "resource_api_version"),
        [("HelmRelease", "helm.toolkit.fluxcd.io/v2"), ("Terraform", "infra.contrib.fluxcd.io/v1alpha2")],
    )
    def test_error_without_healthcheck(
        self, k8s_dir: Path, repo_root: Path, resource_kind: str, resource_api_version: str
    ) -> None:
        cluster = _make_cluster(k8s_dir, resource_kind=resource_kind, resource_api_version=resource_api_version)
        errors = check_controller_health_checks(cluster, k8s_dir, repo_root)
        assert len(errors) == 1
        assert resource_kind in errors[0]

    def test_no_error_for_plain_resources(self, k8s_dir: Path, repo_root: Path) -> None:
        cluster = _make_cluster(k8s_dir, resource_kind="ConfigMap", resource_api_version="v1")
        assert check_controller_health_checks(cluster, k8s_dir, repo_root) == []


class TestRetryPolicy:
    @pytest.fixture
    def k8s_dir(self, tmp_path: Path) -> Path:
        k8s = tmp_path / "cluster" / "k8s"
        k8s.mkdir(parents=True)
        return k8s

    def _make_cluster_with_retry(
        self,
        k8s_dir: Path,
        *,
        health_check_kind: str = "HelmRelease",
        retry_interval: str | None = None,
        wait: bool = False,
    ) -> ParsedCluster:
        flux_file = k8s_dir / "test-app" / "flux-kustomization.yaml"
        return ParsedCluster(
            kustomize_files={},
            flux_kustomizations={
                "test-app": FluxKustomization(
                    name="test-app",
                    file_path=flux_file,
                    spec=FluxKustomizationSpec(
                        path="./cluster/k8s/test-app",
                        health_checks=[HealthCheck(kind=health_check_kind, name="test-app", namespace="test-app")],
                        retry_interval=retry_interval,
                        wait=wait,
                    ),
                )
            },
            source_resources={},
        )

    def test_async_health_check_with_retry_interval_passes(self, k8s_dir: Path) -> None:
        cluster = self._make_cluster_with_retry(k8s_dir, retry_interval="1m")
        check_retry_policy(cluster, k8s_dir)

    def test_async_health_check_without_retry_interval_fails(self, k8s_dir: Path) -> None:
        cluster = self._make_cluster_with_retry(k8s_dir)
        with pytest.raises(AssertionError, match="no retryInterval"):
            check_retry_policy(cluster, k8s_dir)

    def test_wait_true_without_retry_interval_fails(self, k8s_dir: Path) -> None:
        """wait: true requires retryInterval (spec.retries was removed from the Flux CRD)."""
        cluster = self._make_cluster_with_retry(k8s_dir, health_check_kind="Namespace", wait=True)
        with pytest.raises(AssertionError, match="no retryInterval"):
            check_retry_policy(cluster, k8s_dir)

    def test_wait_true_with_retry_passes(self, k8s_dir: Path) -> None:
        cluster = self._make_cluster_with_retry(k8s_dir, health_check_kind="Namespace", wait=True, retry_interval="1m")
        check_retry_policy(cluster, k8s_dir)

    def test_sync_only_no_wait_no_retry_passes(self, k8s_dir: Path) -> None:
        """Sync-only health checks (Namespace) without wait don't need retryInterval."""
        cluster = self._make_cluster_with_retry(k8s_dir, health_check_kind="Namespace")
        check_retry_policy(cluster, k8s_dir)


if __name__ == "__main__":
    pytest_bazel.main()
