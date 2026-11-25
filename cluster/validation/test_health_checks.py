"""Unit tests for controller resource health check validation."""

from __future__ import annotations

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomizationSpec, HealthCheck
from cluster.validation.health_checks import check_controller_health_checks, check_retry_policy
from cluster.validation.k8s import parse_k8s_resources


def test_health_checks_cover_each_applied_controller() -> None:
    version = "helm.toolkit.fluxcd.io/v2"

    def release(name: str, namespace: str, *, suspend: bool = False) -> dict:
        return {
            "kind": "HelmRelease",
            "apiVersion": version,
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"suspend": suspend},
        }

    cluster = ParsedCluster(
        flux_kustomizations={
            "apps": FluxKustomizationSpec(
                health_checks=[HealthCheck(api_version=version, kind="HelmRelease", name="api", namespace="a")]
            ),
            "infra": FluxKustomizationSpec(wait=True),
        },
        rendered_resources={
            "apps": parse_k8s_resources(
                [release("api", "a"), release("worker", "a"), release("api", "b"), release("staged", "a", suspend=True)]
            ),
            "infra": parse_k8s_resources([release("infra", "a")]),
        },
    )
    assert check_controller_health_checks(cluster) == [
        "apps: missing health check for HelmRelease a/worker",
        "apps: missing health check for HelmRelease b/api",
    ]


def test_retry_policy_reports_all_async_kustomizations_missing_a_retry_interval() -> None:
    release = HealthCheck(kind="HelmRelease")
    cluster = ParsedCluster(
        flux_kustomizations={
            "checked": FluxKustomizationSpec(health_checks=[release]),
            "waiting": FluxKustomizationSpec(wait=True),
            "checked-with-retry": FluxKustomizationSpec(health_checks=[release], retry_interval="1m"),
            "waiting-with-retry": FluxKustomizationSpec(wait=True, retry_interval="1m"),
            "sync": FluxKustomizationSpec(health_checks=[HealthCheck(kind="Namespace")]),
            "suspended": FluxKustomizationSpec(wait=True, suspend=True),
        }
    )
    errors = check_retry_policy(cluster)
    assert sorted(error.partition(":")[0] for error in errors) == ["checked", "waiting"]
    assert all("no retryInterval" in error for error in errors)


if __name__ == "__main__":
    pytest_bazel.main()
