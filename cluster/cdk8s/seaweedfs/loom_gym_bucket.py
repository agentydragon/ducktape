"""The loom-gym bucket, readable and writable by the claude-reader identity (`public_s3`)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import cluster, namespace

NAME = "loom-gym"
OUTPUT_DIR = "cluster/k8s/seaweedfs/loom-gym-bucket"
_CHART = "loom-gym-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    Bucket(
        chart,
        "bucket",
        metadata=metadata(NAME, namespace.NAME),
        spec=BucketSpec(
            name=NAME,
            cluster_ref=BucketSpecClusterRef(name=cluster.NAME, namespace=namespace.NAME),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user="claude-reader",
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_loom_gym_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-loom-gym-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name=NAME, namespace=namespace.NAME
                )
            ],
        ),
    )
