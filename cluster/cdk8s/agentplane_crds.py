"""Agentplane's own CRDs, authored in the directory Flux packages directly.
`bb run //agentplane/crds:generate_bin` keeps their derived subject schemas current."""

from __future__ import annotations

from cdk8s import Chart

from agentplane.crds.generate import CRDS_DIR
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization

NAME = "agentplane-crds"
OUTPUT_DIR = CRDS_DIR.as_posix()


def agentplane_crds(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
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
