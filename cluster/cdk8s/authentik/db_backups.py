"""The SeaweedFS storage for Authentik's CNPG backups, rendered into
`cluster/k8s/authentik/db-backups`.

The hand-written `kustomization.yaml` there lists this file beside `object-store.yaml` (the
barman-cloud `ObjectStore`) and `scheduled-backup.yaml` (the CNPG `ScheduledBackup`), which
have no CRD binding yet.
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

NAME = "authentik-db-backups"
NAMESPACE = "authentik"
OUTPUT_DIR = "cluster/k8s/authentik/db-backups"
_SEAWEED = "seaweedfs"
_SEAWEED_NAMESPACE = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    Bucket(
        chart,
        "bucket",
        metadata=metadata(
            NAME, NAMESPACE, annotations={"description": "Private CNPG physical backups and WAL archive for Authentik."}
        ),
        spec=BucketSpec(
            name=NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=NAME,
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
    identity = S3Identity(
        chart,
        "identity",
        metadata=metadata(
            NAME, NAMESPACE, annotations={"description": "Dedicated SeaweedFS identity for Authentik CNPG backups."}
        ),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN,
        ),
    )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(NAME, NAMESPACE),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            identity_ref=S3CredentialsSpecIdentityRef(name=identity.name),
            # The operator creates and owns this Secret in the credential's namespace, where the
            # ObjectStore in `object-store.yaml` consumes it.
            secret_ref=S3CredentialsSpecSecretRef(
                name="authentik-db-backup-s3",
                access_key_field="AWS_ACCESS_KEY_ID",
                secret_key_field="AWS_SECRET_ACCESS_KEY",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permits only the backup resources above to reference the Seaweed cluster.
    ResourceReferenceGrant(
        chart,
        "reference-grant",
        metadata=metadata(NAME, _SEAWEED_NAMESPACE),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind=kind, namespace=NAMESPACE)
                for kind in ("S3Identity", "S3Credentials", "Bucket")
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEED)],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
