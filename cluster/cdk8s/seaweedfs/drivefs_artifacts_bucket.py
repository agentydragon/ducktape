"""The DriveFS artifacts bucket. Its writer and reader identities, whose keys are managed
outside the cluster, are registered by `public_s3`."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.seaweedfs import namespace, s3

NAME = "drivefs-artifacts"
OUTPUT_DIR = "cluster/k8s/seaweedfs/drivefs-artifacts-bucket"
_CHART = "drivefs-artifacts-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    s3.bucket(
        chart,
        name=NAME,
        namespace=namespace.NAME,
        access={"drivefs-artifacts-writer": s3.READ_WRITE, "drivefs-artifacts-reader": s3.READ_ONLY},
        adopt_existing=False,
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_drivefs_artifacts_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-drivefs-artifacts-bucket"
    return flux_kustomization(
        chart, name, artifact, depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)], timeout="5m"
    )
