"""Flux Kustomizations for the cluster/k8s/sshpiper-crds slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def sshpiper_crds(chart: Chart) -> Kustomization:
    name = "sshpiper-crds"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="sshpiper-source", namespace="ducktape-flux"
        ),
        timeout="2m",
        path="./plugin/kubernetes",
        # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
        # for the other CRD Kustomizations (agentplane-crds, external-secrets-crds).
        prune=False,
        description="The sshpiper Pipe CRD, sourced from the tag used by the deployed image.",
    )
