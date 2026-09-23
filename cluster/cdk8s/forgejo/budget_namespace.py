"""The budget Namespace and the `budget-namespace` Flux Kustomization owning it."""

from __future__ import annotations

from pathlib import Path

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_namespace

OUTPUT_DIR = "cluster/k8s/forgejo/budget-namespace"


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        OUTPUT_DIR,
        name="budget",
        labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
        annotations={
            # The suspended parked/budget Kustomization may still have this namespace in
            # its inventory. Keep that stale inventory from pruning the active namespace.
            "kustomize.toolkit.fluxcd.io/prune": "disabled"
        },
    )


def budget_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart, name, artifact, retry_interval=None, wait=None, interval="1h", prune=False, timeout="1m"
    )
