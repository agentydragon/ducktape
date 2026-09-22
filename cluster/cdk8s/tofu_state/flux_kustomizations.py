"""Flux Kustomizations for the cluster/k8s/tofu-state slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def tofu_state_db(chart: Chart, cnpg: Kustomization) -> Kustomization:
    name = "tofu-state-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/tofu-state",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="postgresql.cnpg.io/v1",
                    kind="Database",
                    current=(
                        "has(status.applied) && status.applied && "
                        "has(status.observedGeneration) && status.observedGeneration == "
                        "metadata.generation"
                    ),
                )
            ],
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(cnpg),
        ),
    )
