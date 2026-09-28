"""The oci-cache registry's `registry-cache` bucket: a tenant-local Bucket, S3Identity and
S3Credentials in `oci-cache`, and the grant letting them reference the SeaweedFS cluster."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.seaweedfs.bucket import BucketAccess
from cluster.cdk8s.seaweedfs import s3

NAME = "registry-cache"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/registry-cache-bucket"
_CHART = "registry-cache-bucket"
_TENANT = "oci-cache"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    # The existing cache bucket was handed to the tenant-local CR.
    s3.bucket(
        chart, "bucket", name=NAME, namespace=_TENANT, access=[BucketAccess.read_write(NAME)], adopt_existing=True
    )
    s3.cluster_grant(chart, "grant", name=NAME, namespace=_TENANT, kinds=["Bucket", "S3Identity", "S3Credentials"])
    identity = s3.identity(chart, "identity", name=NAME, namespace=_TENANT)
    s3.credentials(
        chart,
        "credentials",
        identity=identity.name,
        namespace=_TENANT,
        secret="registry-cache-s3-credentials",
        key_fields=None,
    )
    return chart


def seaweedfs_registry_cache_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-registry-cache-bucket"
    return flux_kustomization(
        chart, name, directory, depends_on=[flux_kustomization_depends_on(seaweedfs_operator)], timeout="5m"
    )
