"""Flux Kustomizations for the cluster/k8s/authentik slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def authentik(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "authentik"
    return flux_kustomization(
        chart,
        name,
        artifact,
        interval="10m0s",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="10m0s",
        # Health checks ensure Authentik is fully operational before dependents start
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="authentik", namespace="authentik"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="authentik-server", namespace="authentik"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="authentik-worker", namespace="authentik"
            ),
        ],
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
    )
