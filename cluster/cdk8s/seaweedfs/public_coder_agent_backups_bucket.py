"""The cluster-global `public-coder-agent-backups` S3Identity that public-coder-agent's backup
credentials reference."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import cluster, namespace

NAME = "public-coder-agent-backups"
OUTPUT_DIR = "cluster/k8s/seaweedfs/public-coder-agent-backups-bucket"
_CHART = "public-coder-agent-backups-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    S3Identity(
        chart,
        "identity",
        metadata=metadata(NAME, namespace.NAME),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_public_coder_agent_backups_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-public-coder-agent-backups-bucket"
    return flux_kustomization(
        chart, name, artifact, depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)], timeout="5m"
    )
