"""Flux Kustomizations for the cluster/k8s/external-secrets slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def external_secrets_crds(chart: Chart) -> Kustomization:
    name = "external-secrets-crds"
    return flux_kustomization(
        chart,
        name,
        # CRDs from the external-secrets repository
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
            name="external-secrets-source",
            namespace="ducktape-flux",
        ),
        interval="1h",
        path="./deploy/crds",
        prune=False,  # Don't delete CRDs on uninstall (safety)
        timeout="2m",
    )
