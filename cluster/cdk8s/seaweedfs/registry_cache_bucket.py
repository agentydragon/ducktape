"""The oci-cache registry's `registry-cache` bucket, a `PrivateBucket` in `oci-cache`."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import s3

NAME = "registry-cache"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/registry-cache-bucket"
_CHART = "registry-cache-bucket"
_TENANT = "oci-cache"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    s3.PrivateBucket(
        chart,
        "storage",
        name=NAME,
        tenant=_TENANT,
        # The existing cache bucket was handed to the tenant-local CR.
        adopt_existing=True,
        description="Zot's OCI pull-through cache: manifests and blobs.",
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
