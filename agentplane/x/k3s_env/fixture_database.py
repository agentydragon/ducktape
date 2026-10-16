"""Local-path CNPG database and literal managed-role Secrets for the bare K3s fixture."""

from __future__ import annotations

import secrets
import string

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecEnv,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecManagedServices,
    ClusterSpecManagedServicesAdditional,
    ClusterSpecManagedServicesAdditionalSelectorType,
    ClusterSpecManagedServicesAdditionalServiceTemplate,
    ClusterSpecManagedServicesAdditionalServiceTemplateMetadata,
    ClusterSpecManagedServicesAdditionalServiceTemplateSpec,
    ClusterSpecManagedServicesAdditionalUpdateStrategy,
    ClusterSpecPostgresql,
)
from cnpg_database_crds.io.cnpg.postgresql import DatabaseSpecCluster, DatabaseSpecDatabaseReclaimPolicy
from constructs import Construct

from cluster.cdk8s import cnpg
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.providers.cnpg.database import Database

_ROLES = ("actions", "egress", "notifications", "sandbox_service", "session_dispatcher")


def _password(length: int = 40) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def add_database(scope: Construct, env: Environment, *, node_ip: str) -> Construct:
    """Use shared CNPG builders while supplying the fixture's storage and credentials."""
    db_scope = Construct(scope, "db")
    cluster = database.postgres(env)
    # Point fixture clients at the additional headless Service; k3s has no kube-proxy for CNPG's
    # default postgres-rw ClusterIP, so the stack rewrites that name here where needed.
    host = f"postgres-primary.{env.namespace}.svc"
    for role in (*_ROLES, "electric"):
        password = _password()
        name = f"postgres-{role.replace('_', '-')}"
        logical_database = "app" if role == "electric" else role
        k8s.KubeSecret(
            db_scope,
            f"role-credentials-{role}",
            metadata=k8s.ObjectMeta(name=name, namespace=env.namespace, labels={"cnpg.io/reload": "true"}),
            type="kubernetes.io/basic-auth",
            string_data={
                "username": role,
                "password": password,
                "host": host,
                "port": str(cluster.rw.port.number),
                "dbname": logical_database,
                "uri": f"postgresql://{role}:{password}@{host}:{cluster.rw.port.number}/{logical_database}",
            },
        )
    cnpg.cluster(
        db_scope,
        "cluster",
        ref=cluster,
        instances=env.db.instances,
        placement=None,
        storage_class="local-path",
        size=Size.gibibytes(5).as_string(),
        postgresql=ClusterSpecPostgresql(parameters={"max_slot_wal_keep_size": "512MB", "max_connections": "300"}),
        initdb=cnpg.same_owner_initdb("app"),
        wal_archive=False,
        env=[
            ClusterSpecEnv(name="KUBERNETES_SERVICE_HOST", value=node_ip),
            ClusterSpecEnv(name="KUBERNETES_SERVICE_PORT", value="6443"),
        ],
        enable_pod_monitor=False,
        managed=ClusterSpecManaged(
            services=ClusterSpecManagedServices(
                additional=[
                    ClusterSpecManagedServicesAdditional(
                        selector_type=ClusterSpecManagedServicesAdditionalSelectorType.RW,
                        update_strategy=ClusterSpecManagedServicesAdditionalUpdateStrategy.PATCH,
                        service_template=ClusterSpecManagedServicesAdditionalServiceTemplate(
                            metadata=ClusterSpecManagedServicesAdditionalServiceTemplateMetadata(
                                name="postgres-primary"
                            ),
                            spec=ClusterSpecManagedServicesAdditionalServiceTemplateSpec(cluster_ip="None"),
                        ),
                    )
                ]
            ),
            roles=[
                ClusterSpecManagedRoles(
                    name=role,
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=f"postgres-{role.replace('_', '-')}"),
                )
                for role in _ROLES
            ]
            + [
                ClusterSpecManagedRoles(
                    name="electric",
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    replication=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name="postgres-electric"),
                )
            ],
        ),
    )
    for role in _ROLES:
        Database(
            db_scope,
            f"database-{role}",
            metadata=ApiObjectMetadata(name=role.replace("_", "-"), namespace=env.namespace),
            cluster=DatabaseSpecCluster(name=cluster.name),
            name=role,
            owner=role,
            database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.DELETE,
        )
    return db_scope
