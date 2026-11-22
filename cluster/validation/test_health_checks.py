"""Unit tests for controller resource health check validation."""

from __future__ import annotations

import pytest
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
