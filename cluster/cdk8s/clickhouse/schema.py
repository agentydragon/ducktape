"""The Job that bootstraps ClickHouse databases with distributed DDL. Database creation
is ClickHouse-admin-only, so it lives here; everything inside each database is owned by
its app (aiquota's `migrate` init container, Langfuse's own migration runner).

schema.sql, hand-written beside the generated output, is the DDL; the generated
`kustomization.yaml` renders it into the ConfigMap the Job mounts. A Job's template is
immutable, so the Job name carries a version: bump it to re-run after any change to the
Job or its schema.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Duration
from cdk8s_plus_34 import ConfigMap, Job, PodSecurityContextProps, RestartPolicy, Secret

from cluster.cdk8s import pod_policy
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "clickhouse-schema"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/clickhouse/schema"
NAMESPACE = "clickhouse"
SCHEMA_CONFIG_MAP = ConfigMapArgs(name="clickhouse-aiquota-schema", namespace=NAMESPACE, files=[client.SCHEMA_FILE])
_JOB_NAME = "clickhouse-aiquota-schema-v10"
_LABELS = {
    "app.kubernetes.io/name": NAME,
    "app.kubernetes.io/instance": "clickhouse",
    "app.kubernetes.io/component": "schema",
}
_CLICKHOUSE_UID = 101  # the image's `clickhouse` user


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    job = Job(
        chart,
        "job",
        metadata=ApiObjectMetadata(
            name=_JOB_NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Bootstraps the langfuse and aiquota databases with ClickHouse distributed DDL. "
                    "Table/view schema inside each database is owned by its app."
                )
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_LABELS),
        active_deadline=Duration.minutes(20),
        backoff_limit=60,
        restart_policy=RestartPolicy.ON_FAILURE,
        automount_service_account_token=False,
        enable_service_links=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=_CLICKHOUSE_UID, group=_CLICKHOUSE_UID),
        containers=[
            client.queries_file_container(
                chart,
                "schema",
                schema=ConfigMap.from_config_map_name(chart, "schema-ref", SCHEMA_CONFIG_MAP.name),
                credentials=Secret.from_secret_name(chart, "admin-credentials-ref", "clickhouse-admin-credentials"),
            )
        ],
    )
    pod_policy.harden(job)
    add_fleet_rules(chart)
    return chart


def clickhouse_schema(flux_chart: Chart, directory: RenderedDirectory, clickhouse: Kustomization) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="20m",
        # bootstrap-never-converges: the Job's 20m deadline runs from apply and Flux never recreates it.
        depends_on=[flux_kustomization_depends_on(clickhouse)],
    )
