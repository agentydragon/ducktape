"""The `seaweedfs` Namespace, the SeaweedFS cluster's and operator's home."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "seaweedfs"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.DISABLED,
        agent_readable=None,
        annotations={
            "description": (
                "SeaweedFS object store on the OVH Kimsufi nodes, backed by the\n"
                "Talos-declared UserVolumeConfig data disk on each node. Replication\n"
                'strategy "001" (one copy on a different node, same rack).\n'
                "See cluster/docs/kimsufi_provisioning.md and\n"
                "cluster/docs/plans/ovh_storage_tiering.md.\n"
            )
        },
    )
    return chart


def seaweedfs_namespace(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    name = "seaweedfs-namespace"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        retry_interval=None,
        wait=None,
        suspend=False,
    )
