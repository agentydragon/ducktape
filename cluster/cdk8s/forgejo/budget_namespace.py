"""The budget Namespace and the `budget-namespace` Flux Kustomization owning it."""

from __future__ import annotations

from pathlib import Path

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_namespace, write_yaml

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
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=["namespace.k8s.yaml"]))


def budget_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            path=artifact_path(artifact),
            prune=False,
            source_ref=artifact_source_ref(artifact),
            timeout="1m",
        ),
    )
