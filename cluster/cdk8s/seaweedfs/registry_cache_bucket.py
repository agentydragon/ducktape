"""The oci-cache registry's `registry-cache` bucket: a tenant-local Bucket and S3Credentials
in `oci-cache`, the cluster-global identity, and the grant letting them reference the
SeaweedFS cluster."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import s3

NAME = "registry-cache"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/registry-cache-bucket"
_CHART = "registry-cache-bucket"
_TENANT = "oci-cache"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    # The existing cache bucket was handed to the tenant-local CR.
    bucket = s3.Bucket(chart, "bucket", name=NAME, namespace=_TENANT, adopt_existing=True)
    identity = s3.Identity(chart, "identity", name=NAME)
    bucket.grant_read_write(identity)
    identity.credentials(namespace=_TENANT, secret="registry-cache-s3-credentials", key_fields=None)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_registry_cache_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-registry-cache-bucket"
    return flux_kustomization(
        chart, name, artifact, depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)], timeout="5m"
    )
