"""The anonymously readable `pr-visuals` bucket in `flux-system` and its writer identity and
credentials, plus the grant letting them reference the SeaweedFS cluster."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecAccessActions
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.seaweedfs import s3

NAME = "pr-visuals"
OUTPUT_DIR = "cluster/k8s/seaweedfs/pr-visuals-bucket"
_CHART = "pr-visuals-bucket"
_TENANT = "flux-system"
_WRITER = "pr-visuals-writer"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    s3.bucket(
        chart,
        name=NAME,
        namespace=_TENANT,
        access={_WRITER: s3.READ_WRITE, "anonymous": [BucketSpecAccessActions.READ]},
        # The existing bucket is being handed to the tenant-local CR.
        adopt_existing=True,
    )
    s3.identity(chart, _WRITER)
    s3.credentials(chart, identity=_WRITER, namespace=_TENANT, secret="pr-visuals-s3-credentials", key_fields=None)
    s3.cluster_grant(chart, name=NAME, namespace=_TENANT)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_pr_visuals_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-pr-visuals-bucket"
    return flux_kustomization(
        chart,
        name,
        artifact,
        interval="1h",
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
    )
