"""SeaweedFS operator S3 objects: Bucket, S3Identity, S3Credentials and the
ResourceReferenceGrants that let another namespace's objects reference them.

Operator behaviour these encode (`cluster/skills/seaweed_operator/SKILL.md`):

- A Bucket's physical name is its `spec.name`; it is always the CR's own name.
- An `S3Identity` claims a cluster-global IAM name, so it lives in the SeaweedFS namespace.
- `S3Credentials` resolves `identityRef` literally when no same-namespace `S3Identity`
  exists, so it is named after the identity it holds a key for. It always retains its key
  and Secret: its CRD defaults to `Delete`.
- A Bucket or S3Credentials outside the SeaweedFS namespace needs a grant there to
  reference the `Seaweed` cluster (`tenant_bucket` always creates one); a cross-namespace
  `secretRef` needs a grant in the Secret's namespace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from constructs import Construct
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

from cluster.cdk8s.metadata import metadata

# Aliased: the helpers' own `namespace` parameter is the tenant's.
from cluster.cdk8s.seaweedfs import cluster, namespace as seaweedfs_namespace

GROUP = "seaweed.seaweedfs.com"
READ_WRITE = (
    BucketSpecAccessActions.READ,
    BucketSpecAccessActions.WRITE,
    BucketSpecAccessActions.LIST,
    BucketSpecAccessActions.TAGGING,
)
READ_ONLY = (BucketSpecAccessActions.READ, BucketSpecAccessActions.LIST)


@dataclass(frozen=True)
class SecretKeyFields:
    """The Secret keys S3Credentials writes the key pair to."""

    access_key: str
    secret_key: str


AWS_ENV_KEY_FIELDS = SecretKeyFields(access_key="AWS_ACCESS_KEY_ID", secret_key="AWS_SECRET_ACCESS_KEY")


def bucket(
    scope: Construct,
    *,
    name: str,
    namespace: str,
    access: Mapping[str, Sequence[BucketSpecAccessActions]],
    adopt_existing: bool,
    reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
    description: str | None = None,
) -> Bucket:
    """`access` maps IAM identity to its actions. `adopt_existing` takes over a physical
    bucket that already exists instead of failing with `BucketAlreadyExists`.
    `reclaim_policy=None` leaves the CRD default (`Retain`)."""
    return Bucket(
        scope,
        f"bucket-{namespace}-{name}",
        metadata=metadata(name, namespace, annotations={"description": description} if description else None),
        spec=BucketSpec(
            name=name,
            adopt_existing=adopt_existing or None,
            cluster_ref=BucketSpecClusterRef(name=cluster.NAME, namespace=seaweedfs_namespace.NAME),
            reclaim_policy=reclaim_policy,
            access=[BucketSpecAccess(user=user, actions=list(actions)) for user, actions in access.items()],
        ),
    )


def identity(scope: Construct, name: str) -> S3Identity:
    return S3Identity(
        scope,
        f"s3-identity-{name}",
        metadata=metadata(name, seaweedfs_namespace.NAME),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )


def credentials(
    scope: Construct,
    *,
    identity: str,
    namespace: str,
    secret: str,
    key_fields: SecretKeyFields | None,
    secret_namespace: str | None = None,
    description: str | None = None,
) -> S3Credentials:
    """A key for `identity`, mirrored into Secret `secret`. A same-namespace Secret is
    created and owned by the operator; one in `secret_namespace` must already exist and be
    granted (`secret_grant`). `key_fields=None` keeps the operator's `accessKey`/`secretKey`."""
    return S3Credentials(
        scope,
        f"s3-credentials-{namespace}-{identity}",
        metadata=metadata(identity, namespace, annotations={"description": description} if description else None),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(
                name=cluster.NAME, namespace=None if namespace == seaweedfs_namespace.NAME else seaweedfs_namespace.NAME
            ),
            identity_ref=S3CredentialsSpecIdentityRef(name=identity),
            secret_ref=S3CredentialsSpecSecretRef(
                name=secret,
                namespace=secret_namespace,
                access_key_field=key_fields.access_key if key_fields else None,
                secret_key_field=key_fields.secret_key if key_fields else None,
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )


def cluster_grant(scope: Construct, *, name: str, namespace: str) -> ResourceReferenceGrant:
    """Permits `namespace`'s Buckets and S3Credentials to reference the Seaweed cluster."""
    return ResourceReferenceGrant(
        scope,
        f"cluster-grant-{name}",
        metadata=metadata(name, seaweedfs_namespace.NAME),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=GROUP, kind="Bucket", namespace=namespace),
                ResourceReferenceGrantSpecFrom(group=GROUP, kind="S3Credentials", namespace=namespace),
            ],
            to=[ResourceReferenceGrantSpecTo(group=GROUP, kind="Seaweed", name=cluster.NAME)],
        ),
    )


def secret_grant(scope: Construct, *, secret: str, namespace: str) -> ResourceReferenceGrant:
    """Permits S3Credentials in the SeaweedFS namespace to populate Secret `secret` in `namespace`."""
    return ResourceReferenceGrant(
        scope,
        f"secret-grant-{namespace}-{secret}",
        metadata=metadata(secret, namespace),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=GROUP, kind="S3Credentials", namespace=seaweedfs_namespace.NAME)
            ],
            to=[ResourceReferenceGrantSpecTo(group="", kind="Secret", name=secret)],
        ),
    )


def tenant_bucket(
    scope: Construct,
    *,
    name: str,
    namespace: str,
    owns_identity: bool,
    secret: str,
    key_fields: SecretKeyFields | None,
    grant: str,
    reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
    bucket_description: str | None = None,
    credentials_description: str | None = None,
) -> None:
    """A tenant's private bucket in `namespace`: read-write for the IAM identity of the same
    name, whose key the operator mints into Secret `secret` there, plus the grant (named
    `grant`) that lets both reference the Seaweed cluster. The Bucket adopts an existing
    physical bucket of that name. `owns_identity` declares the cluster-global S3Identity
    here; otherwise another Kustomization owns it, or it predates the operator."""
    bucket(
        scope,
        name=name,
        namespace=namespace,
        access={name: READ_WRITE},
        adopt_existing=True,
        reclaim_policy=reclaim_policy,
        description=bucket_description,
    )
    if owns_identity:
        identity(scope, name)
    credentials(
        scope,
        identity=name,
        namespace=namespace,
        secret=secret,
        key_fields=key_fields,
        description=credentials_description,
    )
    cluster_grant(scope, name=grant, namespace=namespace)
