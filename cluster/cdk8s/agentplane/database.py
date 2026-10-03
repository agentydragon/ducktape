"""The shared CNPG Postgres Cluster, the per-service Databases, and the ESO
Password+ExternalSecret pairs for the actions/egress managed roles' credentials.

Both environments are non-production; data loss in either's Postgres is acceptable.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecPostgresql,
)
from cnpg_database_crds.io.cnpg.postgresql import DatabaseSpecCluster, DatabaseSpecDatabaseReclaimPolicy
from constructs import Construct

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.providers.cnpg.database import Database

_STORAGE_CLASS = "local-path-ovh-ssd"
_STORAGE_SIZE = "5Gi"
# CNPG's fixed Postgres port -- egress/actions/app's CiliumNetworkPolicy rules allowing
# traffic to this Cluster name the same port.
POSTGRES_PORT = 5432

# The two logical databases each service owns on the shared Cluster; the initdb-owned
# "app"/trajectory database needs no Database/role of its own.
_ROLE_NAMES = ["actions", "egress", "notifications"]
_ELECTRIC_ROLE = "electric"


def postgres(env: Environment) -> cnpg.PostgresRef:
    """The environment's shared Cluster."""
    return cnpg.PostgresRef.generated(name="postgres", namespace=env.namespace)


def _role_credentials(
    scope: Construct, id: str, *, cluster: cnpg.PostgresRef, role: str, database_name: str | None = None
) -> None:
    """The ESO Password generator + ExternalSecret pair minting one managed role's
    login credentials, in the shape the Cluster's `managed.roles[].passwordSecret` and
    the role's own consumers (litellm, the actions/egress services) expect.
    """
    mint_db_role_secret(
        scope,
        id,
        name=f"postgres-{role}",
        namespace=cluster.namespace,
        role=role,
        host=cluster.rw.host,
        port=cluster.rw.port.number,
        database=database_name or role,
        url_key="uri",
    )


class Db(Construct):
    """Shared CNPG Postgres Cluster, its per-service Databases, and the ESO-generated
    credentials for the actions/egress managed roles.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        cluster = postgres(env)

        for role in _ROLE_NAMES:
            _role_credentials(self, f"role-credentials-{role}", cluster=cluster, role=role)
        _role_credentials(self, "role-credentials-electric", cluster=cluster, role=_ELECTRIC_ROLE, database_name="app")

        cnpg.cluster(
            self,
            "cluster",
            ref=cluster,
            instances=env.db.instances,
            placement=node_scheduling.HIL_OVH,
            storage_class=_STORAGE_CLASS,
            size=_STORAGE_SIZE,
            # Electric's WAL-loss recovery purges every shape, then stays unready while
            # rebuilding its replication pipeline. Keep a bounded outage budget below the
            # 5Gi volume instead of silently recycling the logical slot's WAL.
            postgresql=ClusterSpecPostgresql(parameters={"max_slot_wal_keep_size": "512MB"}),
            initdb=cnpg.same_owner_initdb("app"),
            wal_archive=False,
            managed=ClusterSpecManaged(
                roles=[
                    ClusterSpecManagedRoles(
                        name=role,
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name=f"postgres-{role}"),
                    )
                    for role in _ROLE_NAMES
                ]
                + [
                    ClusterSpecManagedRoles(
                        name=_ELECTRIC_ROLE,
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        replication=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name="postgres-electric"),
                    )
                ]
            ),
        )

        for role in _ROLE_NAMES:
            Database(
                self,
                f"database-{role}",
                metadata=ApiObjectMetadata(name=role, namespace=env.namespace),
                cluster=DatabaseSpecCluster(name=cluster.name),
                name=role,
                owner=role,
                database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.DELETE,
            )
