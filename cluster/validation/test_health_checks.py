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


@pytest.mark.parametrize(
    ("health_checks", "wait", "retry_interval", "flags_missing_retry"),
    [
        pytest.param([HealthCheck(kind="HelmRelease")], False, "1m", False, id="async-with-retry"),
        pytest.param([HealthCheck(kind="HelmRelease")], False, None, True, id="async-without-retry"),
        pytest.param([], True, "1m", False, id="wait-with-retry"),
        pytest.param([], True, None, True, id="wait-without-retry"),
        pytest.param([HealthCheck(kind="Namespace")], False, None, False, id="sync-without-retry"),
    ],
)
def test_retry_policy(
    health_checks: list[HealthCheck], wait: bool, retry_interval: str | None, flags_missing_retry: bool
) -> None:
    spec = FluxKustomizationSpec(
        path="./cluster/k8s/test-app", health_checks=health_checks, retry_interval=retry_interval, wait=wait
    )
    errors = check_retry_policy(ParsedCluster(flux_kustomizations={"test-app": spec}))
    if flags_missing_retry:
        assert len(errors) == 1
        assert "test-app" in errors[0]
        assert "no retryInterval" in errors[0]
    else:
        assert errors == []


if __name__ == "__main__":
    pytest_bazel.main()
