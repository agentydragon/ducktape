"""Authentik server and worker telemetry from the metrics they currently export."""

from grafana_foundation_sdk.models import dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import (
    line_style,
    mimir,
    mimir_multi_variable,
    mimir_query,
    series_options,
    stat_options,
)

_SERVER = 'namespace=~"$namespace",job="authentik/server"'
_WORKER = 'namespace=~"$namespace",job="authentik/worker"'


def _stat(
    id_val: int, title: str, expression: str, description: str, x: int, *, health: bool = False
) -> dashboard.Panel:
    return dashboard.Panel(
        type_val="stat",
        id_val=id_val,
        title=title,
        description=description,
        targets=[mimir_query(expression, legend="").instant().build()],
        datasource=mimir(),
        grid_pos=dashboard.GridPos(h=5, w=4, x=x, y=1),
        options=stat_options(),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(
                unit="short",
                decimals=0,
                thresholds=dashboard.ThresholdsConfig(
                    steps=[dashboard.Threshold(color="red"), dashboard.Threshold(value=1, color="green")]
                )
                if health
                else None,
            )
        ),
    )


def _series(
    id_val: int, title: str, expression: str, legend: str, description: str, unit: str, x: int, y: int
) -> dashboard.Panel:
    return dashboard.Panel(
        type_val="timeseries",
        id_val=id_val,
        title=title,
        description=description,
        targets=[mimir_query(expression, legend=legend).build()],
        datasource=mimir(),
        grid_pos=dashboard.GridPos(h=8, w=12, x=x, y=y),
        options=series_options(),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(unit=unit, min_val=0, custom=line_style())
        ),
    )


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="authentik",
        title="Authentik",
        description="Current Authentik server and worker health, requests, task processing, and policy evaluation.",
        tags=["authentik", "identity"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-1h"),
        refresh="1m",
        schema_version=39,
        version=1,
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                mimir_multi_variable(
                    name="namespace",
                    label="Namespace",
                    query='query_result(max by (namespace) (up{job=~"authentik/server|authentik/worker"}))',
                    current=dashboard.VariableOption(selected=True, text="authentik", value=["authentik"]),
                    all_value=".*",
                    regex='/namespace="([^"]+)"/',
                )
            ]
        ),
        panels=[
            dashboard.RowPanel(title="Service status", id_val=1, grid_pos=dashboard.GridPos(h=1, w=24)),
            _stat(
                2,
                "Server scrape",
                f"min(up{{{_SERVER}}}) or vector(0)",
                "One means every Authentik server metrics target is currently scraped.",
                0,
                health=True,
            ),
            _stat(
                3,
                "Worker scrape",
                f"min(up{{{_WORKER}}}) or vector(0)",
                "One means every Authentik worker metrics target is currently scraped; task panels depend on it.",
                4,
                health=True,
            ),
            _stat(
                4,
                "Current workers",
                f"sum(authentik_tasks_workers{{{_WORKER}}})",
                "Connected worker processes, including any with a version mismatch. This is distinct from scrape health.",
                8,
                health=True,
            ),
            _stat(
                5,
                "Connected outposts",
                f"sum(max by (uid) (authentik_outposts_connected{{{_SERVER}}}))",
                "Connected outposts, deduplicated across server processes by UID. No data means the metric is absent.",
                12,
                health=True,
            ),
            _stat(
                6,
                "Queued tasks",
                f"sum(authentik_tasks_queued{{{_WORKER}}})",
                "Tasks currently queued in the worker. Zero is healthy when measured with no backlog; no data means the metric is absent.",
                16,
            ),
            _stat(
                7,
                "Cached policies",
                f"max(authentik_policies_cached{{{_SERVER}}})",
                "Policy objects currently cached by the server.",
                20,
            ),
            dashboard.RowPanel(title="Requests", id_val=8, grid_pos=dashboard.GridPos(h=1, w=24, y=6)),
            _series(
                9,
                "Requests by destination",
                f"sum by (dest) (rate(authentik_main_request_duration_seconds_count{{{_SERVER}}}[5m]))",
                "{{dest}}",
                "Server requests per second by destination, averaged over five minutes.",
                "reqps",
                0,
                7,
            ),
            _series(
                10,
                "Mean request duration",
                f"sum by (dest) (rate(authentik_main_request_duration_seconds_sum{{{_SERVER}}}[5m])) / sum by (dest) (rate(authentik_main_request_duration_seconds_count{{{_SERVER}}}[5m]))",
                "{{dest}}",
                "Mean server request duration in seconds over five minutes; a destination without requests has no value.",
                "s",
                12,
                7,
            ),
            dashboard.RowPanel(title="Worker tasks", id_val=11, grid_pos=dashboard.GridPos(h=1, w=24, y=15)),
            _series(
                12,
                "Tasks processed by actor",
                f"topk(8, sum by (actor_name) (rate(authentik_tasks_total{{{_WORKER}}}[5m])))",
                "{{actor_name}}",
                "Top eight task actors by processed tasks per second over five minutes. This counter has no success/error status label.",
                "ops",
                0,
                16,
            ),
            _series(
                13,
                "Mean task duration by actor",
                f"topk(8, sum by (actor_name) (rate(authentik_tasks_duration_milliseconds_sum{{{_WORKER}}}[5m])) / sum by (actor_name) (rate(authentik_tasks_duration_milliseconds_count{{{_WORKER}}}[5m])))",
                "{{actor_name}}",
                "Top eight mean processing durations among actors with completed tasks in the five-minute window. Units are milliseconds.",
                "ms",
                12,
                16,
            ),
            dashboard.RowPanel(title="Policy evaluation", id_val=14, grid_pos=dashboard.GridPos(h=1, w=24, y=24)),
            _series(
                15,
                "Policy engine evaluations by object type",
                f"sum by (obj_type) (rate(authentik_policies_engine_time_total_seconds_count{{{_SERVER}}}[5m]))",
                "{{obj_type}}",
                "Server policy-engine evaluations per second by object type, averaged over five minutes.",
                "ops",
                0,
                25,
            ),
            _series(
                16,
                "Mean policy engine duration",
                f"sum by (obj_type) (rate(authentik_policies_engine_time_total_seconds_sum{{{_SERVER}}}[5m])) / sum by (obj_type) (rate(authentik_policies_engine_time_total_seconds_count{{{_SERVER}}}[5m]))",
                "{{obj_type}}",
                "Mean server policy-engine evaluation duration in seconds over five minutes; idle object types have no value.",
                "s",
                12,
                25,
            ),
            _series(
                17,
                "Worker policy executions by binding type",
                f"sum by (binding_target_type) (rate(authentik_policies_execution_time_count{{{_WORKER}}}[5m]))",
                "{{binding_target_type}}",
                "Worker policy executions per second by binding target type, averaged over five minutes.",
                "ops",
                0,
                34,
            ),
        ],
    )
