"""The oci-cache registry's `registry-cache` bucket: a tenant-local Bucket, S3Identity and
S3Credentials in `oci-cache`, and the grant letting them reference the SeaweedFS cluster."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
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
    identity = s3.Identity(chart, "identity", name=NAME, namespace=_TENANT)
    bucket.grant_read_write(identity)
    identity.credentials(namespace=_TENANT, secret="registry-cache-s3-credentials", key_fields=None)
    return chart


def seaweedfs_registry_cache_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-registry-cache-bucket"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=[flux_kustomization_depends_on(seaweedfs_operator)],
        timeout="5m",
    )
