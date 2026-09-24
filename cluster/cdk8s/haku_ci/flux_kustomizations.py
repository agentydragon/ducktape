"""Flux Kustomizations for the cluster/k8s/haku-ci slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def haku_ci(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, keda: Kustomization) -> Kustomization:
    name = "haku-ci"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        # The runner pod stays pending until its registration-token Secret
        # (haku-ci-runner-token) is provisioned during paving — don't block on health.
        wait=False,
        # Supplies the ScaledJob and TriggerAuthentication CRDs.
        depends_on=flux_kustomization_depends_on_many(keda),
    )
