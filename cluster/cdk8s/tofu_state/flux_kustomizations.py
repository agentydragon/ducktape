"""Flux Kustomizations for the cluster/k8s/tofu-state slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def tofu_state_db(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization) -> Kustomization:
    name = "tofu-state-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
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
