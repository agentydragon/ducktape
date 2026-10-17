"""Pinned PostgreSQL 18 and disposable per-service databases for the bare-k3s fixture."""

from __future__ import annotations

import secrets
import string

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

_ROLES = ("actions", "egress", "notifications", "sandbox_service", "session_dispatcher")
_LABELS = {"app.kubernetes.io/name": NAME}


def _password(length: int = 40) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def _bootstrap_sql() -> str:
    return get_required_path(own_repo_rlocation("agentplane/x/k3s_env/bootstrap.sql")).read_text(encoding="utf-8")


def _database_secret(
    scope: Construct, env: Environment, *, role: str, database: str, password: str, async_uri: bool = False
) -> None:
    host = f"{SERVICE}.{env.namespace}.svc"
    uri = f"postgresql://{role}:{password}@{host}:{PORT}/{database}"
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

    bootstrap_password = _password()
    Secret(
        db_scope,
        "bootstrap-credentials",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap-credentials", namespace=env.namespace),
        string_data={"password": bootstrap_password},
    )
    app_password = _password()
    _database_secret(db_scope, env, role="app", database="app", password=app_password, async_uri=True)
    for role in _ROLES:
        _database_secret(db_scope, env, role=role, database=role, password=_password())

    bootstrap = ConfigMap(
        db_scope,
        "bootstrap-config",
        metadata=ApiObjectMetadata(name=f"{NAME}-bootstrap", namespace=env.namespace),
        data={"01-bootstrap.sql": _bootstrap_sql()},
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
        security_context=PodSecurityContextProps(ensure_non_root=True, user=999, group=999, fs_group=999),
    )
    env_variables = {
        "PGDATA": EnvValue.from_value("/var/lib/postgresql/18/docker"),
        "POSTGRES_USER": EnvValue.from_value("fixture_admin"),
        "POSTGRES_DB": EnvValue.from_value("postgres"),
        "POSTGRES_PASSWORD": SecretRef(namespace=env.namespace, name=f"{NAME}-bootstrap-credentials")
        .key("password")
        .env_value(db_scope, "postgres-bootstrap-password"),
    }
    for role in ("app", *_ROLES):
        env_variables[f"AGENTPLANE_DB_PASSWORD_{role.upper()}"] = (
            SecretRef(namespace=env.namespace, name=f"postgres-{role.replace('_', '-')}")
            .key("password")
            .env_value(db_scope, f"postgres-{role}-password")
        )

    container = deployment.add_container(
        name="postgres",
        image=IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        args=["postgres", "-c", "wal_level=logical", "-c", "max_connections=300"],
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
    container.mount(
        "/docker-entrypoint-initdb.d", Volume.from_config_map(db_scope, "bootstrap-volume", bootstrap, name="bootstrap")
    )
    container.mount("/var/lib/postgresql", Volume.from_empty_dir(db_scope, "data-volume", "data"))

    Service(
        db_scope,
        "service",
        metadata=ApiObjectMetadata(name=SERVICE, namespace=env.namespace),
        selector=deployment,
        ports=[ServicePort(name="postgres", port=PORT, target_port=PORT)],
    )
    return db_scope
