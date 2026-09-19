"""Flux Kustomizations for the cluster/k8s/kube-api-proxy slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def kube_api_proxy() -> dict[str, object]:
    name = "kube-api-proxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kube-api-proxy",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="kubeapi-proxy", namespace="default"
                ),
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1",
                    kind="HTTPRoute",
                    name="kubeapi-allegedly-works",
                    namespace="default",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/kube-api-proxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kube_api_proxy())
