"""This cluster's own `Bucket`/`Identity`/`IdentityRef`/`secret_grant`, bound to the one
deployed `Seaweed` cluster (`cluster.cdk8s.seaweedfs.cluster.NAME`/`namespace.NAME`). Generic
shape, grant machinery and operator behaviour: `cluster.cdk8s.providers.seaweedfs.s3`, whose
`SecretKeyFields`/`AWS_ENV_KEY_FIELDS` (no ducktape fact in them) callers import directly.
"""

from __future__ import annotations

from constructs import Construct
from seaweed_bucket_crds.com.seaweedfs.seaweed import BucketSpecReclaimPolicy
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import ResourceReferenceGrant

from cluster.cdk8s.providers.seaweedfs import s3 as _s3
from cluster.cdk8s.seaweedfs import cluster, namespace as seaweedfs_namespace


class IdentityRef(_s3.IdentityRef):
    """An IAM identity this chart does not declare: another Kustomization's `Identity`, or
    one that predates the operator. Bound to this cluster's deployed Seaweed cluster."""

    def __init__(self, scope: Construct, id: str, *, name: str) -> None:
        super().__init__(scope, id, name=name, cluster_name=cluster.NAME, cluster_namespace=seaweedfs_namespace.NAME)


class Identity(_s3.Identity):
    """Declares the S3Identity claiming IAM name `name`, in this cluster's deployed Seaweed
    cluster. Our policy: `namespace` defaults to the SeaweedFS namespace itself, since most
    identities are cluster-global rather than tenant-local."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str = seaweedfs_namespace.NAME,
        description: str | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            name=name,
            namespace=namespace,
            cluster_name=cluster.NAME,
            cluster_namespace=seaweedfs_namespace.NAME,
            description=description,
        )


class Bucket(_s3.Bucket):
    """A bucket of this cluster's deployed Seaweed cluster, named `name` both as a CR and
    physically. See `cluster.cdk8s.providers.seaweedfs.s3.Bucket` for `grant`/`grant_read`/
    `grant_read_write` and the other keywords' meaning."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        adopt_existing: bool,
        reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
        description: str | None = None,
        grant_name: str | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            name=name,
            namespace=namespace,
            cluster_name=cluster.NAME,
            cluster_namespace=seaweedfs_namespace.NAME,
            adopt_existing=adopt_existing,
            reclaim_policy=reclaim_policy,
            description=description,
            grant_name=grant_name,
        )


def secret_grant(scope: Construct, *, secret: str, namespace: str) -> ResourceReferenceGrant:
    """Permits S3Credentials in the SeaweedFS namespace to populate Secret `secret` in `namespace`."""
    return _s3.secret_grant(scope, secret=secret, namespace=namespace, cluster_namespace=seaweedfs_namespace.NAME)
