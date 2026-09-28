"""Ergonomic wrapper for Flux's `GitRepository`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `GitRepository(scope, id, ...)`.
Every keyword is a `GitRepositorySpec` field under its own name and type; `None` leaves it
unset, so Flux's own default applies. No ducktape repository, namespace or credential
lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_gitrepository_crds.io.fluxcd.toolkit.source import (
    GitRepository as _GitRepository,
    GitRepositorySpec,
    GitRepositorySpecInclude,
    GitRepositorySpecProvider,
    GitRepositorySpecProxySecretRef,
    GitRepositorySpecRef,
    GitRepositorySpecSecretRef,
    GitRepositorySpecVerify,
)


class GitRepository(_GitRepository):
    """Flux's `GitRepository`. `url` and `interval` are the only fields the CRD itself requires.
    Every `GitRepositorySpec` field is a keyword except `serviceAccountName`, which the CRD
    accepts only with the `aws` or `azure` provider.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        url: str,
        interval: str,
        ref: GitRepositorySpecRef | None = None,
        provider: GitRepositorySpecProvider | None = None,
        secret_ref: GitRepositorySpecSecretRef | None = None,
        proxy_secret_ref: GitRepositorySpecProxySecretRef | None = None,
        sparse_checkout: Sequence[str] | None = None,
        ignore: str | None = None,
        include: Sequence[GitRepositorySpecInclude] | None = None,
        recurse_submodules: bool | None = None,
        verify: GitRepositorySpecVerify | None = None,
        timeout: str | None = None,
        suspend: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GitRepositorySpec(
                url=url,
                interval=interval,
                ref=ref,
                provider=provider,
                secret_ref=secret_ref,
                proxy_secret_ref=proxy_secret_ref,
                sparse_checkout=list(sparse_checkout) if sparse_checkout is not None else None,
                ignore=ignore,
                include=list(include) if include is not None else None,
                recurse_submodules=recurse_submodules,
                verify=verify,
                timeout=timeout,
                suspend=suspend,
            ),
        )
