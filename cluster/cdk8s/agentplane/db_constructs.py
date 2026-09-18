"""The shared CNPG Postgres Cluster, the per-service Databases, and the ESO
Password+ExternalSecret pairs for the actions/egress managed roles' credentials.

Both environments are non-production; data loss in either's Postgres is acceptable.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from cnpg_database_crds.io.cnpg.postgresql import (
    Database,
    DatabaseSpec,
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s.agentplane import node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY

_CLUSTER_NAME = "postgres"
_IMAGE_NAME = "ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie"
_STORAGE_CLASS = "local-path-ovh-ssd"
_STORAGE_SIZE = "5Gi"
# CNPG's fixed Postgres port -- egress/actions/app's CiliumNetworkPolicy rules allowing
# traffic to this Cluster name the same port.
POSTGRES_PORT = 5432

# The two logical databases each service owns on the shared Cluster; the initdb-owned
# "app"/trajectory database needs no Database/role of its own.
_ROLE_NAMES = ["actions", "egress"]

_CONTROL_PLANE_TOLERATION = ClusterSpecAffinityTolerations(
    key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
)


def _role_credentials(scope: Construct, id: str, *, role: str, namespace: str) -> None:
    """The ESO Password generator + ExternalSecret pair minting one managed role's
    login credentials, in the shape the Cluster's `managed.roles[].passwordSecret` and
    the role's own consumers (litellm, the actions/egress services) expect.
    """
    secret_name = f"postgres-{role}"
    host = f"{_CLUSTER_NAME}-rw.{namespace}.svc"
    Password(
        scope,
        f"{id}-generator",
        metadata=ApiObjectMetadata(name=f"{secret_name}-generator", namespace=namespace),
        spec=PasswordSpec(length=40, digits=8, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=secret_name, namespace=namespace),
        spec=ExternalSecretSpec(
            refresh_interval="8760h",
            target=ExternalSecretSpecTarget(
                name=secret_name,
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/basic-auth",
                    data={
                        "username": role,
                        "password": "{{ .password }}",
                        "host": host,
                        "port": str(POSTGRES_PORT),
                        "dbname": role,
                        "uri": f"postgresql://{role}:{{{{ .password }}}}@{host}:{POSTGRES_PORT}/{role}",
                    },
                ),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=f"{secret_name}-generator",
                        )
                    )
                )
            ],
        ),
    )


class Db(Construct):
    """Shared CNPG Postgres Cluster, its per-service Databases, and the ESO-generated
    credentials for the actions/egress managed roles.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)

        for role in _ROLE_NAMES:
            _role_credentials(self, f"role-credentials-{role}", role=role, namespace=env.namespace)

        Cluster(
            self,
            "cluster",
            metadata=ApiObjectMetadata(name=_CLUSTER_NAME, namespace=env.namespace),
            spec=ClusterSpec(
                instances=env.db.instances,
                image_name=_IMAGE_NAME,
                probes=ClusterSpecProbes(
                    liveness=ClusterSpecProbesLiveness(
                        isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                    )
                ),
                affinity=ClusterSpecAffinity(
                    enable_pod_anti_affinity=True if env.db.pod_anti_affinity else None,
                    pod_anti_affinity_type="preferred" if env.db.pod_anti_affinity else None,
                    topology_key="kubernetes.io/hostname" if env.db.pod_anti_affinity else None,
                    node_selector={"topology.kubernetes.io/zone": node_scheduling.ZONE},
                    tolerations=[_CONTROL_PLANE_TOLERATION],
                    node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
                ),
                storage=ClusterSpecStorage(storage_class=_STORAGE_CLASS, size=_STORAGE_SIZE),
                monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
                bootstrap=ClusterSpecBootstrap(initdb=ClusterSpecBootstrapInitdb(database="app", owner="app")),
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
                ),
            ),
        )

        for role in _ROLE_NAMES:
            Database(
                self,
                f"database-{role}",
                metadata=ApiObjectMetadata(name=role, namespace=env.namespace),
                spec=DatabaseSpec(
                    cluster=DatabaseSpecCluster(name=_CLUSTER_NAME),
                    name=role,
                    owner=role,
                    database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.DELETE,
                ),
            )
