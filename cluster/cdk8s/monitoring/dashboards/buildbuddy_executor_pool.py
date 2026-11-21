"""BuildBuddy executor pool dashboard."""

from grafana_foundation_sdk.models import common, dashboard, stat

from cluster.cdk8s.monitoring.dashboards.foundation import last_value, line_style, mimir, mimir_query, series_options

_EXECUTOR_METRICS = 'namespace="buildbuddy-executor",service="buildbuddy-kvm-worker-metrics",endpoint="metrics"'
_GUEST_METRICS = 'namespace="buildbuddy-executor",service="buildbuddy-kvm-worker-metrics",endpoint="node-metrics"'
_CURRENT_EXECUTORS = 'and on (namespace, pod) kube_pod_info{namespace="buildbuddy-executor"}'


def _guest_filesystem(metric: str, mount: str) -> str:
    return f'node_filesystem_{metric}_bytes{{{_GUEST_METRICS},mountpoint="{mount}",pod=~"virt-launcher-buildbuddy-kvm-worker-[0-9]+-.*"}}'


def _worker_max(expression: str) -> str:
    return f'max by (worker) (label_replace(({expression}), "worker", "$1", "pod", "^virt-launcher-(buildbuddy-kvm-worker-[0-9]+)-.*$"))'


def _filesystem_used(mount: str) -> str:
    return _worker_max(f"{_guest_filesystem('size', mount)} - {_guest_filesystem('free', mount)}")


def _filesystem_unavailable(mount: str) -> str:
    return _worker_max(f"100 * (1 - {_guest_filesystem('avail', mount)} / {_guest_filesystem('size', mount)})")


def _series_fields(unit: str = "short", max_val: float | None = None) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit=unit,
            min_val=0,
            max_val=max_val,
            color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC),
            custom=line_style(draw_style=None),
        )
    )


def _healthy_count_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="short",
            decimals=0,
            thresholds=dashboard.ThresholdsConfig(
                steps=[dashboard.Threshold(color="red"), dashboard.Threshold(value=1, color="green")]
            ),
        )
    )


def _count_stat_options() -> stat.Options:
    return stat.Options(text_mode=common.BigValueTextMode.VALUE, reduce_options=last_value())


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="buildbuddy-executor-pool",
        title="BuildBuddy executor pool",
        description="The in-cluster kvm-vm workers: health, executor activity, guest resources and scheduler reservations.",
        tags=["ci", "buildbuddy", "executor"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.RowPanel(title="Pool status", grid_pos=dashboard.GridPos(h=1, w=24), id_val=1),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query(f"sum(up{{{_EXECUTOR_METRICS}}} {_CURRENT_EXECUTORS}) or vector(0)", legend="")
                    .instant()
                    .build()
                ],
                title="Executor scrapes up",
                description="Number of executor metrics endpoints successfully scraped. Zero also covers a missing scrape target. Filtered to pods still present in kube-state-metrics.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, w=6, y=1),
                options=_count_stat_options(),
                field_config=_healthy_count_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[
                    mimir_query(
                        f'sum(buildbuddy_health_check_status{{{_EXECUTOR_METRICS},health_check_name="registered_to_scheduler"}} {_CURRENT_EXECUTORS}) or vector(0)',
                        legend="",
                    )
                    .instant()
                    .build()
                ],
                title="Registered executors",
                description="Executors reporting a healthy registered_to_scheduler check. This is not the configured worker count; missing metrics contribute no healthy executors. Filtered to pods still present in kube-state-metrics.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, w=6, x=6, y=1),
                options=_count_stat_options(),
                field_config=_healthy_count_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=4,
                targets=[
                    mimir_query(
                        f"sum(buildbuddy_remote_execution_tasks_executing{{{_EXECUTOR_METRICS}}} {_CURRENT_EXECUTORS}) or vector(0)",
                        legend="",
                    )
                    .instant()
                    .build()
                ],
                title="Tasks in progress",
                description="Tasks in executor stages, including preparation, execution and upload; zero includes missing metrics. Filtered to pods still present in kube-state-metrics.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, w=6, x=12, y=1),
                options=_count_stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        decimals=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=5,
                targets=[
                    mimir_query(f"sum(up{{{_GUEST_METRICS}}} {_CURRENT_EXECUTORS}) or vector(0)", legend="")
                    .instant()
                    .build()
                ],
                title="Guest scrapes up",
                description="Number of worker guest node-exporter endpoints successfully scraped. Guest CPU, memory and filesystem panels require these scrapes. Filtered to pods still present in kube-state-metrics.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, w=6, x=18, y=1),
                options=_count_stat_options(),
                field_config=_healthy_count_fields(),
            ),
            dashboard.RowPanel(title="Executor activity", grid_pos=dashboard.GridPos(h=1, w=24, y=6), id_val=6),
            dashboard.Panel(
                type_val="timeseries",
                id_val=7,
                targets=[
                    mimir_query(
                        f"sum by (stage) (buildbuddy_remote_execution_tasks_executing{{{_EXECUTOR_METRICS}}})",
                        legend="{{stage}}",
                    ).build()
                ],
                title="Tasks by stage",
                description="Current executor-side tasks by stage, summed across the KVM pool. A task occupies one stage at a time. Scrape intervals may miss short tasks.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=7),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query(
                        f"sum by (pod) (buildbuddy_remote_execution_tasks_executing{{{_EXECUTOR_METRICS}}})",
                        legend="{{pod}}",
                    ).build()
                ],
                title="Tasks per worker",
                description="Tasks in all execution stages per worker. This shows executor occupancy, not queued central scheduler work.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=7),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query(
                        f"buildbuddy_health_check_status{{{_EXECUTOR_METRICS}}}",
                        legend="{{pod}} · {{health_check_name}}",
                    ).build()
                ],
                title="Executor health checks",
                description="A value of 1 means the named executor check is healthy; 0 means it failed. Missing series are not healthy. Registration is the check required by the worker readiness probe.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=16),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(max_val=1),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[
                    mimir_query(
                        f"sum by (pod) (buildbuddy_remote_execution_queue_length{{{_EXECUTOR_METRICS}}})",
                        legend="{{pod}}",
                    ).build()
                ],
                title="Local executor queue",
                description="Actions waiting in each executor's local queue, summed over group labels. This is not the BuildBuddy scheduler's pool backlog or a count of distinct pending actions.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=16),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(),
            ),
            dashboard.RowPanel(title="Guest resources", grid_pos=dashboard.GridPos(h=1, w=24, y=25), id_val=11),
            dashboard.Panel(
                type_val="timeseries",
                id_val=12,
                targets=[
                    mimir_query(
                        f'100 * (1 - avg by (pod) (rate(node_cpu_seconds_total{{{_GUEST_METRICS},mode="idle"}}[5m])))',
                        legend="{{pod}}",
                    ).build()
                ],
                title="Guest CPU busy",
                description="100% minus idle time across the guest vCPUs, averaged over five minutes. Busy includes I/O wait and steal time.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(unit="percent", max_val=100),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[
                    mimir_query(
                        f"max by (pod) (node_memory_MemTotal_bytes{{{_GUEST_METRICS}}} - node_memory_MemAvailable_bytes{{{_GUEST_METRICS}}})",
                        legend="{{pod}}",
                    ).build()
                ],
                title="Guest RAM used",
                description="Guest MemTotal minus MemAvailable: the Linux estimate of memory not readily available to new work. It includes the executor, Docker and actions, and is not a sum of process RSS.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, x=8, y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(unit="bytes"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    mimir_query(
                        f"100 * (1 - max by (pod) (node_memory_MemAvailable_bytes{{{_GUEST_METRICS}}}) / max by (pod) (node_memory_MemTotal_bytes{{{_GUEST_METRICS}}}))",
                        legend="{{pod}}",
                    ).build()
                ],
                title="Guest RAM used %",
                description="Guest memory in use, relative to the guest's MemTotal; MemAvailable includes reclaimable cache.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, x=16, y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(unit="percent", max_val=100),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=20,
                targets=[
                    mimir_query(_filesystem_used("/"), legend="{{worker}} used").build(),
                    mimir_query(
                        _worker_max(_guest_filesystem("size", "/")), legend="{{worker}} capacity", ref_id="B"
                    ).build(),
                ],
                title="Root filesystem used and capacity",
                description="Allocated bytes and total capacity of each guest root filesystem. This does not include the separate /boot or /buildbuddy filesystems. Series are grouped by stable worker identity across launcher-pod replacements; overlapping samples keep the maximum.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, y=35),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields(unit="bytes"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    mimir_query(_filesystem_used("/buildbuddy"), legend="{{worker}} used").build(),
                    mimir_query(
                        _worker_max(_guest_filesystem("size", "/buildbuddy")), legend="{{worker}} capacity", ref_id="B"
                    ).build(),
                ],
                title="Work filesystem used and capacity",
                description="Allocated bytes and total capacity inside each guest /buildbuddy filesystem. These are distinct from its backing PVC or physical thin pool. Series are grouped by stable worker identity across launcher-pod replacements; overlapping samples keep the maximum.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, x=12, y=35),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields(unit="bytes"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=21,
                targets=[mimir_query(_filesystem_unavailable("/"), legend="{{worker}}").build()],
                title="Root filesystem unavailable %",
                description="Percentage of root filesystem capacity not available to unprivileged processes, including reserved ext4 blocks. Series are grouped by stable worker identity across launcher-pod replacements; overlapping samples keep the maximum percentage.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=46),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields(unit="percent", max_val=100),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=16,
                targets=[mimir_query(_filesystem_unavailable("/buildbuddy"), legend="{{worker}}").build()],
                title="Work filesystem unavailable %",
                description="Percentage of /buildbuddy capacity not available to unprivileged processes, including reserved ext4 blocks. Series are grouped by stable worker identity across launcher-pod replacements; overlapping samples keep the maximum percentage.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=46),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields(unit="percent", max_val=100),
            ),
            dashboard.RowPanel(
                title="Scheduler resource reservations", grid_pos=dashboard.GridPos(h=1, w=24, y=55), id_val=17
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=18,
                targets=[
                    mimir_query(
                        f"buildbuddy_remote_execution_assigned_milli_cpu{{{_EXECUTOR_METRICS}}} / 1000",
                        legend="{{pod}} assigned",
                    ).build(),
                    mimir_query(
                        f"buildbuddy_remote_execution_assignable_milli_cpu{{{_EXECUTOR_METRICS}}} / 1000",
                        legend="{{pod}} advertised",
                        ref_id="B",
                    ).build(),
                ],
                title="Assigned and advertised CPU",
                description="BuildBuddy estimated CPU assigned to tasks and the maximum advertised by each executor, in CPU cores. These are reservations, not measured guest CPU use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=56),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=19,
                targets=[
                    mimir_query(
                        f"buildbuddy_remote_execution_assigned_ram_bytes{{{_EXECUTOR_METRICS}}}",
                        legend="{{pod}} assigned",
                    ).build(),
                    mimir_query(
                        f"buildbuddy_remote_execution_assignable_ram_bytes{{{_EXECUTOR_METRICS}}}",
                        legend="{{pod}} advertised",
                        ref_id="B",
                    ).build(),
                ],
                title="Assigned and advertised RAM",
                description="BuildBuddy estimated RAM assigned to tasks and the maximum advertised by each executor. These are reservations, not measured guest memory use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=56),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_series_fields(unit="bytes"),
            ),
        ],
    )
