"""Builds the `ExternalSecret`s that copy or generate a Secret in a consumer namespace."""

from __future__ import annotations

from collections.abc import Sequence

from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s.metadata import metadata


def cluster_secret_store(name: str) -> ExternalSecretSpecSecretStoreRef:
    return ExternalSecretSpecSecretStoreRef(kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE, name=name)


def secret_store(name: str) -> ExternalSecretSpecSecretStoreRef:
    """A namespace-local `SecretStore` in the `ExternalSecret`'s own namespace."""
    return ExternalSecretSpecSecretStoreRef(kind=ExternalSecretSpecSecretStoreRefKind.SECRET_STORE, name=name)


def remote_data(key: str, property: str, *, secret_key: str | None = None) -> ExternalSecretSpecData:
    """Copy `property` of the store's `key` into the target, under `secret_key` or else the same name."""
    return ExternalSecretSpecData(
        secret_key=secret_key or property, remote_ref=ExternalSecretSpecDataRemoteRef(key=key, property=property)
    )


def password_generator(name: str) -> ExternalSecretSpecDataFrom:
    """Source the template's `.password` from the `Password` generator `name`."""
    return ExternalSecretSpecDataFrom(
        source_ref=ExternalSecretSpecDataFromSourceRef(
            generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                api_version="generators.external-secrets.io/v1alpha1",
                kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                name=name,
            )
        )
    )


def add_external_secret(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    refresh: str | ExternalSecretSpecRefreshPolicy,
    store: ExternalSecretSpecSecretStoreRef | None = None,
    data: Sequence[ExternalSecretSpecData] = (),
    data_from: Sequence[ExternalSecretSpecDataFrom] = (),
    creation_policy: ExternalSecretSpecTargetCreationPolicy | None = None,
    deletion_policy: ExternalSecretSpecTargetDeletionPolicy | None = None,
    template: ExternalSecretSpecTargetTemplate | None = None,
    immutable: bool | None = None,
    target_name: str | None = None,
    annotations: dict[str, str] | None = None,
) -> ExternalSecret:
    """Add an `ExternalSecret` `name` whose target Secret is `target_name`, else also `name`.

    `refresh` is a `refreshInterval` duration or a non-periodic `refreshPolicy`. Exactly one of
    `data` and `data_from` is given; `store` is omitted only for a generator source. `None`
    leaves a field unset, so ESO's own default applies.
    """
    if bool(data) == bool(data_from):
        raise ValueError(f"{name=}: give exactly one of data and data_from")
    refresh_interval: str | None = None
    refresh_policy: ExternalSecretSpecRefreshPolicy | None = None
    match refresh:
        case str():
            refresh_interval = refresh
        case ExternalSecretSpecRefreshPolicy():
            refresh_policy = refresh
    return ExternalSecret(
        scope,
        id,
        metadata=metadata(name, namespace, annotations=annotations),
        spec=ExternalSecretSpec(
            refresh_interval=refresh_interval,
            refresh_policy=refresh_policy,
            secret_store_ref=store,
            data=list(data) or None,
            data_from=list(data_from) or None,
            target=ExternalSecretSpecTarget(
                name=target_name or name,
                creation_policy=creation_policy,
                deletion_policy=deletion_policy,
                template=template,
                immutable=immutable,
            ),
        ),
    )
