"""Flux Kustomizations for the cluster/k8s/haku-ci slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def haku_ci(
    chart: Chart, forgejo: Kustomization, keda: Kustomization, reflector: Kustomization, haku_forgejo_tea: Kustomization
) -> Kustomization:
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
            depends_on=[
                # Forgejo (with Actions enabled, #2556) must be up first
                flux_kustomization_depends_on(forgejo),
                # supplies the ScaledObject and TriggerAuthentication CRDs
                flux_kustomization_depends_on(keda),
                # Reflector mirrors haku-forgejo-tea from the completed haku-forgejo-tea
                # Kustomization into haku-ci for the native Forgejo KEDA scaler.
                flux_kustomization_depends_on(reflector),
                flux_kustomization_depends_on(haku_forgejo_tea),
            ],
        ),
    )
