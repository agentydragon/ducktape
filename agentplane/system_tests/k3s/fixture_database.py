"""Pinned PostgreSQL 18 and disposable per-service databases for the bare-k3s fixture."""

from __future__ import annotations

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

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.secret_ref import SecretRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "postgres"
SERVICE = "postgres-primary"
PORT = 5432
IMAGE = "git.allegedly.works/ducktape-ci/agentplane-postgres:unset"

_PASSWORD = "test-k3s-database-password"
_ROLES = ("app", "actions", "egress", "mcp_edge", "notifications", "sandbox_service", "session_dispatcher")
_LABELS = {"app.kubernetes.io/name": NAME}


def _fixture_file(name: str) -> str:
    return get_required_path(own_repo_rlocation(f"agentplane/system_tests/k3s/{name}")).read_text(encoding="utf-8")


def _database_secret(
    scope: Construct, env: Environment, *, role: str, database: str, password: str, async_uri: bool = False
) -> None:
    host = f"{SERVICE}.{env.namespace}.svc"
    # This fixture's PostgreSQL listener has no TLS; lib/pq otherwise requires it.
    uri = f"postgresql://{role}:{password}@{host}:{PORT}/{database}?sslmode=disable"
    data = {"password": password, "uri": uri}
    if async_uri:
        data["async-uri"] = f"postgresql+asyncpg://{role}:{password}@{host}:{PORT}/{database}"
    Secret(
        scope,
        f"credentials-{role}",
        metadata=ApiObjectMetadata(name=f"postgres-{role.replace('_', '-')}", namespace=env.namespace),
        string_data=data,
    )


def add_database(scope: Construct, env: Environment) -> Construct:
    """Build the fixture database and its connection Secrets."""
    db_scope = Construct(scope, "db")
    roles = _ROLES + (("central_sampler",) if env.central_sampler is not None else ())

    Secret(
        db_scope,
        "bootstrap-credentials",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap-credentials", namespace=env.namespace),
        string_data={"password": _PASSWORD},
    )
    for role in roles:
        _database_secret(db_scope, env, role=role, database=role, password=_PASSWORD, async_uri=role == "app")

    bootstrap_sql = _fixture_file("bootstrap.sql")
    if env.central_sampler is not None:
        bootstrap_sql += "\n" + _fixture_file("bootstrap_central_sampler.sql")
    bootstrap = ConfigMap(
        db_scope,
        "bootstrap-config",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap", namespace=env.namespace),
        data={"bootstrap.sql": bootstrap_sql, "postgres_entrypoint.sh": _fixture_file("postgres_entrypoint.sh")},
    )
    labels = _LABELS
    deployment = Deployment(
        db_scope,
        "deployment",
        metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=labels),
        pod_metadata=ApiObjectMetadata(labels=labels),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        automount_service_account_token=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=26, group=26, fs_group=26),
    )
    env_variables = {
        "PGDATA": EnvValue.from_value("/var/lib/postgresql/18/docker"),
        "POSTGRES_USER": EnvValue.from_value("fixture_admin"),
        "POSTGRES_PASSWORD": SecretRef(namespace=env.namespace, name=f"{NAME}-bootstrap-credentials")
        .key("password")
        .env_value(db_scope, "postgres-bootstrap-password"),
    }
    for role in roles:
        env_variables[f"AGENTPLANE_DB_PASSWORD_{role.upper()}"] = (
            SecretRef(namespace=env.namespace, name=f"postgres-{role.replace('_', '-')}")
            .key("password")
            .env_value(db_scope, f"postgres-{role}-password")
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
            cpu=CpuResources(request=Cpu.millis(100)), memory=MemoryResources(request=Size.mebibytes(256))
        ),
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
    container.mount("/fixture", Volume.from_config_map(db_scope, "bootstrap-volume", bootstrap, name="bootstrap"))
    container.mount("/var/lib/postgresql", Volume.from_empty_dir(db_scope, "data-volume", "data"))

    Service(
        db_scope,
        "service",
        metadata=ApiObjectMetadata(name=SERVICE, namespace=env.namespace),
        selector=deployment,
        ports=[ServicePort(name="postgres", port=PORT, target_port=PORT)],
    )
    return db_scope
