"""Flux Kustomizations for the cluster/k8s/flux-grafana-secrets slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def flux_grafana_secrets() -> dict[str, object]:
    name = "flux-grafana-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/flux-grafana-secrets",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="grafana.integreatly.org/v1beta1",
                    kind="GrafanaServiceAccount",
                    name="flux-notifications",
                    namespace="flux-system",
                )
            ],
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="grafana-instance", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grafana-operator", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/flux-grafana-secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, flux_grafana_secrets())
