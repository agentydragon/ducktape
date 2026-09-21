"""Flux Kustomizations for the cluster/k8s/haku-ci slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def haku_ci(chart: Chart, keda: Kustomization) -> Kustomization:
    name = "haku-ci"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/haku-ci",
            prune=True,
            # The runner pod stays pending until its registration-token Secret
            # (haku-ci-runner-token) is provisioned during paving — don't block on health.
            wait=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            # Supplies the ScaledJob and TriggerAuthentication CRDs.
            depends_on=flux_kustomization_depends_on_many(keda),
        ),
    )
