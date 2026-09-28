"""Ergonomic wrappers for External Secrets Operator's `SecretStore` and `ClusterSecretStore`,
following cdk8s-plus's own construction pattern: a class named after the kind, constructed as
`SecretStore(scope, id, ...)`. Every keyword is a `<Kind>Spec` field under its own name and type;
`None` leaves it unset, so ESO's own default applies. No ducktape namespace, service account or
CA location lives here -- those are `cluster.cdk8s.external_secrets.kubernetes_store`'s own.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStore as _ClusterSecretStore,
    ClusterSecretStoreSpec,
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecRefreshInterval,
    ClusterSecretStoreSpecRetrySettings,
)
from external_secrets_secretstore_crds.io.external_secrets import (
    SecretStore as _SecretStore,
    SecretStoreSpec,
    SecretStoreSpecConditions,
    SecretStoreSpecProvider,
    SecretStoreSpecRefreshInterval,
    SecretStoreSpecRetrySettings,
)


class ClusterSecretStore(_ClusterSecretStore):
    """ESO's cluster-scoped `ClusterSecretStore`: `metadata` carries no `namespace`. `provider`
    is the only field the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        provider: ClusterSecretStoreSpecProvider,
        conditions: Sequence[ClusterSecretStoreSpecConditions] | None = None,
        controller: str | None = None,
        refresh_interval: ClusterSecretStoreSpecRefreshInterval | None = None,
        retry_settings: ClusterSecretStoreSpecRetrySettings | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ClusterSecretStoreSpec(
                provider=provider,
                conditions=list(conditions) if conditions is not None else None,
                controller=controller,
                refresh_interval=refresh_interval,
                retry_settings=retry_settings,
            ),
        )


class SecretStore(_SecretStore):
    """ESO's namespaced `SecretStore`, usable only by `ExternalSecret`s in its own namespace.
    `provider` is the only field the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        provider: SecretStoreSpecProvider,
        conditions: Sequence[SecretStoreSpecConditions] | None = None,
        controller: str | None = None,
        refresh_interval: SecretStoreSpecRefreshInterval | None = None,
        retry_settings: SecretStoreSpecRetrySettings | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=SecretStoreSpec(
                provider=provider,
                conditions=list(conditions) if conditions is not None else None,
                controller=controller,
                refresh_interval=refresh_interval,
                retry_settings=retry_settings,
            ),
        )
