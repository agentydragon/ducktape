"""Retain PostgreSQL across the Pod system-test suite and reset its service databases."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml

from agentplane.system_tests.k3s import certificates, rendered_fixture
from agentplane.system_tests.tracing import tracer

NAMESPACE = "systest-infrastructure"
POSTGRES_SERVICE = "postgres-primary"
POSTGRES_DEPLOYMENT = "postgres"
POSTGRES_CONTAINER = "postgres"
BOOTSTRAP_CONFIG_MAP = "postgres-bootstrap"
BOOTSTRAP_SECRET = "postgres-bootstrap-credentials"
ADMIN_ROLE = "fixture_admin"
ELECTRIC_ROLE = "electric"

_POSTGRES_SERVICE_HOST = f"{POSTGRES_SERVICE}.{NAMESPACE}.svc"
_DATABASE_PORT = 5432
_REQUEST_TIMEOUT = "10s"
_NAMESPACE_TIMEOUT = 15
_APPLY_TIMEOUT = 30
_ROLLOUT_TIMEOUT = 190
_SQL_TIMEOUT = 60
_DATABASE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_USER_RELATIONS_SQL = """SELECT 'user_relations', current_database(), count(*)::text
FROM pg_class AS relation
JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND schema.nspname <> 'information_schema'
  AND schema.nspname !~ '^pg_';"""


@dataclass(frozen=True)
class Baseline:
    fixture: rendered_fixture.RenderedFixture
    objects: list[dict[str, Any]]
    databases: tuple[str, ...]


@dataclass(frozen=True)
class _Credential:
    secret_name: str
    role: str
    database: str


@dataclass(frozen=True)
class _Inspection:
    identity: dict[str, Any]
    database_oids: dict[str, str]
    user_relation_counts: dict[str, int]
    sentinel_present: bool


def _resource_key(resource: dict[str, Any]) -> tuple[str, str, str]:
    return resource["kind"], resource["metadata"].get("namespace", ""), resource["metadata"]["name"]


def _parse_uri(uri: str, *, secret_name: str, expected_scheme: str | None = None) -> tuple[str, str]:
    parts = urlsplit(uri)
    if parts.scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise ValueError(f"{secret_name} has unsupported PostgreSQL URI scheme {parts.scheme!r}")
    if expected_scheme is not None and parts.scheme != expected_scheme:
        raise ValueError(f"{secret_name} has unexpected URI scheme {parts.scheme!r}")
    if parts.username is None or parts.password is None or not parts.path.startswith("/"):
        raise ValueError(f"{secret_name} has an incomplete PostgreSQL URI")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{secret_name} has an invalid PostgreSQL URI port") from exc
    if port not in {None, _DATABASE_PORT} or parts.fragment:
        raise ValueError(f"{secret_name} has an unexpected PostgreSQL URI port or fragment")

    role = unquote(parts.username)
    database = unquote(parts.path[1:])
    if not _DATABASE_IDENTIFIER.fullmatch(role) or not _DATABASE_IDENTIFIER.fullmatch(database):
        raise ValueError(f"{secret_name} has an unsupported role or database identifier")
    return role, database


def _rewrite_uri(uri: str, *, secret_name: str, host: str, expected_scheme: str | None = None) -> str:
    _parse_uri(uri, secret_name=secret_name, expected_scheme=expected_scheme)
    parts = urlsplit(uri)
    userinfo, separator, _ = parts.netloc.rpartition("@")
    if not separator:
        raise ValueError(f"{secret_name} has no PostgreSQL URI user information")
    return urlunsplit(parts._replace(netloc=f"{userinfo}@{host}:{_DATABASE_PORT}"))


def _credential_secrets(objects: list[dict[str, Any]], *, namespace: str) -> list[_Credential]:
    credentials: list[_Credential] = []
    for resource in objects:
        if resource.get("kind") != "Secret" or resource["metadata"].get("namespace") != namespace:
            continue
        secret_data = resource.get("stringData", {})
        if "uri" not in secret_data:
            continue
        secret_name = resource["metadata"]["name"]
        role, database = _parse_uri(secret_data["uri"], secret_name=secret_name, expected_scheme="postgresql")
        if secret_name != f"postgres-{role.replace('_', '-')}" or "password" not in secret_data:
            raise ValueError(f"{secret_name} does not match its PostgreSQL URI role")
        if "async-uri" in secret_data:
            async_role, async_database = _parse_uri(
                secret_data["async-uri"], secret_name=secret_name, expected_scheme="postgresql+asyncpg"
            )
            if (async_role, async_database) != (role, database):
                raise ValueError(f"{secret_name} async URI does not match its PostgreSQL URI")
        credentials.append(_Credential(secret_name=secret_name, role=role, database=database))

    if len(credentials) != 7:
        raise ValueError(f"expected seven PostgreSQL credential Secrets in {namespace}, found {len(credentials)}")
    if len({credential.role for credential in credentials}) != len(credentials):
        raise ValueError("PostgreSQL credential Secrets contain duplicate roles")
    if len({credential.database for credential in credentials}) != len(credentials):
        raise ValueError("PostgreSQL credential Secrets contain duplicate databases")
    if "app" not in {credential.role for credential in credentials}:
        raise ValueError("PostgreSQL credential Secrets do not contain the app role")
    return sorted(credentials, key=lambda credential: credential.database)


def _validate_deployment_credentials(deployment: dict[str, Any], credentials: list[_Credential]) -> None:
    env = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    expected = {"POSTGRES_PASSWORD": BOOTSTRAP_SECRET} | {
        f"AGENTPLANE_DB_PASSWORD_{credential.role.upper()}": credential.secret_name for credential in credentials
    }
    actual_names = {name for name in env if name == "POSTGRES_PASSWORD" or name.startswith("AGENTPLANE_DB_PASSWORD_")}
    if actual_names != set(expected):
        raise ValueError(f"PostgreSQL Deployment credential env names differ: {actual_names ^ set(expected)}")
    for env_name, secret_name in expected.items():
        secret_ref = env[env_name].get("valueFrom", {}).get("secretKeyRef", {})
        if secret_ref.get("name") != secret_name or secret_ref.get("key") != "password":
            raise ValueError(f"PostgreSQL Deployment {env_name} does not reference {secret_name}.password")


def _set_namespace(resource: dict[str, Any], namespace: str) -> dict[str, Any]:
    moved = copy.deepcopy(resource)
    moved["metadata"]["namespace"] = namespace
    return moved


@tracer.start_as_current_span("system_test.postgres.prepare")
def prepare(fixture: rendered_fixture.RenderedFixture) -> Baseline:
    if fixture.vm_sandboxes or fixture.central_sampler or fixture.mcp_fixtures is not None:
        raise ValueError("retained PostgreSQL supports only the ordinary Pod fixture")

    credentials = _credential_secrets(fixture.objects, namespace=fixture.namespace)
    resources = {_resource_key(resource): resource for resource in fixture.objects}
    required = {
        ("Deployment", fixture.namespace, POSTGRES_DEPLOYMENT),
        ("Service", fixture.namespace, POSTGRES_SERVICE),
        ("ConfigMap", fixture.namespace, BOOTSTRAP_CONFIG_MAP),
        ("Secret", fixture.namespace, BOOTSTRAP_SECRET),
    }
    if not required <= resources.keys():
        raise ValueError(f"rendered fixture lacks PostgreSQL resources: {required - resources.keys()}")
    if len(resources) != len(fixture.objects):
        raise ValueError("rendered fixture has duplicate Kubernetes resource names")

    deployment = resources[("Deployment", fixture.namespace, POSTGRES_DEPLOYMENT)]
    config_map = resources[("ConfigMap", fixture.namespace, BOOTSTRAP_CONFIG_MAP)]
    if config_map.get("data", {}).get("bootstrap.sql") is None:
        raise ValueError("PostgreSQL bootstrap ConfigMap has no bootstrap.sql")
    _validate_deployment_credentials(deployment, credentials)

    moved_keys = required
    credential_names = {credential.secret_name for credential in credentials}
    app_objects: list[dict[str, Any]] = []
    moved_objects: dict[tuple[str, str, str], dict[str, Any]] = {}
    copied_secrets: list[dict[str, Any]] = []
    for resource in fixture.objects:
        key = _resource_key(resource)
        if key in moved_keys:
            moved_objects[key] = _set_namespace(resource, NAMESPACE)
        elif (
            resource.get("kind") == "Secret"
            and resource["metadata"].get("namespace") == fixture.namespace
            and resource["metadata"]["name"] in credential_names
        ):
            app_secret = copy.deepcopy(resource)
            app_secret_data = app_secret["stringData"]
            app_secret_data["uri"] = _rewrite_uri(
                app_secret_data["uri"],
                secret_name=resource["metadata"]["name"],
                host=_POSTGRES_SERVICE_HOST,
                expected_scheme="postgresql",
            )
            if "async-uri" in app_secret_data:
                app_secret_data["async-uri"] = _rewrite_uri(
                    app_secret_data["async-uri"],
                    secret_name=resource["metadata"]["name"],
                    host=_POSTGRES_SERVICE_HOST,
                    expected_scheme="postgresql+asyncpg",
                )
            app_objects.append(app_secret)
            copied_secrets.append(_set_namespace(app_secret, NAMESPACE))
        else:
            app_objects.append(copy.deepcopy(resource))

    if set(moved_objects) != moved_keys or len(copied_secrets) != 7:
        raise ValueError("rendered fixture PostgreSQL resources could not be partitioned completely")
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}
    retained_objects = [
        namespace,
        moved_objects[("Secret", fixture.namespace, BOOTSTRAP_SECRET)],
        *copied_secrets,
        moved_objects[("ConfigMap", fixture.namespace, BOOTSTRAP_CONFIG_MAP)],
        moved_objects[("Service", fixture.namespace, POSTGRES_SERVICE)],
        moved_objects[("Deployment", fixture.namespace, POSTGRES_DEPLOYMENT)],
    ]
    rewritten_fixture = fixture.model_copy(update={"objects": app_objects})
    return Baseline(
        fixture=rewritten_fixture,
        objects=retained_objects,
        databases=tuple(credential.database for credential in credentials),
    )


def _kubectl_json(kubectl: certificates.Kubectl, *args: str, timeout: int = _NAMESPACE_TIMEOUT) -> dict[str, Any]:
    output = kubectl(*args, f"--request-timeout={_REQUEST_TIMEOUT}", "-o", "json", timeout=timeout)
    value = yaml.safe_load(output)
    if not isinstance(value, dict):
        raise RuntimeError(f"kubectl returned a non-object for {args}")
    return value


def _process_identity(kubectl: certificates.Kubectl) -> dict[str, Any]:
    deployment = _kubectl_json(kubectl, "-n", NAMESPACE, "get", f"deployment/{POSTGRES_DEPLOYMENT}")
    deployment_uid = deployment["metadata"]["uid"]
    selector = deployment["spec"]["selector"]["matchLabels"]
    selector_text = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = _kubectl_json(kubectl, "-n", NAMESPACE, "get", "pods", f"-l={selector_text}")
    items = pods.get("items", [])
    if len(items) != 1:
        raise RuntimeError(f"expected one retained PostgreSQL Pod, found {len(items)}")
    pod = items[0]
    if pod.get("status", {}).get("phase") != "Running":
        raise RuntimeError(f"retained PostgreSQL Pod is not Running: {pod.get('status', {}).get('phase')}")
    containers = [
        status
        for status in pod.get("status", {}).get("containerStatuses", [])
        if status.get("name") == POSTGRES_CONTAINER
    ]
    if len(containers) != 1 or not containers[0].get("ready"):
        raise RuntimeError("retained PostgreSQL container is not ready")
    container = containers[0]
    if not container.get("containerID"):
        raise RuntimeError("retained PostgreSQL container has no runtime ID")
    return {
        "deployment_uid": deployment_uid,
        "pod_name": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "container_id": container["containerID"],
        "restart_count": container["restartCount"],
    }


def _psql(
    kubectl: certificates.Kubectl,
    pod_name: str,
    *,
    input_text: str | None = None,
    arguments: tuple[str, ...] = (),
    timeout: int = _SQL_TIMEOUT,
) -> str:
    args = ["--request-timeout=" + _REQUEST_TIMEOUT, "-n", NAMESPACE, "exec"]
    if input_text is not None:
        args.append("-i")
    args.extend(
        [
            pod_name,
            "-c",
            POSTGRES_CONTAINER,
            "--",
            "psql",
            "-X",
            "--quiet",
            "--no-password",
            f"--username={ADMIN_ROLE}",
            "--host=/tmp",
            *arguments,
        ]
    )
    return kubectl(*args, input_text=input_text, timeout=timeout)


def _inspection_script(databases: tuple[str, ...], app_database: str) -> str:
    literals = ", ".join(f"'{database}'" for database in databases)
    lines = [
        "SELECT 'system_identifier', system_identifier::text FROM pg_control_system();",
        "SELECT 'postmaster_start_time', pg_postmaster_start_time()::text;",
        f"SELECT 'database_oid', datname, oid::text FROM pg_database WHERE datname IN ({literals}) ORDER BY datname;",
    ]
    for database in databases:
        lines.extend([f"\\connect {database}", _USER_RELATIONS_SQL])
        if database == app_database:
            lines.append(
                "SELECT 'sentinel_present', (to_regclass('public.postgres_persistence_sentinel') IS NOT NULL)::text;"
            )
    return "\n".join(lines) + "\n"


def _inspect(kubectl: certificates.Kubectl, baseline: Baseline) -> _Inspection:
    process_identity = _process_identity(kubectl)
    credentials = _credential_secrets(baseline.objects, namespace=NAMESPACE)
    app_database = _assert_app_database(baseline, credentials)
    output = _psql(
        kubectl,
        process_identity["pod_name"],
        input_text=_inspection_script(baseline.databases, app_database),
        arguments=(
            "--dbname=postgres",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--field-separator=|",
            "--file=-",
        ),
    )

    system_identifier: str | None = None
    postmaster_start_time: str | None = None
    database_oids: dict[str, str] = {}
    user_relation_counts: dict[str, int] = {}
    sentinel_present: bool | None = None
    for line in output.splitlines():
        fields = line.strip().split("|")
        if fields[0] == "system_identifier" and len(fields) == 2:
            system_identifier = fields[1]
        elif fields[0] == "postmaster_start_time" and len(fields) == 2:
            postmaster_start_time = fields[1]
        elif fields[0] == "database_oid" and len(fields) == 3:
            database_oids[fields[1]] = fields[2]
        elif fields[0] == "user_relations" and len(fields) == 3:
            user_relation_counts[fields[1]] = int(fields[2])
        elif fields[0] == "sentinel_present" and len(fields) == 2 and fields[1] in {"true", "false"}:
            sentinel_present = fields[1] == "true"

    if system_identifier is None or postmaster_start_time is None:
        raise RuntimeError("could not read PostgreSQL system identifier and postmaster start time")
    if set(database_oids) != set(baseline.databases):
        raise RuntimeError(f"PostgreSQL database OIDs differ from fixture databases: {set(database_oids)}")
    if set(user_relation_counts) != set(baseline.databases):
        raise RuntimeError(f"could not inspect every fixture database: {set(user_relation_counts)}")
    if sentinel_present is None:
        raise RuntimeError("could not inspect the PostgreSQL reset sentinel")
    return _Inspection(
        identity=process_identity
        | {"system_identifier": system_identifier, "postmaster_start_time": postmaster_start_time},
        database_oids=database_oids,
        user_relation_counts=user_relation_counts,
        sentinel_present=sentinel_present,
    )


def _evidence(inspection: _Inspection, *, outcome: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "identity": inspection.identity,
        "database_oids": inspection.database_oids,
        "user_relation_counts": inspection.user_relation_counts,
    }


def _require_empty(inspection: _Inspection, *, action: str) -> None:
    dirty = {database: count for database, count in inspection.user_relation_counts.items() if count != 0}
    if dirty:
        raise RuntimeError(f"PostgreSQL fixture databases contain user relations {action}: {dirty}")


@tracer.start_as_current_span("system_test.postgres.ensure")
def ensure(kubectl: certificates.Kubectl, baseline: Baseline, *, warm: bool, marker: dict[str, Any]) -> dict[str, Any]:
    if not warm:
        existing = kubectl(
            "get",
            "namespace",
            NAMESPACE,
            "--ignore-not-found=true",
            "-o",
            "name",
            f"--request-timeout={_REQUEST_TIMEOUT}",
            timeout=_NAMESPACE_TIMEOUT,
        )
        if existing.strip():
            raise RuntimeError("cold PostgreSQL setup found an existing infrastructure namespace")
        namespace, *resources = baseline.objects
        kubectl(
            "apply",
            "--server-side",
            f"--request-timeout={_REQUEST_TIMEOUT}",
            "-f",
            "-",
            input_text=yaml.safe_dump(namespace),
            timeout=_APPLY_TIMEOUT,
        )
        kubectl(
            "apply",
            "--server-side",
            f"--request-timeout={_REQUEST_TIMEOUT}",
            "-f",
            "-",
            input_text=yaml.safe_dump_all(resources),
            timeout=_APPLY_TIMEOUT,
        )
        kubectl(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{POSTGRES_DEPLOYMENT}",
            f"--timeout={_ROLLOUT_TIMEOUT - 10}s",
            f"--request-timeout={_REQUEST_TIMEOUT}",
            timeout=_ROLLOUT_TIMEOUT,
        )

    inspection = _inspect(kubectl, baseline)
    if warm:
        previous_identity = marker.get("postgres_identity")
        previous_oids = marker.get("postgres_database_oids")
        if previous_identity != inspection.identity:
            raise RuntimeError("warm PostgreSQL process identity differs from the retained marker")
        if previous_oids != inspection.database_oids:
            raise RuntimeError("warm PostgreSQL database OIDs differ from the retained marker")
    _require_empty(inspection, action="before the Pod suite")

    marker["postgres_identity"] = inspection.identity
    marker["postgres_database_oids"] = inspection.database_oids
    return _evidence(inspection, outcome="warm" if warm else "cold")


def _assert_application_namespaces_deleted(kubectl: certificates.Kubectl, baseline: Baseline) -> None:
    namespaces = {
        resource["metadata"]["name"] for resource in baseline.fixture.objects if resource.get("kind") == "Namespace"
    }
    for namespace in sorted(namespaces):
        present = kubectl(
            "get",
            "namespace",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "name",
            f"--request-timeout={_REQUEST_TIMEOUT}",
            timeout=_NAMESPACE_TIMEOUT,
        )
        if present.strip():
            raise RuntimeError(f"application namespace {namespace} still exists during PostgreSQL reset")


def _bootstrap_sql(baseline: Baseline) -> str:
    for resource in baseline.objects:
        if resource.get("kind") == "ConfigMap" and resource["metadata"]["name"] == BOOTSTRAP_CONFIG_MAP:
            return resource["data"]["bootstrap.sql"]
    raise ValueError("retained PostgreSQL baseline has no bootstrap ConfigMap")


def _assert_app_database(baseline: Baseline, credentials: list[_Credential]) -> str:
    app_credentials = [credential for credential in credentials if credential.role == "app"]
    if len(app_credentials) != 1 or app_credentials[0].database not in baseline.databases:
        raise ValueError("retained PostgreSQL baseline has no unique app database")
    return app_credentials[0].database


@tracer.start_as_current_span("system_test.postgres.reset")
def clean(kubectl: certificates.Kubectl, baseline: Baseline, marker: dict[str, Any]) -> dict[str, Any]:
    _assert_application_namespaces_deleted(kubectl, baseline)
    credentials = _credential_secrets(baseline.objects, namespace=NAMESPACE)
    if tuple(credential.database for credential in credentials) != baseline.databases:
        raise ValueError("retained PostgreSQL credential databases differ from the baseline")
    app_database = _assert_app_database(baseline, credentials)
    bootstrap_sql = _bootstrap_sql(baseline)

    before = _inspect(kubectl, baseline)
    if marker.get("postgres_identity") != before.identity:
        raise RuntimeError("PostgreSQL process identity changed before its reset")
    if marker.get("postgres_database_oids") != before.database_oids:
        raise RuntimeError("PostgreSQL database OIDs changed before their reset")

    pod_name = before.identity["pod_name"]
    sentinel_sql = (
        "CREATE TABLE public.postgres_persistence_sentinel (value text PRIMARY KEY);\n"
        "INSERT INTO public.postgres_persistence_sentinel (value) VALUES ('retained-postgres-reset');"
    )
    _psql(kubectl, pod_name, arguments=(f"--dbname={app_database}", "--set=ON_ERROR_STOP=1", "--command", sentinel_sql))

    reset_sql = [f'DROP DATABASE "{database}" WITH (FORCE);' for database in baseline.databases]
    reset_sql.extend(f'DROP ROLE "{credential.role}";' for credential in credentials)
    reset_sql.append(f'DROP ROLE "{ELECTRIC_ROLE}";')
    _psql(
        kubectl,
        pod_name,
        input_text="\n".join(reset_sql) + "\n",
        arguments=("--dbname=postgres", "--set=ON_ERROR_STOP=1", "--file=-"),
    )
    _psql(
        kubectl,
        pod_name,
        input_text=bootstrap_sql,
        arguments=("--dbname=postgres", "--set=ON_ERROR_STOP=1", "--file=-"),
    )

    after = _inspect(kubectl, baseline)
    if after.identity != before.identity:
        raise RuntimeError("PostgreSQL process identity changed during its database reset")
    if any(after.database_oids[database] == before.database_oids[database] for database in baseline.databases):
        raise RuntimeError("PostgreSQL reset did not change every service database OID")
    _require_empty(after, action="after reset")

    if after.sentinel_present:
        raise RuntimeError("PostgreSQL reset left the sentinel table in the app database")

    marker["postgres_identity"] = after.identity
    marker["postgres_database_oids"] = after.database_oids
    return {
        "identity": after.identity,
        "database_oids_before": before.database_oids,
        "database_oids": after.database_oids,
        "user_relation_counts": after.user_relation_counts,
        "checks": {
            "same_process_identity": True,
            "all_database_oids_changed": True,
            "zero_user_relations": True,
            "sentinel_absent": not after.sentinel_present,
        },
    }
