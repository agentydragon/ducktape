"""This cluster's SeaweedFS S3 objects: `bucket`, `identity` and `credentials` build the
`providers/seaweedfs` kinds against the one deployed `Seaweed` cluster, and `cluster_grant` and
`secret_grant` build the ResourceReferenceGrants that let another namespace's objects reference it
or its Secrets. Each object is built complete, once.

Operator behaviour these encode (`cluster/skills/seaweed_operator/SKILL.md`):

- An `S3Identity` claims a cluster-global IAM name; it lives in the SeaweedFS namespace
  unless its tenant keeps it local.
- `S3Credentials` resolves `identityRef` literally when no same-namespace `S3Identity`
  exists, so it is named after the identity it holds a key for. It always retains its key
  and Secret: its CRD defaults to `Delete`.
- A Bucket, S3Identity or S3Credentials outside the SeaweedFS namespace may reference the
  `Seaweed` cluster only through a grant there listing its kind (`cluster_grant`). A
  cross-namespace `secretRef` needs a grant in the Secret's namespace instead (`secret_grant`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from constructs import Construct
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecAccess, BucketSpecClusterRef, BucketSpecReclaimPolicy
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import S3IdentitySpecReclaimPolicy, S3IdentitySpecSeaweedRef

from cluster.cdk8s.providers.seaweedfs.bucket import Bucket
from cluster.cdk8s.providers.seaweedfs.resource_reference_grant import ResourceReferenceGrant
from cluster.cdk8s.providers.seaweedfs.s3_credentials import S3Credentials
from cluster.cdk8s.providers.seaweedfs.s3_identity import S3Identity

# Aliased: the functions' own `namespace` parameter is the tenant's.
from cluster.cdk8s.seaweedfs import cluster, namespace as seaweedfs_namespace

_GROUP = "seaweed.seaweedfs.com"


@dataclass(frozen=True)
class SecretKeyFields:
    """The Secret keys S3Credentials writes the key pair to."""

    access_key: str
    secret_key: str


AWS_ENV_KEY_FIELDS = SecretKeyFields(access_key="AWS_ACCESS_KEY_ID", secret_key="AWS_SECRET_ACCESS_KEY")


def _description(description: str | None) -> dict[str, str] | None:
    return {"description": description} if description else None


def _seaweed_ref_namespace(namespace: str) -> str | None:
    """A reference to the Seaweed cluster names its namespace only from outside it."""
    return None if namespace == seaweedfs_namespace.NAME else seaweedfs_namespace.NAME


def bucket(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    access: Sequence[BucketSpecAccess],
    adopt_existing: bool,
    reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
    description: str | None = None,
) -> Bucket:
    """Bucket `name` of the Seaweed cluster, with `access` its whole access list.
    `adopt_existing` takes over a physical bucket that already exists instead of failing with
    `BucketAlreadyExists`. Our policy: `Retain`; `reclaim_policy=None` leaves the CRD default,
    also `Retain`."""
    return Bucket(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations=_description(description)),
        cluster_ref=BucketSpecClusterRef(name=cluster.NAME, namespace=seaweedfs_namespace.NAME),
        access=access,
        adopt_existing=adopt_existing,
        reclaim_policy=reclaim_policy,
    )


def identity(
    scope: Construct, id: str, *, name: str, namespace: str = seaweedfs_namespace.NAME, description: str | None = None
) -> S3Identity:
    """The S3Identity claiming IAM name `name`. Our policy: `Retain`."""
    return S3Identity(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations=_description(description)),
        seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME, namespace=_seaweed_ref_namespace(namespace)),
        reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN,
    )


def credentials(
    scope: Construct,
    id: str,
    *,
    identity: str,
    namespace: str,
    secret: str,
    key_fields: SecretKeyFields | None,
    secret_namespace: str | None = None,
    description: str | None = None,
) -> S3Credentials:
    """A key for IAM identity `identity`, mirrored into Secret `secret`. A same-namespace Secret
    is created and owned by the operator; one in `secret_namespace` must already exist and be
    granted (`secret_grant`). `key_fields=None` keeps `accessKey`/`secretKey`. Our policy:
    `Retain`."""
    return S3Credentials(
        scope,
        id,
        metadata=ApiObjectMetadata(name=identity, namespace=namespace, annotations=_description(description)),
        seaweed_ref=S3CredentialsSpecSeaweedRef(name=cluster.NAME, namespace=_seaweed_ref_namespace(namespace)),
        identity_ref=S3CredentialsSpecIdentityRef(name=identity),
        secret_ref=S3CredentialsSpecSecretRef(
            name=secret,
            namespace=secret_namespace,
            access_key_field=key_fields.access_key if key_fields else None,
            secret_key_field=key_fields.secret_key if key_fields else None,
        ),
        reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
    )


def cluster_grant(
    scope: Construct, id: str, *, name: str, namespace: str, kinds: Sequence[str]
) -> ResourceReferenceGrant:
    """Grant `name`, letting objects of `kinds` in `namespace` reference the Seaweed cluster."""
    return ResourceReferenceGrant(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=seaweedfs_namespace.NAME),
        from_=[ResourceReferenceGrantSpecFrom(group=_GROUP, kind=kind, namespace=namespace) for kind in kinds],
        to=[ResourceReferenceGrantSpecTo(group=_GROUP, kind="Seaweed", name=cluster.NAME)],
    )


def secret_grant(scope: Construct, *, secret: str, namespace: str) -> ResourceReferenceGrant:
    """Permits S3Credentials in the SeaweedFS namespace to populate Secret `secret` in `namespace`."""
    return ResourceReferenceGrant(
        scope,
        f"secret-grant-{namespace}-{secret}",
        metadata=ApiObjectMetadata(name=secret, namespace=namespace),
        from_=[ResourceReferenceGrantSpecFrom(group=_GROUP, kind="S3Credentials", namespace=seaweedfs_namespace.NAME)],
        to=[ResourceReferenceGrantSpecTo(group="", kind="Secret", name=secret)],
    )
