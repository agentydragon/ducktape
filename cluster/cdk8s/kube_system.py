"""The kube-system Namespace's labels: Goldilocks recommendations, applied on pod creation."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts

NAME = "kube-system"
OUTPUT_DIR = "cluster/k8s/kube-system"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            # Kyverno's default resourceFilters exclude kube-system, so label-driven
            # diagnostics readers cannot create RoleBindings here. Keep both generic
            # agent and public-coder opt-ins absent until we intentionally add an
            # explicit binding or a narrowly scoped resource-filter exception.
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "initial"},
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def kube_system(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, goldilocks: Kustomization) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, wait=None, depends_on=[flux_kustomization_depends_on(goldilocks)])
