"""Ergonomic wrapper for External Secrets Operator's `ExternalSecret`, following
cdk8s-plus's own construction pattern: a class named after the kind, and named
`@staticmethod` factories grouping a spec fragment's real variant shapes under one type, each
returning the generated struct.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from external_secrets_clusterexternalsecret_crds.io.external_secrets import (
    ClusterExternalSecret as _ClusterExternalSecret,
    ClusterExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpecData,
    ClusterExternalSecretSpecExternalSecretSpecDataFrom,
    ClusterExternalSecretSpecExternalSecretSpecDataFromExtract,
    ClusterExternalSecretSpecExternalSecretSpecDataRemoteRef,
    ClusterExternalSecretSpecExternalSecretSpecRefreshPolicy,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind,
    ClusterExternalSecretSpecExternalSecretSpecTarget,
    ClusterExternalSecretSpecExternalSecretSpecTargetCreationPolicy,
    ClusterExternalSecretSpecExternalSecretSpecTargetDeletionPolicy,
    ClusterExternalSecretSpecExternalSecretSpecTargetTemplate,
)
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


class SecretStoreRef:
    """Which store an `ExternalSecret` reads from: a cluster-wide `ClusterSecretStore`, or
    a `SecretStore` local to the `ExternalSecret`'s own namespace."""

    @staticmethod
    def cluster(name: str) -> ExternalSecretSpecSecretStoreRef:
        return ExternalSecretSpecSecretStoreRef(
            kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE, name=name
        )

    @staticmethod
    def namespaced(name: str) -> ExternalSecretSpecSecretStoreRef:
        return ExternalSecretSpecSecretStoreRef(kind=ExternalSecretSpecSecretStoreRefKind.SECRET_STORE, name=name)


class DataFrom:
    """One `dataFrom` source for an `ExternalSecret`. ESO's `dataFrom[]` entries are a real
    variant type (`extract`, `find`, `sourceRef`); each factory here covers one shape this
    repo actually builds — add another the day a second caller needs it."""

    @staticmethod
    def from_password_generator(
        name: str, *, rewrite: Sequence[ExternalSecretSpecDataFromRewrite] | None = None
    ) -> ExternalSecretSpecDataFrom:
        """Source the template's `.password` from the `Password` generator `name`."""
        return ExternalSecretSpecDataFrom(
            source_ref=ExternalSecretSpecDataFromSourceRef(
                generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                    api_version="generators.external-secrets.io/v1alpha1",
                    kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                    name=name,
                )
            ),
            rewrite=list(rewrite) if rewrite else None,
        )

    @staticmethod
    def from_extract(key: str) -> ExternalSecretSpecDataFrom:
        """Copy every property of the store's `key` into the target, under the same names."""
        return ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=key))

    @staticmethod
    def from_find_by_name_regexp(regexp: str) -> ExternalSecretSpecDataFrom:
        """Copy every store entry whose name matches `regexp` into the target, under the same names."""
        return ExternalSecretSpecDataFrom(
            find=ExternalSecretSpecDataFromFind(name=ExternalSecretSpecDataFromFindName(regexp=regexp))
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
    """The target Secret is `target_name`, else `metadata.name`.

    Exactly one of `data` and `data_from` is given; `secret_store_ref` is omitted only for a
    generator source. `None` leaves a field unset, so ESO's own default applies.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        refresh_interval: str | None = None,
        refresh_policy: ExternalSecretSpecRefreshPolicy | None = None,
        secret_store_ref: ExternalSecretSpecSecretStoreRef | None = None,
        data: Sequence[ExternalSecretSpecData] = (),
        data_from: Sequence[ExternalSecretSpecDataFrom] = (),
        creation_policy: ExternalSecretSpecTargetCreationPolicy | None = None,
        deletion_policy: ExternalSecretSpecTargetDeletionPolicy | None = None,
        template: ExternalSecretSpecTargetTemplate | None = None,
        immutable: bool | None = None,
        target_name: str | None = None,
    ) -> None:
        if bool(data) == bool(data_from):
            raise ValueError(f"{metadata.name=}: give exactly one of data and data_from")
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ExternalSecretSpec(
                refresh_interval=refresh_interval,
                refresh_policy=refresh_policy,
                secret_store_ref=secret_store_ref,
                data=list(data) or None,
                data_from=list(data_from) or None,
                target=ExternalSecretSpecTarget(
                    name=target_name or metadata.name,
                    creation_policy=creation_policy,
                    deletion_policy=deletion_policy,
                    template=template,
                    immutable=immutable,
                ),
            ),
        )


def cluster_remote_data(
    key: str, property: str, *, secret_key: str | None = None
) -> ClusterExternalSecretSpecExternalSecretSpecData:
    """Copy `property` of the store's `key` into the target, under `secret_key` or else the same name.

    Mirrors `remote_data`, for `ClusterExternalSecret`'s embedded, `ExternalSecretSpec`-shaped
    target: `cdk8s_import` generates a distinct Python type per CRD it's pointed at, so this
    identically-shaped embedded field (`ClusterExternalSecretSpecExternalSecretSpecData`, not
    `ExternalSecretSpecData`) isn't the same class and needs its own builder.
    """
    return ClusterExternalSecretSpecExternalSecretSpecData(
        secret_key=secret_key or property,
        remote_ref=ClusterExternalSecretSpecExternalSecretSpecDataRemoteRef(key=key, property=property),
    )


class ClusterSecretStoreRef:
    """`SecretStoreRef` for a `ClusterExternalSecret`'s embedded `ExternalSecretSpec`, generated as
    its own type by the `ClusterExternalSecret` CRD import. Only `cluster`: a namespaced
    `SecretStore` would have to exist identically in every namespace the object mirrors into."""

    @staticmethod
    def cluster(name: str) -> ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef:
        return ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef(
            kind=ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE, name=name
        )


class ClusterDataFrom:
    """One `dataFrom` source for a `ClusterExternalSecret`'s embedded `ExternalSecretSpec`. Same
    variant shape as `DataFrom` (see its docstring), generated as its own type by the
    `ClusterExternalSecret` CRD import; add another factory the day a second shape is needed here.
    """

    @staticmethod
    def from_extract(key: str) -> ClusterExternalSecretSpecExternalSecretSpecDataFrom:
        """Copy every property of the store's `key` into the target, under the same names."""
        return ClusterExternalSecretSpecExternalSecretSpecDataFrom(
            extract=ClusterExternalSecretSpecExternalSecretSpecDataFromExtract(key=key)
        )


class ClusterExternalSecret(_ClusterExternalSecret):
    """Adds `namespaces`: which namespaces get the mirrored `ExternalSecret`. Its target Secret is
    `target_name`, else `metadata.name`. Cluster-scoped, unlike `ExternalSecret`: `metadata`
    carries no `namespace`.

    Exactly one of `data` and `data_from` is given. `None` leaves a field unset, so ESO's own
    default applies.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        namespaces: Sequence[str],
        secret_store_ref: ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef,
        refresh_interval: str | None = None,
        refresh_policy: ClusterExternalSecretSpecExternalSecretSpecRefreshPolicy | None = None,
        data: Sequence[ClusterExternalSecretSpecExternalSecretSpecData] = (),
        data_from: Sequence[ClusterExternalSecretSpecExternalSecretSpecDataFrom] = (),
        creation_policy: ClusterExternalSecretSpecExternalSecretSpecTargetCreationPolicy | None = None,
        deletion_policy: ClusterExternalSecretSpecExternalSecretSpecTargetDeletionPolicy | None = None,
        template: ClusterExternalSecretSpecExternalSecretSpecTargetTemplate | None = None,
        immutable: bool | None = None,
        target_name: str | None = None,
    ) -> None:
        if bool(data) == bool(data_from):
            raise ValueError(f"{metadata.name=}: give exactly one of data and data_from")
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ClusterExternalSecretSpec(
                namespaces=list(namespaces),
                external_secret_spec=ClusterExternalSecretSpecExternalSecretSpec(
                    refresh_interval=refresh_interval,
                    refresh_policy=refresh_policy,
                    secret_store_ref=secret_store_ref,
                    data=list(data) or None,
                    data_from=list(data_from) or None,
                    target=ClusterExternalSecretSpecExternalSecretSpecTarget(
                        name=target_name or metadata.name,
                        creation_policy=creation_policy,
                        deletion_policy=deletion_policy,
                        template=template,
                        immutable=immutable,
                    ),
                ),
            ),
        )
