"""Ergonomic wrapper for External Secrets Operator's `ExternalSecret`, following
cdk8s-plus's own construction pattern: a class named after the kind, and named
`@classmethod` factories grouping a spec fragment's real variant shapes under one type.
"""

from __future__ import annotations

from collections.abc import Sequence

from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret as _ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromExtract,
    ExternalSecretSpecDataFromFind,
    ExternalSecretSpecDataFromFindName,
    ExternalSecretSpecDataFromRewrite,
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


class SecretStoreRef:
    """Which store an `ExternalSecret` reads from: a cluster-wide `ClusterSecretStore`, or
    a `SecretStore` local to the `ExternalSecret`'s own namespace."""

    def __init__(self, spec: ExternalSecretSpecSecretStoreRef) -> None:
        self._spec = spec

    def to_spec(self) -> ExternalSecretSpecSecretStoreRef:
        return self._spec

    @classmethod
    def cluster(cls, name: str) -> SecretStoreRef:
        return cls(
            ExternalSecretSpecSecretStoreRef(kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE, name=name)
        )

    @classmethod
    def namespaced(cls, name: str) -> SecretStoreRef:
        return cls(ExternalSecretSpecSecretStoreRef(kind=ExternalSecretSpecSecretStoreRefKind.SECRET_STORE, name=name))


class DataFrom:
    """One `dataFrom` source for an `ExternalSecret`. ESO's `dataFrom[]` entries are a real
    variant type (`extract`, `find`, `sourceRef`); each factory here covers one shape this
    repo actually builds — add another the day a second caller needs it."""

    def __init__(self, spec: ExternalSecretSpecDataFrom) -> None:
        self._spec = spec

    def to_spec(self) -> ExternalSecretSpecDataFrom:
        return self._spec

    @classmethod
    def from_password_generator(
        cls, name: str, *, rewrite: Sequence[ExternalSecretSpecDataFromRewrite] | None = None
    ) -> DataFrom:
        """Source the template's `.password` from the `Password` generator `name`."""
        return cls(
            ExternalSecretSpecDataFrom(
                source_ref=ExternalSecretSpecDataFromSourceRef(
                    generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                        api_version="generators.external-secrets.io/v1alpha1",
                        kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                        name=name,
                    )
                ),
                rewrite=list(rewrite) if rewrite else None,
            )
        )

    @classmethod
    def from_extract(cls, key: str) -> DataFrom:
        """Copy every property of the store's `key` into the target, under the same names."""
        return cls(ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=key)))

    @classmethod
    def from_find_by_name_regexp(cls, regexp: str) -> DataFrom:
        """Copy every store entry whose name matches `regexp` into the target, under the same names."""
        return cls(
            ExternalSecretSpecDataFrom(
                find=ExternalSecretSpecDataFromFind(name=ExternalSecretSpecDataFromFindName(regexp=regexp))
            )
        )


def remote_data(key: str, property: str, *, secret_key: str | None = None) -> ExternalSecretSpecData:
    """Copy `property` of the store's `key` into the target, under `secret_key` or else the same name.

    Stays a function, not a factory on a class: `ExternalSecretSpecData.remote_ref` is always
    required, so there is no variant shape here to group under one type the way `DataFrom`'s
    extract/find/sourceRef genuinely are alternatives.
    """
    return ExternalSecretSpecData(
        secret_key=secret_key or property, remote_ref=ExternalSecretSpecDataRemoteRef(key=key, property=property)
    )


class ExternalSecret(_ExternalSecret):
    """Adds `name`'s target Secret is `target_name`, else also `name`.

    `refresh` is a `refreshInterval` duration or a non-periodic `refreshPolicy`. Exactly one of
    `data` and `data_from` is given; `store` is omitted only for a generator source. `None`
    leaves a field unset, so ESO's own default applies.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        refresh: str | ExternalSecretSpecRefreshPolicy,
        store: SecretStoreRef | None = None,
        data: Sequence[ExternalSecretSpecData] = (),
        data_from: Sequence[DataFrom] = (),
        creation_policy: ExternalSecretSpecTargetCreationPolicy | None = None,
        deletion_policy: ExternalSecretSpecTargetDeletionPolicy | None = None,
        template: ExternalSecretSpecTargetTemplate | None = None,
        immutable: bool | None = None,
        target_name: str | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        if bool(data) == bool(data_from):
            raise ValueError(f"{name=}: give exactly one of data and data_from")
        refresh_interval: str | None = None
        refresh_policy: ExternalSecretSpecRefreshPolicy | None = None
        match refresh:
            case str():
                refresh_interval = refresh
            case ExternalSecretSpecRefreshPolicy():
                refresh_policy = refresh
        super().__init__(
            scope,
            id,
            metadata=metadata(name, namespace, annotations=annotations),
            spec=ExternalSecretSpec(
                refresh_interval=refresh_interval,
                refresh_policy=refresh_policy,
                secret_store_ref=store.to_spec() if store else None,
                data=list(data) or None,
                data_from=[item.to_spec() for item in data_from] or None,
                target=ExternalSecretSpecTarget(
                    name=target_name or name,
                    creation_policy=creation_policy,
                    deletion_policy=deletion_policy,
                    template=template,
                    immutable=immutable,
                ),
            ),
        )
