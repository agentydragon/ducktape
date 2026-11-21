"""The static-service VPA policies stay within their Deployment resource contracts."""

from typing import Any

import pytest_bazel
from cdk8s import App
from more_itertools import one

from cluster.cdk8s.agentplane import staging_vpa
from cluster.cdk8s.testing.manifests import find_resource


def _mib(quantity: str) -> int:
    if quantity.endswith("Gi"):
        return int(quantity[:-2]) * 1024
    assert quantity.endswith("Mi")
    return int(quantity[:-2])


def test_vpas_target_only_static_deployments(scratch_app: App, staging_manifests: list[dict[str, Any]]) -> None:
    vpas = staging_vpa.chart(scratch_app).to_json()
    assert len(vpas) == 11
    assert all(vpa["apiVersion"] == "autoscaling.k8s.io/v1" and vpa["kind"] == "VerticalPodAutoscaler" for vpa in vpas)
    assert (
        find_resource(staging_manifests, "Namespace", "agentplane-staging")["metadata"]["labels"][
            "goldilocks.fairwinds.com/enabled"
        ]
        == "false"
    )

    for vpa in vpas:
        spec = vpa["spec"]
        target = spec["targetRef"]
        assert target == {"apiVersion": "apps/v1", "kind": "Deployment", "name": vpa["metadata"]["name"]}
        assert vpa["metadata"]["namespace"] == "agentplane-staging"
        assert spec["updatePolicy"] == {"updateMode": "Recreate" if target["name"] == "frontend" else "Initial"}
        deployment = find_resource(staging_manifests, "Deployment", target["name"])
        container = one(deployment["spec"]["template"]["spec"]["containers"])
        selected, wildcard = spec["resourcePolicy"]["containerPolicies"]
        assert selected["containerName"] == container["name"]
        resources = set(selected["controlledResources"])
        assert resources in ({"memory"}, {"cpu", "memory"})
        assert set(selected["minAllowed"]) == set(selected["maxAllowed"]) == {"memory"}
        assert selected["controlledValues"] == "RequestsOnly"
        assert _mib(selected["minAllowed"]["memory"]) >= _mib(container["resources"]["requests"]["memory"])
        assert _mib(selected["maxAllowed"]["memory"]) <= _mib(container["resources"]["limits"]["memory"])
        if "cpu" in resources:
            assert spec["updatePolicy"]["updateMode"] == "Initial"
        assert wildcard == {"containerName": "*", "mode": "Off"}


if __name__ == "__main__":
    pytest_bazel.main()
