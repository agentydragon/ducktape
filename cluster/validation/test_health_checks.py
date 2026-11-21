"""Unit tests for controller resource health check validation."""

from __future__ import annotations

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomizationSpec, HealthCheck
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.k8s import K8sResource


def _make_cluster(
    *,
    resource_kind: str = "HelmRelease",
    resource_api_version: str = "helm.toolkit.fluxcd.io/v2",
    health_check_kind: str | None = None,
    wait: bool = False,
) -> ParsedCluster:
    """Build a minimal ParsedCluster with one resource and optional healthCheck."""
    return ParsedCluster(
        flux_kustomizations={
            "test-app": FluxKustomizationSpec(
                health_checks=[HealthCheck(kind=health_check_kind, name="test-app", namespace="test-app")]
                if health_check_kind
                else [],
                wait=wait,
            )
        },
        rendered_resources={"test-app": [K8sResource(kind=resource_kind, apiVersion=resource_api_version)]},
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
        self, resource_kind: str, resource_api_version: str, health_check_kind: str
    ) -> None:
        cluster = _make_cluster(
            resource_kind=resource_kind, resource_api_version=resource_api_version, health_check_kind=health_check_kind
        )
        assert check_controller_health_checks(cluster) == []

    @pytest.mark.parametrize(
        ("resource_kind", "resource_api_version"),
        [("HelmRelease", "helm.toolkit.fluxcd.io/v2"), ("Terraform", "infra.contrib.fluxcd.io/v1alpha2")],
    )
    def test_error_without_healthcheck(self, resource_kind: str, resource_api_version: str) -> None:
        cluster = _make_cluster(resource_kind=resource_kind, resource_api_version=resource_api_version)
        errors = check_controller_health_checks(cluster)
        assert len(errors) == 1
        assert resource_kind in errors[0]

    def test_wait_covers_controller_resources(self) -> None:
        """wait: true health-checks every applied object, the HelmRelease included."""
        assert check_controller_health_checks(_make_cluster(wait=True)) == []

    def test_no_error_for_plain_resources(self) -> None:
        cluster = _make_cluster(resource_kind="ConfigMap", resource_api_version="v1")
        assert check_controller_health_checks(cluster) == []


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
