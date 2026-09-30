"""Ergonomic wrapper for the SeaweedFS operator's `S3Identity`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `S3Identity(scope, id, ...)`.
Every keyword is an `S3IdentitySpec` field under its own name and type. No ducktape namespace or
Seaweed cluster name lives here.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity as _S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)


class S3Identity(_S3Identity):
    """SeaweedFS `S3Identity`, claiming the cluster-global IAM name `metadata.name` in the Seaweed
    cluster `seaweed_ref` names. `reclaim_policy=None` leaves the operator's default."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        seaweed_ref: S3IdentitySpecSeaweedRef,
        reclaim_policy: S3IdentitySpecReclaimPolicy | None,
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=S3IdentitySpec(seaweed_ref=seaweed_ref, reclaim_policy=reclaim_policy)
        )
