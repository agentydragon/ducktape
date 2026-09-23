"""Flux Kustomizations for the cluster/k8s/external-secrets slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def external_secrets_crds(chart: Chart) -> Kustomization:
    name = "external-secrets-crds"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="1h",
            # CRDs from the external-secrets repository
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="external-secrets-source",
                namespace="ducktape-flux",
            ),
            path="./deploy/crds",
            prune=False,  # Don't delete CRDs on uninstall (safety)
            wait=True,
            timeout="2m",
        ),
    )
