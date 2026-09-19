"""The Job that bootstraps ClickHouse databases with distributed DDL. Database creation
is ClickHouse-admin-only, so it lives here; everything inside each database is owned by
its app (aiquota's `migrate` init container, Langfuse's own migration runner).

schema.sql, hand-written beside the generated output, is the DDL; the generated
`kustomization.yaml` renders it into the ConfigMap the Job mounts. A Job's template is
immutable, so the Job name carries a version: bump it to re-run after any change to the
Job or its schema.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Duration
from cdk8s_plus_34 import ConfigMap, Job, PodSecurityContextProps, RestartPolicy, Secret
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    NAMESPACE as FLUX_NAMESPACE,
    ConfigMapArgs,
    flux_kustomization,
    health_checks,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch

NAME = "clickhouse-schema"
OUTPUT_DIR = "cluster/k8s/clickhouse/schema"
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
        metadata=metadata(
            _JOB_NAME,
            NAMESPACE,
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
    ApiObject.of(job).add_json_patch(runtime_default_seccomp_patch())
    return chart


def write_manifests(root: Path) -> None:
    name = NAME
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    rendered_chart = chart(app)
    add_fleet_rules(
        rendered_chart,
        provided_secrets={"clickhouse-admin-credentials": "clickhouse"},
        # schema.sql (hand-written, listed below) always provides its own ConfigMap: the
        # generated kustomization.yaml's config_map_generator entry renders it from
        # exactly that file.
        provided_config_maps={SCHEMA_CONFIG_MAP.name: "schema.sql"},
        providers=frozenset({"clickhouse", "schema.sql"}),
    )
    app.synth()

    write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="20m",
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=FLUX_NAMESPACE
                ),
                path=f"./{OUTPUT_DIR}",
                prune=True,
                wait=True,
                health_checks=health_checks(rendered_chart, ("Job",)),
                depends_on=[KustomizationSpecDependsOn(name="clickhouse")],
            ),
        ),
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        # schema.sql stays hand-written; the generator entry renders it into the ConfigMap the
        # Job mounts. See cluster/docs/cdk8s.md.
        kustomize_kustomization(resources=[f"{name}.k8s.yaml"], config_map_generator=[SCHEMA_CONFIG_MAP]),
    )
