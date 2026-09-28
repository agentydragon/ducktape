"""The cluster-global `public-coder-agent-backups` S3Identity that public-coder-agent's backup
credentials reference."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.seaweedfs.s3 import Identity
from cluster.cdk8s.seaweedfs import cluster, namespace as seaweedfs_namespace

NAME = "public-coder-agent-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/public-coder-agent-backups-bucket"
_CHART = "public-coder-agent-backups-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    Identity(
        chart,
        "identity",
        name=NAME,
        namespace=seaweedfs_namespace.NAME,
        cluster_name=cluster.NAME,
        cluster_namespace=seaweedfs_namespace.NAME,
    )
    return chart


def seaweedfs_public_coder_agent_backups_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-public-coder-agent-backups-bucket"
    return flux_kustomization(
        chart, name, directory, depends_on=[flux_kustomization_depends_on(seaweedfs_operator)], timeout="5m"
    )
