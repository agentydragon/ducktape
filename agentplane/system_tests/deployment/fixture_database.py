"""Pinned PostgreSQL 18 and disposable per-service databases for the bare-k3s fixture."""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Probe,
    Secret,
    Service,
    ServicePort,
    Volume,
)
from constructs import Construct
from python.runfiles import Runfiles

from cluster.cdk8s.agentplane.service_database import Database
from cluster.cdk8s.secret_ref import SecretRef

NAME = "postgres"
SERVICE = "postgres-primary"
PORT = 5432
IMAGE = "git.allegedly.works/ducktape-ci/agentplane-postgres:unset"

_PASSWORD = "test-k3s-database-password"
_LABELS = {"app.kubernetes.io/name": NAME}


def password_env(database: str) -> str:
    return f"AGENTPLANE_DB_PASSWORD_{database.upper()}"


def _fixture_file(name: str) -> str:
    return (Runfiles.CreateOrRaise().root() / f"ducktape/agentplane/system_tests/deployment/{name}").read_text(
        encoding="utf-8"
    )


def _database_secret(scope: Construct, *, namespace: str, database: Database) -> None:
    host = f"{SERVICE}.{namespace}.svc"
    # This fixture's PostgreSQL listener has no TLS; lib/pq otherwise requires it.
    uri = f"postgresql://{database}:{_PASSWORD}@{host}:{PORT}/{database}?sslmode=disable"
    data = {"password": _PASSWORD, "uri": uri}
    if database is Database.APP:
        data["async-uri"] = f"postgresql+asyncpg://{database}:{_PASSWORD}@{host}:{PORT}/{database}?sslmode=disable"
    Secret(
        scope,
        f"credentials-{database}",
        metadata=ApiObjectMetadata(name=database.secret_name, namespace=namespace),
        string_data=data,
    )


def add_database(scope: Construct, *, namespace: str, databases: Sequence[Database]) -> None:
    """Build the fixture database and its connection Secrets."""
    db_scope = Construct(scope, "db")
    Secret(
        db_scope,
        "bootstrap-credentials",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap-credentials", namespace=namespace),
        string_data={"password": _PASSWORD},
    )
    bootstrap_statements: list[str] = []
    for database in databases:
        _database_secret(db_scope, namespace=namespace, database=database)
        bootstrap_statements.extend(
            (
                f"\\getenv {database}_password {password_env(database)}",
                f"CREATE ROLE {database} WITH LOGIN PASSWORD :'{database}_password';",
                f"CREATE DATABASE {database} OWNER {database};",
            )
        )
    if Database.HISTORY in databases:
        # History's baseline migration grants Electric its replication read access.
        # It needs the role even though this fixture does not run an Electric service.
        bootstrap_statements.append("CREATE ROLE electric;")
    bootstrap = ConfigMap(
        db_scope,
        "bootstrap-config",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap", namespace=namespace),
        data={
            "bootstrap.sql": "\n".join(bootstrap_statements),
            "postgres_entrypoint.sh": _fixture_file("postgres_entrypoint.sh"),
        },
    )
    labels = _LABELS
    deployment = Deployment(
        db_scope,
        "deployment",
        metadata=ApiObjectMetadata(name=NAME, namespace=namespace, labels=labels),
        pod_metadata=ApiObjectMetadata(labels=labels),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        automount_service_account_token=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=26, group=26, fs_group=26),
    )
    env_variables = {
        "PGDATA": EnvValue.from_value("/var/lib/postgresql/18/docker"),
        "POSTGRES_USER": EnvValue.from_value("fixture_admin"),
        "POSTGRES_PASSWORD": SecretRef(namespace=namespace, name=f"{NAME}-bootstrap-credentials")
        .key("password")
        .env_value(db_scope, "postgres-bootstrap-password"),
    }
    for database in databases:
        env_variables[password_env(database)] = (
            SecretRef(namespace=namespace, name=database.secret_name)
            .key("password")
            .env_value(db_scope, f"postgres-{database}-password")
        )

    container = deployment.add_container(
        name="postgres",
        image=IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        command=["/bin/bash", "/fixture/postgres_entrypoint.sh"],
        args=["-c", "wal_level=logical", "-c", "max_connections=300"],
        env_variables=env_variables,
        ports=[ContainerPort(name="postgres", number=PORT)],
        readiness=Probe.from_tcp_socket(
            port=PORT, initial_delay_seconds=Duration.seconds(5), period_seconds=Duration.seconds(5)
        ),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(100)), memory=MemoryResources(request=Size.mebibytes(240))
        ),
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
    container.mount("/fixture", Volume.from_config_map(db_scope, "bootstrap-volume", bootstrap, name="bootstrap"))
    container.mount("/var/lib/postgresql", Volume.from_empty_dir(db_scope, "data-volume", "data"))

    Service(
        db_scope,
        "service",
        metadata=ApiObjectMetadata(name=SERVICE, namespace=namespace),
        selector=deployment,
        ports=[ServicePort(name="postgres", port=PORT, target_port=PORT)],
    )
