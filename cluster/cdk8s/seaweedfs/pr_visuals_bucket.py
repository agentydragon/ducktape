"""The anonymously readable `pr-visuals` bucket in `flux-system` and its writer identity and
credentials, plus the grant letting them reference the SeaweedFS cluster."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecAccessActions

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import s3

NAME = "pr-visuals"
OUTPUT_DIR = f"{GENERATED_ROOT}/seaweedfs/pr-visuals-bucket"
_CHART = "pr-visuals-bucket"
# TODO: move the Bucket, S3Credentials and S3Identity into a dedicated `pr-visuals` namespace.
#   flux-system is only where tf/gitops/github-secrets-sync reads `pr-visuals-s3-credentials`
#   into the PR_VISUALS_* repo secrets, and tf-runner reads Secrets in any namespace. The moved
#   S3Credentials is a new CR with a new key: let github-secrets-sync publish it before the old
#   CR is pruned, or pr-visuals-publish runs hold a dead key in between.
_TENANT = "flux-system"
_WRITER = "pr-visuals-writer"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    # The existing bucket is being handed to the tenant-local CR.
    bucket = s3.Bucket(chart, "bucket", name=NAME, namespace=_TENANT, adopt_existing=True)
    writer = s3.Identity(chart, "writer", name=_WRITER, namespace=_TENANT)
    bucket.grant_read_write(writer)
    bucket.grant("anonymous", BucketSpecAccessActions.READ)
    writer.credentials(namespace=_TENANT, secret="pr-visuals-s3-credentials", key_fields=None)
    return chart


def seaweedfs_pr_visuals_bucket(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-pr-visuals-bucket"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        interval="1h",
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(seaweedfs_operator)],
    )
