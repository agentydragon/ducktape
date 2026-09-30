"""The DriveFS artifacts bucket. Its writer and reader identities, whose keys are managed
outside the cluster, are registered by `public_s3`."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import namespace, s3

NAME = "drivefs-artifacts"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/drivefs-artifacts-bucket"
_CHART = "drivefs-artifacts-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    bucket = s3.Bucket(chart, "bucket", name=NAME, namespace=namespace.NAME, adopt_existing=False)
    bucket.grant_read_write("drivefs-artifacts-writer")
    bucket.grant_read("drivefs-artifacts-reader")
    return chart


def seaweedfs_drivefs_artifacts_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-drivefs-artifacts-bucket"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=[flux_kustomization_depends_on(seaweedfs_operator)],
        timeout="5m",
    )
