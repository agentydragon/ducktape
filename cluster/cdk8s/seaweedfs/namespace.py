"""The `seaweedfs` Namespace, the SeaweedFS cluster's and operator's home."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml

NAME = "seaweedfs"
OUTPUT_DIR = "cluster/k8s/seaweedfs/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            labels={"goldilocks.fairwinds.com/enabled": "false"},
            annotations={
                "description": (
                    "SeaweedFS object store on the OVH Kimsufi nodes, backed by the\n"
                    "Talos-declared UserVolumeConfig data disk on each node. Replication\n"
                    'strategy "001" (one copy on a different node, same rack).\n'
                    "See cluster/docs/kimsufi_provisioning.md and\n"
                    "cluster/docs/plans/ovh_storage_tiering.md.\n"
                )
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=["namespace.k8s.yaml"]))


def seaweedfs_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "seaweedfs-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
        ),
    )
