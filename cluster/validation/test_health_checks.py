"""Unit tests for controller resource health check validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomizationSpec, HealthCheck
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def _make_cluster(
    repo_root: Path,
    *,
    resource_kind: str = "HelmRelease",
    resource_api_version: str = "helm.toolkit.fluxcd.io/v2",
    health_check_kind: str | None = None,
    wait: bool = False,
) -> ParsedCluster:
    """Build a minimal ParsedCluster with one resource and optional healthCheck."""
    kust_file = repo_root / "cluster/k8s/test-app/kustomization.yaml"

    return ParsedCluster(
        flux_kustomizations={
            "test-app": FluxKustomizationSpec(
                path="./cluster/k8s/test-app",
                health_checks=[HealthCheck(kind=health_check_kind, name="test-app", namespace="test-app")]
                if health_check_kind
                else [],
                wait=wait,
            )
        },
        build_results=[
            KustomizeBuildResult(
                kustomization_path=kust_file,
                resources=[K8sResource(kind=resource_kind, apiVersion=resource_api_version)],
            )
        ],
    )


class TestControllerResourceHealthChecks:
    @pytest.mark.parametrize(
        ("resource_kind", "resource_api_version", "health_check_kind"),
        [
            ("HelmRelease", "helm.toolkit.fluxcd.io/v2", "HelmRelease"),
            ("Terraform", "infra.contrib.fluxcd.io/v1alpha2", "Terraform"),
        ],
    )
    def test_no_error_with_matching_healthcheck(
        self, tmp_path: Path, resource_kind: str, resource_api_version: str, health_check_kind: str
    ) -> None:
        cluster = _make_cluster(
            tmp_path,
            resource_kind=resource_kind,
            resource_api_version=resource_api_version,
            health_check_kind=health_check_kind,
        )
        assert check_controller_health_checks(cluster, tmp_path) == []

    @pytest.mark.parametrize(
        ("resource_kind", "resource_api_version"),
        [("HelmRelease", "helm.toolkit.fluxcd.io/v2"), ("Terraform", "infra.contrib.fluxcd.io/v1alpha2")],
    )
    def test_error_without_healthcheck(self, tmp_path: Path, resource_kind: str, resource_api_version: str) -> None:
        cluster = _make_cluster(tmp_path, resource_kind=resource_kind, resource_api_version=resource_api_version)
        errors = check_controller_health_checks(cluster, tmp_path)
        assert len(errors) == 1
        assert resource_kind in errors[0]

    def test_wait_covers_controller_resources(self, tmp_path: Path) -> None:
        """wait: true health-checks every applied object, the HelmRelease included."""
        assert check_controller_health_checks(_make_cluster(tmp_path, wait=True), tmp_path) == []

    def test_no_error_for_plain_resources(self, tmp_path: Path) -> None:
        cluster = _make_cluster(tmp_path, resource_kind="ConfigMap", resource_api_version="v1")
        assert check_controller_health_checks(cluster, tmp_path) == []


class TestRetryPolicy:
    def _make_cluster(
        self, *, health_check_kind: str | None = "HelmRelease", retry_interval: str | None = None, wait: bool = False
    ) -> ParsedCluster:
        return ParsedCluster(
            flux_kustomizations={
                "test-app": FluxKustomizationSpec(
                    path="./cluster/k8s/test-app",
                    health_checks=[HealthCheck(kind=health_check_kind, name="test-app", namespace="test-app")]
                    if health_check_kind
                    else [],
                    retry_interval=retry_interval,
                    wait=wait,
                )
            }
        )

    def test_async_health_check_with_retry_interval_passes(self) -> None:
        check_retry_policy(self._make_cluster(retry_interval="1m"))

    def test_async_health_check_without_retry_interval_fails(self) -> None:
        with pytest.raises(AssertionError, match="no retryInterval"):
            check_retry_policy(self._make_cluster())

    def test_wait_true_without_retry_interval_fails(self) -> None:
        """wait: true requires retryInterval (spec.retries was removed from the Flux CRD)."""
        with pytest.raises(AssertionError, match="no retryInterval"):
            check_retry_policy(self._make_cluster(health_check_kind=None, wait=True))

    def test_wait_true_with_retry_passes(self) -> None:
        check_retry_policy(self._make_cluster(health_check_kind=None, wait=True, retry_interval="1m"))

    def test_cli_proxy_api_preserves_existing_retry_interval_omission(self) -> None:
        cluster = ParsedCluster(flux_kustomizations={"cli-proxy-api": FluxKustomizationSpec(wait=True)})
        check_retry_policy(cluster)

    def test_sync_only_no_wait_no_retry_passes(self) -> None:
        """Sync-only health checks (Namespace) without wait don't need retryInterval."""
        check_retry_policy(self._make_cluster(health_check_kind="Namespace"))


if __name__ == "__main__":
    pytest_bazel.main()
