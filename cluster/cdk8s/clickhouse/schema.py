"""The Job that bootstraps ClickHouse databases with distributed DDL. Database creation
is ClickHouse-admin-only, so it lives here; everything inside each database is owned by
its app (aiquota's `migrate` init container, Langfuse's own migration runner).

`schema.sql` beside this module is the DDL, copied into the output directory; the generated
`kustomization.yaml` renders it into the ConfigMap the Job mounts. A Job's template is
immutable, so the Job name carries a version: bump it to re-run after any change to the
Job or its schema.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Duration
from cdk8s_plus_34 import ConfigMap, Job, PodSecurityContextProps, RestartPolicy

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
from cluster.cdk8s.generation import copy_source_file
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "clickhouse-schema"
OUTPUT_DIR = f"{GENERATED_ROOT}/clickhouse/schema"
NAMESPACE = "clickhouse"
_CONFIG_MAP = "clickhouse-aiquota-schema"
_JOB_NAME = "clickhouse-aiquota-schema-v11"
_LABELS = {
    "app.kubernetes.io/name": NAME,
    "app.kubernetes.io/instance": "clickhouse",
    "app.kubernetes.io/component": "schema",
}
_CLICKHOUSE_UID = 101  # the image's `clickhouse` user


def write_config_map(root: Path) -> ConfigMapArgs:
    """Copy `schema.sql` into `OUTPUT_DIR`; return the `configMapGenerator` entry packaging it."""
    return ConfigMapArgs(
        name=_CONFIG_MAP,
        namespace=NAMESPACE,
        files=[copy_source_file(root, OUTPUT_DIR, f"cluster/cdk8s/clickhouse/{client.SCHEMA_FILE}")],
    )


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
                schema=ConfigMap.from_config_map_name(chart, "schema-ref", _CONFIG_MAP),
                credentials=client.ADMIN_CREDENTIALS,
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
