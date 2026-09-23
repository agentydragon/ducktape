"""The anonymously readable `pr-visuals` bucket in `flux-system` and its writer identity and
credentials, plus the grant letting them reference the SeaweedFS cluster."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import cluster, namespace

NAME = "pr-visuals"
OUTPUT_DIR = "cluster/k8s/seaweedfs/pr-visuals-bucket"
_CHART = "pr-visuals-bucket"
_TENANT = "flux-system"
_WRITER = "pr-visuals-writer"
_GROUP = "seaweed.seaweedfs.com"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    Bucket(
        chart,
        "bucket",
        metadata=metadata(NAME, _TENANT),
        spec=BucketSpec(
            name=NAME,
            # The existing bucket is being handed to the tenant-local CR.
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=cluster.NAME, namespace=namespace.NAME),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=_WRITER,
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                ),
                BucketSpecAccess(user="anonymous", actions=[BucketSpecAccessActions.READ]),
            ],
        ),
    )
    S3Identity(
        chart,
        "identity",
        metadata=metadata(_WRITER, namespace.NAME),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(_WRITER, _TENANT),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=cluster.NAME, namespace=namespace.NAME),
            identity_ref=S3CredentialsSpecIdentityRef(name=_WRITER),
            secret_ref=S3CredentialsSpecSecretRef(name="pr-visuals-s3-credentials"),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit the tenant-local Bucket and S3Credentials to reference the SeaweedFS cluster in
    # its namespace.
    ResourceReferenceGrant(
        chart,
        "grant",
        metadata=metadata(NAME, namespace.NAME),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_GROUP, kind="Bucket", namespace=_TENANT),
                ResourceReferenceGrantSpecFrom(group=_GROUP, kind="S3Credentials", namespace=_TENANT),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_GROUP, kind="Seaweed", name=cluster.NAME)],
        ),
    )
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
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name=NAME, namespace=_TENANT
            ),
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name=_WRITER, namespace=_TENANT
            ),
        ],
    )
