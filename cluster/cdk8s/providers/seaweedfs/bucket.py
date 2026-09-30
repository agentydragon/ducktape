"""Ergonomic wrapper for the SeaweedFS operator's `Bucket`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `Bucket(scope, id, ...)`.
Every keyword is a `BucketSpec` field under its own name and type. No ducktape namespace or
Seaweed cluster name lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket as _Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)


class BucketAccess:
    """`spec.access` entries for the action sets granted at more than one site."""

    @staticmethod
    def read_write(user: str) -> BucketSpecAccess:
        return BucketSpecAccess(
            user=user,
            actions=[
                BucketSpecAccessActions.READ,
                BucketSpecAccessActions.WRITE,
                BucketSpecAccessActions.LIST,
                BucketSpecAccessActions.TAGGING,
            ],
        )

    @staticmethod
    def read(user: str) -> BucketSpecAccess:
        return BucketSpecAccess(user=user, actions=[BucketSpecAccessActions.READ, BucketSpecAccessActions.LIST])


class Bucket(_Bucket):
    """SeaweedFS `Bucket`, physically named `metadata.name`: the operator selects the bucket by
    `spec.name`. An empty `access`, `adopt_existing=False` and `reclaim_policy=None` leave their
    fields unset, so the operator's defaults apply."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        cluster_ref: BucketSpecClusterRef,
        access: Sequence[BucketSpecAccess],
        adopt_existing: bool,
        reclaim_policy: BucketSpecReclaimPolicy | None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=BucketSpec(
                name=metadata.name,
                cluster_ref=cluster_ref,
                access=list(access) or None,
                adopt_existing=adopt_existing or None,
                reclaim_policy=reclaim_policy,
            ),
        )
