"""The loom-gym bucket, readable and writable by the claude-reader identity (`public_s3`)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import namespace, s3

NAME = "loom-gym"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/loom-gym-bucket"
_CHART = "loom-gym-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    s3.Bucket(chart, "bucket", name=NAME, namespace=namespace.NAME, adopt_existing=False).grant_read_write(
        "claude-reader"
    )
    return chart


def seaweedfs_loom_gym_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-loom-gym-bucket"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=[flux_kustomization_depends_on(seaweedfs_operator)],
        timeout="5m",
    )
