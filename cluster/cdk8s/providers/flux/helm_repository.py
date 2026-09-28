"""Ergonomic wrapper for Flux's `HelmRepository`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `HelmRepository(scope, id, ...)`.
Every keyword is a `HelmRepositorySpec` field under its own name and type; `None` leaves it
unset, so Flux's own default applies. No ducktape namespace or policy default (interval)
lives here -- those are `cluster.cdk8s.helm.helm_repository`'s own.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_source.io.fluxcd.toolkit.source import (
    HelmRepository as _HelmRepository,
    HelmRepositorySpec,
    HelmRepositorySpecAccessFrom,
    HelmRepositorySpecCertSecretRef,
    HelmRepositorySpecProvider,
    HelmRepositorySpecSecretRef,
    HelmRepositorySpecType,
)


class HelmRepository(_HelmRepository):
    """Flux's `HelmRepository`. `url` is the only field the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        url: str,
        type: HelmRepositorySpecType | None = None,
        interval: str | None = None,
        timeout: str | None = None,
        suspend: bool | None = None,
        provider: HelmRepositorySpecProvider | None = None,
        secret_ref: HelmRepositorySpecSecretRef | None = None,
        cert_secret_ref: HelmRepositorySpecCertSecretRef | None = None,
        pass_credentials: bool | None = None,
        insecure: bool | None = None,
        access_from: HelmRepositorySpecAccessFrom | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=HelmRepositorySpec(
                url=url,
                type=type,
                interval=interval,
                timeout=timeout,
                suspend=suspend,
                provider=provider,
                secret_ref=secret_ref,
                cert_secret_ref=cert_secret_ref,
                pass_credentials=pass_credentials,
                insecure=insecure,
                access_from=access_from,
            ),
        )
