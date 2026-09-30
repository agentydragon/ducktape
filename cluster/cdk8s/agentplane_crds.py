"""Agentplane's own CRDs, copied verbatim from `agentplane/crds`, where
`bb run //agentplane/crds:generate_bin` keeps their derived subject schemas current."""

from __future__ import annotations

from pathlib import Path

from cdk8s import Chart

from agentplane.crds.generate import CRD_FILES, CRDS_DIR
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.generation import copy_source_file
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agentplane-crds"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"


def copy_crds(root: Path) -> list[str]:
    """Copy each CRD into `OUTPUT_DIR`; return the names its `kustomization.yaml` lists."""
    return [copy_source_file(root, OUTPUT_DIR, (CRDS_DIR / name).as_posix()) for name in CRD_FILES]


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
