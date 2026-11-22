"""The shared CNPG Postgres Cluster, per-service Databases, and ESO-generated
credentials for managed roles.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecPostgresql,
)
from cnpg_database_crds.io.cnpg.postgresql import DatabaseSpecDatabaseReclaimPolicy
from constructs import Construct

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_database import Database, role_secret_name
from cluster.cdk8s.cnpg import cluster as cnpg  # Local `cluster` parameters shadow the module name.
from cluster.cdk8s.cnpg.cluster import ClusterPlacement
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.providers.cilium.network_policy import EgressRule

_CLUSTER_NAME = "postgres"

_ELECTRIC_ROLE = "electric"


def postgres(env: Environment) -> cnpg.PostgresRef:
    """The environment's shared Cluster."""
    return cnpg.PostgresRef.generated(name=_CLUSTER_NAME, namespace=env.namespace)


def egress(namespace: str) -> CiliumNetworkPolicySpecEgress:
    """A client's rule reaching the environment's shared Cluster."""
    return EgressRule.to_endpoints(
        {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": _CLUSTER_NAME}, cnpg.PORT.number
    )


def _role_credentials(
    scope: Construct, id: str, *, cluster: cnpg.PostgresRef, role: str, database_name: str | None = None
) -> None:
    """The ESO Password generator + ExternalSecret pair minting one managed role's
    login credentials, in the shape the Cluster's `managed.roles[].passwordSecret` and
    the role's own consumers expect.
    """
    mint_db_role_secret(
        scope,
        id,
        name=role_secret_name(role),
        namespace=cluster.namespace,
        role=role,
        host=cluster.rw.host,
        port=cluster.rw.port.number,
        database=database_name or role,
        url_key="uri",
    )


@dataclass(frozen=True)
class DbProps:
    instances: int
    # Archive WAL and take base backups into the environment's SeaweedFS bucket
    # (agentplane/db_backups.py) through the barman-cloud plugin.
    wal_archive: bool
    # Node selector, storage class, and control-plane tolerance are selected together.
    profile: ClusterPlacement
    size: str = "5Gi"
    resize_in_use_volumes: bool | None = None


class Db(Construct):
    """Shared CNPG Postgres Cluster, its per-service Databases, and the ESO-generated
    credentials for managed roles.
    """

    def __init__(self, scope: Construct, id: str, env: Environment, props: DbProps) -> None:
        super().__init__(scope, id)
        cluster = postgres(env)
        # CNPG initdb owns app. The optional sampler keeps its database when disabled.
        roles = [
            role.value
            for role in Database
            if role != Database.APP and (role != Database.CENTRAL_SAMPLER or env.central_sampler is not None)
        ]

        for role in roles:
            _role_credentials(self, f"role-credentials-{role}", cluster=cluster, role=role)
        _role_credentials(self, "role-credentials-electric", cluster=cluster, role=_ELECTRIC_ROLE, database_name="app")

        postgres_cluster = cnpg.cluster(
            self,
            "cluster",
            ref=cluster,
            instances=props.instances,
            profile=props.profile,
            size=props.size,
            resize_in_use_volumes=props.resize_in_use_volumes,
            # Electric's WAL-loss recovery purges every shape, then stays unready while
            # rebuilding its replication pipeline. Keep a bounded outage budget below the
            # configured volume instead of silently recycling the logical slot's WAL.
            # Every service Deployment's pools and LISTEN connections, summed at full overflow over
            # its replicas plus a rollout's surge Pod, exceed the default 100; a rejected connection
            # fails requests and can stop background loops such as the History Service fold.
            postgresql=ClusterSpecPostgresql(parameters={"max_slot_wal_keep_size": "512MB", "max_connections": "300"}),
            initdb=cnpg.same_owner_initdb("app"),
            wal_archive=props.wal_archive,
            managed=ClusterSpecManaged(
                roles=[
                    ClusterSpecManagedRoles(
                        name=role,
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name=role_secret_name(role)),
                    )
                    for role in roles
                ]
                + [
                    ClusterSpecManagedRoles(
                        name=_ELECTRIC_ROLE,
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        replication=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name=role_secret_name(_ELECTRIC_ROLE)),
                    )
                ]
            ),
        )

        for role in roles:
            cnpg.Database(
                self,
                f"database-{role}",
                metadata=ApiObjectMetadata(name=role.replace("_", "-"), namespace=env.namespace),
                cluster=postgres_cluster,
                name=role,
                owner=role,
                # Disabling the optional sampler must preserve its authored conversations.
                database_reclaim_policy=(
                    DatabaseSpecDatabaseReclaimPolicy.RETAIN
                    if role == Database.CENTRAL_SAMPLER
                    else DatabaseSpecDatabaseReclaimPolicy.DELETE
                ),
            )
