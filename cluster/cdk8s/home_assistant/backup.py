"""Home Assistant's backup bucket: the SeaweedFS identity, tenant-local Bucket and S3Credentials,
and the grant letting them reference the SeaweedFS cluster.

Hand-written beside the generated output: `repository-secret-store.yaml` (no namespaced
`SecretStore` binding), `credentials-secret.sops.yaml` and the `kustomization.yaml` listing them.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
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

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/home-assistant/backup"
_NAME = "home-assistant-backups"
_NAMESPACE = "home-assistant"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    S3Identity(
        chart,
        "s3-identity",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    Bucket(
        chart,
        "bucket",
        metadata=metadata(
            _NAME, _NAMESPACE, annotations={"description": "Home Assistant's tenant-local SeaweedFS backup bucket."}
        ),
        spec=BucketSpec(
            name=_NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=_NAME,
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                )
            ],
        ),
    )
    S3Credentials(
        chart,
        "s3-credentials",
        metadata=metadata(
            _NAME,
            _NAMESPACE,
            annotations={"description": "Home Assistant's tenant-local SeaweedFS backup credentials."},
        ),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            identity_ref=S3CredentialsSpecIdentityRef(name=_NAME),
            secret_ref=S3CredentialsSpecSecretRef(
                name="home-assistant-seaweedfs-credentials",
                access_key_field="AWS_ACCESS_KEY_ID",
                secret_key_field="AWS_SECRET_ACCESS_KEY",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Home Assistant's tenant-local Bucket and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        chart,
        "reference-grant",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
