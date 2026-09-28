"""Ergonomic wrapper for Flux's `ImageRepository`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `ImageRepository(scope, id, ...)`.
Every keyword is an `ImageRepositorySpec` field under its own name and type; `None` leaves it
unset, so Flux's own default applies. No ducktape registry, namespace or credential lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_imagerepository_crds.io.fluxcd.toolkit.image import (
    ImageRepository as _ImageRepository,
    ImageRepositorySpec,
    ImageRepositorySpecAccessFrom,
    ImageRepositorySpecCertSecretRef,
    ImageRepositorySpecProvider,
    ImageRepositorySpecProxySecretRef,
    ImageRepositorySpecSecretRef,
)


class ImageRepository(_ImageRepository):
    """Flux's `ImageRepository`. `image` and `interval` are the only fields the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        image: str,
        interval: str,
        secret_ref: ImageRepositorySpecSecretRef | None = None,
        provider: ImageRepositorySpecProvider | None = None,
        service_account_name: str | None = None,
        cert_secret_ref: ImageRepositorySpecCertSecretRef | None = None,
        proxy_secret_ref: ImageRepositorySpecProxySecretRef | None = None,
        insecure: bool | None = None,
        exclusion_list: Sequence[str] | None = None,
        timeout: str | None = None,
        suspend: bool | None = None,
        access_from: ImageRepositorySpecAccessFrom | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ImageRepositorySpec(
                image=image,
                interval=interval,
                secret_ref=secret_ref,
                provider=provider,
                service_account_name=service_account_name,
                cert_secret_ref=cert_secret_ref,
                proxy_secret_ref=proxy_secret_ref,
                insecure=insecure,
                exclusion_list=list(exclusion_list) if exclusion_list is not None else None,
                timeout=timeout,
                suspend=suspend,
                access_from=access_from,
            ),
        )
