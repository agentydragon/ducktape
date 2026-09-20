"""Flux Kustomizations for the cluster/k8s/agentplane-crds slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def agentplane_crds(chart: Chart) -> Kustomization:
    name = "agentplane-crds"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./",
            # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
            # for the other CRD Kustomizations (external-secrets-crds, snapshot-controller-crds).
            prune=False,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
        description=(
            "Agentplane's own CRDs (EgressPolicy, EgressBinding, "
            "EgressCredential, ActionPolicySet, ActionPolicyBinding), "
            "cluster-scoped and shared by every Agentplane namespace; the "
            "environment seeds, the egress proxy and the Action Service depend on "
            "this."
        ),
    )
