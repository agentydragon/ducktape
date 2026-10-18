"""Archived Restic recovery resources for the retired Haku OpenClaw spike.

The Retain bucket and S3 credentials remain reproducible alongside the Restic
repository configuration. The SOPS-encrypted Restic password beside the output
is hand-written and must remain unchanged.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecClusterRef, BucketSpecReclaimPolicy
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)

from cluster.cdk8s.manifest_roots import PARKED_ROOT
from cluster.cdk8s.providers.seaweedfs.bucket import Bucket, BucketAccess
from cluster.cdk8s.providers.seaweedfs.s3_credentials import S3Credentials
from cluster.cdk8s.restic_backup import ResticBackup

NAME = "haku-openclaw-spike-backup"
OUTPUT_DIR = f"{PARKED_ROOT}/haku-openclaw-spike/backup"
NAMESPACE = "haku-openclaw-spike"
_BUCKET = "haku-openclaw-spike-backups"
_IDENTITY = "haku-openclaw-spike-backups"
_S3_SECRET = "haku-openclaw-spike-volsync-s3-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Bucket(
        chart,
        "backup-bucket",
        metadata=ApiObjectMetadata(
            name=_BUCKET, namespace=NAMESPACE, annotations={"description": "Haku OpenClaw spike VolSync backup bucket."}
        ),
        cluster_ref=BucketSpecClusterRef(name="seaweedfs", namespace="seaweedfs"),
        access=[BucketAccess.read_write(_IDENTITY)],
        adopt_existing=True,
        reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
    )
    S3Credentials(
        chart,
        "backup-credentials",
        metadata=ApiObjectMetadata(
            name=_IDENTITY,
            namespace=NAMESPACE,
            annotations={"description": "Haku OpenClaw spike VolSync SeaweedFS credentials."},
        ),
        seaweed_ref=S3CredentialsSpecSeaweedRef(name="seaweedfs", namespace="seaweedfs"),
        identity_ref=S3CredentialsSpecIdentityRef(name=_IDENTITY),
        secret_ref=S3CredentialsSpecSecretRef(
            name=_S3_SECRET, access_key_field="AWS_ACCESS_KEY_ID", secret_key_field="AWS_SECRET_ACCESS_KEY"
        ),
        reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
    )
    # The source is the optiplex home worker PVC (local-path-home-ssd). No mover affinity: the
    # Direct mover mounts that already-bound PVC, so its PV's node affinity places the mover and
    # its WaitForFirstConsumer cache on optiplex. A restore into an unbound PVC is the opposite
    # case: there the mover's placement chooses the node, so it must be pinned.
    ResticBackup(
        chart,
        "backup",
        namespace=NAMESPACE,
        name="haku-openclaw-spike-state-restic",
        repository="haku-openclaw-spike-volsync-restic",
        source_pvc="haku-openclaw-spike-state-v2",
        bucket=_BUCKET,
        s3_credentials=_S3_SECRET,
        # 03:23 UTC, away from interactive use and staggered from public-coder's 09:17.
        schedule="23 03 * * *",
        cache_storage_class_name="local-path-home-ssd",
    )
    return chart
