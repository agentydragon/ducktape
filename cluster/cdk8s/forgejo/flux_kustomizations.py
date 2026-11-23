"""Flux Kustomizations for the cluster/k8s/forgejo slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many


def forgejo(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
    monitoring_crds: Kustomization,
    valkey: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "forgejo",
        directory,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        # Admission needs these CRDs; runtime dependencies converge independently.
        depends_on=flux_kustomization_depends_on_many(
            cnpg,
            external_secrets_operator,
            seaweedfs_operator,
            monitoring_crds,
            # the cache's RedisReplication CRD and its operator
            valkey,
        ),
    )
