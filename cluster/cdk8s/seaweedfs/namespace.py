"""The `seaweedfs` Namespace, the SeaweedFS cluster's and operator's home."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "seaweedfs"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/seaweedfs/namespace"


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


def seaweedfs_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "seaweedfs-namespace"
    return flux_kustomization(chart, name, artifact, retry_interval=None, wait=None, suspend=False)
