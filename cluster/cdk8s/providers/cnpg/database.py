"""Ergonomic wrapper for CloudNativePG's `Database`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `Database(scope, id, *, metadata, ...)`.
Every keyword is a `DatabaseSpec` field under its own name and type; `None` leaves it unset, so
CNPG's own default applies. No cluster, owner or reclaim policy lives here.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cnpg_database_crds.io.cnpg.postgresql import (
    Database as _Database,
    DatabaseSpec,
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecEnsure,
    DatabaseSpecExtensions,
    DatabaseSpecFdws,
    DatabaseSpecSchemas,
    DatabaseSpecServers,
)
from constructs import Construct


class Database(_Database):
    """CloudNativePG's `Database`: database `name`, owned by role `owner`, on the `Cluster` in the
    object's own namespace that `cluster` names. Those three are the fields the CRD itself
    requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        cluster: DatabaseSpecCluster,
        name: str,
        owner: str,
        ensure: DatabaseSpecEnsure | None = None,
        database_reclaim_policy: DatabaseSpecDatabaseReclaimPolicy | None = None,
        extensions: Sequence[DatabaseSpecExtensions] | None = None,
        schemas: Sequence[DatabaseSpecSchemas] | None = None,
        fdws: Sequence[DatabaseSpecFdws] | None = None,
        servers: Sequence[DatabaseSpecServers] | None = None,
        template: str | None = None,
        tablespace: str | None = None,
        encoding: str | None = None,
        locale: str | None = None,
        locale_provider: str | None = None,
        locale_collate: str | None = None,
        locale_c_type: str | None = None,
        icu_locale: str | None = None,
        icu_rules: str | None = None,
        builtin_locale: str | None = None,
        collation_version: str | None = None,
        is_template: bool | None = None,
        allow_connections: bool | None = None,
        connection_limit: int | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=DatabaseSpec(
                cluster=cluster,
                name=name,
                owner=owner,
                ensure=ensure,
                database_reclaim_policy=database_reclaim_policy,
                extensions=list(extensions) if extensions is not None else None,
                schemas=list(schemas) if schemas is not None else None,
                fdws=list(fdws) if fdws is not None else None,
                servers=list(servers) if servers is not None else None,
                template=template,
                tablespace=tablespace,
                encoding=encoding,
                locale=locale,
                locale_provider=locale_provider,
                locale_collate=locale_collate,
                locale_c_type=locale_c_type,
                icu_locale=icu_locale,
                icu_rules=icu_rules,
                builtin_locale=builtin_locale,
                collation_version=collation_version,
                is_template=is_template,
                allow_connections=allow_connections,
                connection_limit=connection_limit,
            ),
        )
