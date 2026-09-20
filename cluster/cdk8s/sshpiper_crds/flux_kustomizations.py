"""Flux Kustomizations for the cluster/k8s/sshpiper-crds slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def sshpiper_crds(chart: Chart) -> Kustomization:
    name = "sshpiper-crds"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./plugin/kubernetes",
            # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
            # for the other CRD Kustomizations (agentplane-crds, external-secrets-crds).
            prune=False,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="sshpiper-source", namespace="ducktape-flux"
            ),
        ),
        description="The sshpiper Pipe CRD, sourced from the tag used by the deployed image.",
    )
