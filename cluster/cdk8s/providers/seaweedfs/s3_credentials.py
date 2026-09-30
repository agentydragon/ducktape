"""Ergonomic wrapper for the SeaweedFS operator's `S3Credentials`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `S3Credentials(scope, id, ...)`.
Every keyword is an `S3CredentialsSpec` field under its own name and type. No ducktape namespace,
Secret name or Seaweed cluster name lives here.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials as _S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)


class S3Credentials(_S3Credentials):
    """SeaweedFS `S3Credentials`: a key pair for the IAM identity `identity_ref` names, written
    into the Secret `secret_ref` names. `None` leaves a field to the operator's default; the CRD
    defaults `reclaimPolicy` to `Delete`, which removes the key and a same-namespace Secret the
    operator controls."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        seaweed_ref: S3CredentialsSpecSeaweedRef,
        identity_ref: S3CredentialsSpecIdentityRef,
        secret_ref: S3CredentialsSpecSecretRef | None,
        reclaim_policy: S3CredentialsSpecReclaimPolicy | None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=S3CredentialsSpec(
                seaweed_ref=seaweed_ref, identity_ref=identity_ref, secret_ref=secret_ref, reclaim_policy=reclaim_policy
            ),
        )
