"""Flux Kustomizations for the cluster/k8s/agentplane-crds slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def agentplane_crds(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "agentplane-crds"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="2m",
        # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
        # for the other CRD Kustomizations (external-secrets-crds, snapshot-controller-crds).
        prune=False,
        description=(
            "Agentplane's own CRDs (EgressPolicy, EgressBinding, "
            "EgressCredential, ActionPolicySet, ActionPolicyBinding), "
            "cluster-scoped and shared by every Agentplane namespace; the "
            "environment seeds, the egress proxy and the Action Service depend on "
            "this."
        ),
    )
