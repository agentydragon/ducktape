"""Ergonomic wrappers for SeaweedFS operator's `Bucket` and `S3Identity`, following
cdk8s-plus's own construction pattern: a class named after the kind. No ducktape
namespace, secret name or topology fact lives here: the deployed `Seaweed` cluster's own
name and namespace (`cluster_name`/`cluster_namespace`) are required parameters with no
default.

Operator behaviour these encode (`cluster/skills/seaweed_operator/SKILL.md`):

- A Bucket's physical name is its `spec.name`; it is always the CR's own name.
- An `S3Identity` claims a cluster-global IAM name; whether it lives in the Seaweed
  cluster's own namespace or a tenant's is the caller's choice (`namespace`).

Cross-namespace access grants and `S3Credentials` minting are a separate follow-up; a
Bucket or Identity outside the Seaweed cluster's own namespace renders schema-valid YAML
here, but the operator will reject it without a `ResourceReferenceGrant` this module does
not yet build.
"""

from __future__ import annotations

from dataclasses import dataclass

from constructs import Construct
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket as _Bucket,
    BucketSpec,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)

from cluster.cdk8s.metadata import metadata


@dataclass(frozen=True)
class SecretKeyFields:
    """The Secret keys S3Credentials writes the key pair to."""

    access_key: str
    secret_key: str


AWS_ENV_KEY_FIELDS = SecretKeyFields(access_key="AWS_ACCESS_KEY_ID", secret_key="AWS_SECRET_ACCESS_KEY")


def _description(description: str | None) -> dict[str, str] | None:
    return {"description": description} if description else None


def _seaweed_ref_namespace(namespace: str, cluster_namespace: str) -> str | None:
    """A reference to the Seaweed cluster names its namespace only from outside it."""
    return None if namespace == cluster_namespace else cluster_namespace


class Identity(Construct):
    """Declares the S3Identity claiming IAM name `name`. `cluster_name`/`cluster_namespace`
    identify the Seaweed cluster it belongs to."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        cluster_name: str,
        cluster_namespace: str,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.name = name
        self._resource = S3Identity(
            self,
            "Resource",
            metadata=metadata(name, namespace, annotations=_description(description)),
            spec=S3IdentitySpec(
                seaweed_ref=S3IdentitySpecSeaweedRef(
                    name=cluster_name, namespace=_seaweed_ref_namespace(namespace, cluster_namespace)
                ),
                reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN,
            ),
        )


class Bucket(Construct):
    """A bucket of the Seaweed cluster, named `name` both as a CR and physically.

    `cluster_name`/`cluster_namespace` identify the Seaweed cluster it belongs to.
    `adopt_existing` takes over a physical bucket that already exists instead of failing
    with `BucketAlreadyExists`; `reclaim_policy=None` leaves the CRD default (`Retain`)."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        cluster_name: str,
        cluster_namespace: str,
        adopt_existing: bool,
        reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        self._resource = _Bucket(
            self,
            "Resource",
            metadata=metadata(name, namespace, annotations=_description(description)),
            spec=BucketSpec(
                name=name,
                adopt_existing=adopt_existing or None,
                cluster_ref=BucketSpecClusterRef(name=cluster_name, namespace=cluster_namespace),
                reclaim_policy=reclaim_policy,
            ),
        )
