"""BuildBuddy KVM pool capacity, worker activity and resource pressure."""

from grafana_foundation_sdk.models import common, dashboard, statetimeline

from cluster.cdk8s.monitoring.dashboards.foundation import (
    field_override,
    line_style,
    mimir,
    mimir_multi_variable,
    mimir_query,
    series_options,
)

_SERVICE = 'namespace="buildbuddy-executor",service="buildbuddy-kvm-worker-metrics"'
_EXECUTOR = f'{_SERVICE},endpoint="metrics"'
_WORKER_PREFIX = "virt-launcher-buildbuddy-kvm-worker-"
_WORKER_POD = f'pod=~"{_WORKER_PREFIX}${{worker}}-.*"'
_WORKER_NUMBER_REGEX = f"/^{_WORKER_PREFIX}([0-9]+)-.*$/"
_SELECTED = f"{_EXECUTOR},{_WORKER_POD}"
_GUEST_ENDPOINT = f'{_SERVICE},endpoint="node-metrics"'
_GUEST = f"{_GUEST_ENDPOINT},{_WORKER_POD}"
_CURRENT = 'and on (namespace, pod) kube_pod_info{namespace="buildbuddy-executor"}'
_DESIRED = 'max(kube_customresource_kubevirt_vmpool_desired_replicas{namespace="buildbuddy-executor",name="buildbuddy-kvm-worker"})'


def _registered(selector: str) -> str:
    return f'buildbuddy_health_check_status{{{selector},health_check_name="registered_to_scheduler"}} {_CURRENT}'


def _tasks(selector: str) -> str:
    return f"buildbuddy_remote_execution_tasks_executing{{{selector}}} {_CURRENT}"


def _chart(
    panel_id: int,
    title: str,
    description: str,
    pos: dashboard.GridPos,
    queries: list[tuple[str, str]],
    unit: str = "short",
) -> dashboard.Panel:
    return dashboard.Panel(
        type_val="timeseries",
        id_val=panel_id,
        title=title,
        description=description,
        datasource=mimir(),
        grid_pos=pos,
        targets=[
            mimir_query(expr, legend=label, ref_id=chr(ord("A") + index)).build()
            for index, (label, expr) in enumerate(queries)
        ],
        options=series_options(),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(
                unit=unit, min_val=0, max_val=100 if unit == "percent" else None, custom=line_style()
            )
        ),
    )


def _capacity() -> dashboard.Panel:
    panel = _chart(
        22,
        "Pool capacity and active tasks",
        "Desired is the KubeVirt pool target. Shadow target shows what the trial scale-up policy would request "
        "without changing the pool. Registered counts executors reporting scheduler registration. "
        "Active tasks includes preparation, execution and upload on current workers. Missing samples leave gaps.",
        dashboard.GridPos(h=10, w=24),
        [
            ("Active tasks", f"sum({_tasks(_EXECUTOR)})"),
            ("Registered workers", f"sum({_registered(_EXECUTOR)})"),
            ("Desired workers", _DESIRED),
            ("Shadow target workers", "buildbuddy_kvm:shadow_target_workers"),
        ],
    )
    panel.field_config = dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="short",
            decimals=0,
            min_val=0,
            custom=common.GraphFieldConfig(
                line_interpolation=common.LineInterpolation.STEP_AFTER,
                line_width=2,
                fill_opacity=0,
                show_points=common.VisibilityMode.NEVER,
                span_nulls=False,
            ),
        ),
        overrides=[
            field_override(
                "Active tasks", {"color": {"mode": "fixed", "fixedColor": "green"}, "custom.fillOpacity": 22}
            ),
            field_override(
                "Registered workers", {"color": {"mode": "fixed", "fixedColor": "blue"}, "custom.lineWidth": 3}
            ),
            field_override(
                "Desired workers",
                {
                    "color": {"mode": "fixed", "fixedColor": "#9299a5"},
                    "custom.lineWidth": 5,
                    "custom.lineStyle": {"fill": "dash", "dash": [7, 6]},
                },
            ),
            field_override("Shadow target workers", {"color": {"mode": "fixed", "fixedColor": "purple"}}),
        ],
    )
    return panel


def _activity() -> dashboard.Panel:
    # A new executor can register before its first task creates the stage gauge.
    registered = f"max by (pod) ({_registered(_SELECTED)}) == 1"
    busy = f"(sum by (pod) ({_tasks(_SELECTED)}) > bool 0)"
    state = f"({busy} and ({registered})) or (-1 * ({registered}))"
    worker = f'max by (worker) (label_replace(({state}), "worker", "$1", "pod", "virt-launcher-buildbuddy-kvm-worker-([0-9]+)-.*"))'
    return dashboard.Panel(
        type_val="state-timeline",
        id_val=8,
        title="Activity by worker",
        description="One row per KubeVirt worker number across launcher replacements. Idle and active use the sampled task gauge. "
        "Registered only means the worker has registered but has no task sample. A gap means no registration was observed.",
        datasource=mimir(),
        targets=[mimir_query(worker, legend="Worker {{worker}}").build()],
        grid_pos=dashboard.GridPos(h=9, w=24, y=10),
        options=statetimeline.Options(
            show_value=common.VisibilityMode.NEVER,
            row_height=0.75,
            legend=common.VizLegendOptions(show_legend=True, display_mode=common.LegendDisplayMode.LIST),
        ),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(
                color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                mappings=[
                    dashboard.ValueMap(
                        options={
                            "0": dashboard.ValueMappingResult(text="Idle", color="dark-blue"),
                            "1": dashboard.ValueMappingResult(text="Active", color="green"),
                            "-1": dashboard.ValueMappingResult(text="Registered only", color="#777e89"),
                        }
                    )
                ],
                custom=statetimeline.FieldConfig(fill_opacity=85, line_width=0, span_nulls=False),
            )
        ),
    )


def _health() -> dashboard.Panel:
    panel = _chart(
        9,
        "Worker health and scrape coverage",
        "Counts for the selected workers. Scrape lines count endpoints that responded. Each health line counts "
        "workers reporting that check healthy. When counts coincide, the legend and hover show each series.",
        dashboard.GridPos(h=8, w=24, y=28),
        [
            ("Executor scrape", f"sum(up{{{_SELECTED}}} {_CURRENT})"),
            ("Guest scrape", f"sum(up{{{_GUEST}}} {_CURRENT})"),
            (
                "{{health_check_name}}",
                f"sum by (health_check_name) (buildbuddy_health_check_status{{{_SELECTED}}} {_CURRENT})",
            ),
        ],
    )
    options = series_options()
    options.legend = common.VizLegendOptions(
        show_legend=True,
        display_mode=common.LegendDisplayMode.TABLE,
        placement=common.LegendPlacement.RIGHT,
        calcs=["lastNotNull"],
    )
    panel.options = options
    panel.field_config = dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="short",
            decimals=0,
            min_val=0,
            custom=common.GraphFieldConfig(
                line_interpolation=common.LineInterpolation.STEP_AFTER,
                line_width=2,
                fill_opacity=0,
                show_points=common.VisibilityMode.NEVER,
                span_nulls=False,
            ),
        ),
        # Draw scrape counts with wider strokes so they remain visible under a coincident health line.
        overrides=[
            field_override(
                "Executor scrape",
                {
                    "color": {"mode": "fixed", "fixedColor": "#9299a5"},
                    "custom.lineWidth": 6,
                    "custom.lineStyle": {"fill": "dash", "dash": [9, 6]},
                },
            ),
            field_override(
                "Guest scrape",
                {
                    "color": {"mode": "fixed", "fixedColor": "#4db6ac"},
                    "custom.lineWidth": 4,
                    "custom.lineStyle": {"fill": "dot"},
                },
            ),
            field_override("executor_disk_usage", {"displayName": "Check: executor disk"}),
            field_override("grpc_app_connection", {"displayName": "Check: app connection"}),
            field_override("grpc_cache_connection", {"displayName": "Check: cache connection"}),
            field_override("registered_to_scheduler", {"displayName": "Check: scheduler registration"}),
        ],
    )
    return panel


def _reservation(panel_id: int, title: str, metric: str, unit: str, x: int) -> dashboard.Panel:
    scale = " / 1000" if metric == "milli_cpu" else ""
    resource = "cpu" if metric == "milli_cpu" else "memory"
    host_requests = (
        f'kube_pod_container_resource_requests{{namespace="buildbuddy-executor",{_WORKER_POD},resource="{resource}"}}'
    )
    return _chart(
        panel_id,
        title,
        "Reserved and advertised are BuildBuddy estimates. Host requests sums Kubernetes container requests in scheduled "
        "worker pods, including the compute container and its sidecars.",
        dashboard.GridPos(h=8, w=12, x=x, y=37),
        [
            (label, f"sum(buildbuddy_remote_execution_{kind}_{metric}{{{_SELECTED}}} {_CURRENT}){scale}")
            for label, kind in (("Reserved", "assigned"), ("Advertised", "assignable"))
        ]
        + [
            (
                "Host requests",
                f'sum(max by (pod, container) ({host_requests} and on (namespace, pod) kube_pod_info{{namespace="buildbuddy-executor",node!=""}}))',
            )
        ],
        unit,
    )


def _disk(panel_id: int, title: str, mount: str, x: int) -> dashboard.Panel:
    filesystem = f'{_GUEST},mountpoint="{mount}"'
    return _chart(
        panel_id,
        title,
        "Lowest available space across selected guests. The capacity line is the smallest filesystem size in the selection.",
        dashboard.GridPos(h=8, w=12, x=x, y=53),
        [
            (label, f"min(node_filesystem_{metric}_bytes{{{filesystem}}} {_CURRENT})")
            for label, metric in (("Available", "avail"), ("Capacity", "size"))
        ],
        "bytes",
    )


def _guest_memory_detail() -> list[dashboard.Panel]:
    pod = f'pod=~"{_WORKER_PREFIX}${{memory_worker}}-.*"'
    guest = f"{_GUEST_ENDPOINT},{pod}"

    def meminfo(field: str) -> str:
        return f"node_memory_{field}_bytes{{{guest}}}"

    total = meminfo("MemTotal")
    free = meminfo("MemFree")
    # Keep all categories on the same launcher when replacement pods overlap.
    launcher = (
        f'topk(1, max by (namespace, pod) (kube_pod_start_time{{namespace="buildbuddy-executor",{pod}}})'
        f" and on (namespace, pod) {total})"
    )

    def selected(expr: str) -> str:
        return f"max(({expr}) and on (namespace, pod) ({launcher}))"

    anonymous = meminfo("AnonPages")
    shared = meminfo("Shmem")
    file_cache = f"{meminfo('Cached')} + {meminfo('Buffers')} - {shared}"
    reclaimable = meminfo("SReclaimable")
    unreclaimable = meminfo("SUnreclaim")
    other = f"{total} - ({anonymous} + {shared} + ({file_cache}) + {reclaimable} + {unreclaimable} + {free})"
    categories = [
        ("Anonymous pages", anonymous, "#6887d8"),
        ("File cache + buffers", file_cache, "#48ac99"),
        ("Shared memory / tmpfs", shared, "#dd8b57"),
        ("Reclaimable slab", reclaimable, "#b496d5"),
        ("Unreclaimable slab", unreclaimable, "#dbb94f"),
        ("Other", other, "#8c98af"),
        ("Free", free, "#b5c1cb"),
    ]
    composition = _chart(
        23,
        "Worker ${memory_worker} guest RAM composition",
        "The stack adds up to Linux MemTotal across all guest processes. Anonymous pages can include RAM held by nested VMs. "
        "Shared memory includes tmpfs and is subtracted from Cached before adding Buffers. "
        "Reclaimable and unreclaimable slab partition slab memory. Other is the residual, including kernel "
        "allocations such as page tables and stacks. Free is MemFree. Each sample uses the newest launcher "
        "with guest memory metrics at that time.",
        dashboard.GridPos(h=10, w=16, y=62),
        [(label, selected(expr)) for label, expr, _ in categories],
        "bytes",
    )
    options = series_options()
    options.legend = common.VizLegendOptions(
        show_legend=True,
        display_mode=common.LegendDisplayMode.TABLE,
        placement=common.LegendPlacement.RIGHT,
        calcs=["lastNotNull"],
    )
    composition.options = options
    composition.field_config = dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="bytes",
            min_val=0,
            custom=common.GraphFieldConfig(
                line_width=1,
                fill_opacity=65,
                show_points=common.VisibilityMode.NEVER,
                span_nulls=False,
                stacking=common.StackingConfig(mode=common.StackingMode.NORMAL, group="RAM"),
            ),
        ),
        overrides=[
            field_override(label, {"color": {"mode": "fixed", "fixedColor": color}}) for label, _, color in categories
        ],
    )
    headroom = _chart(
        24,
        "Worker ${memory_worker} guest RAM headroom",
        "Available (MemAvailable) estimates how much RAM new workloads can use without swapping. Linux estimates "
        "how much cache and slab it can reclaim and accounts for reserved pages. Free (MemFree) counts unused RAM.",
        dashboard.GridPos(h=10, w=8, x=16, y=62),
        [("Available", selected(meminfo("MemAvailable"))), ("Free", selected(free))],
        "bytes",
    )
    headroom.options = series_options(legend_calcs=["lastNotNull", "min"])
    return [composition, headroom]


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="buildbuddy-executor-pool",
        title="BuildBuddy executor pool",
        description="KVM pool capacity and worker activity. Worker detail filters the diagnostics. Memory worker picks a single guest for the RAM breakdown.",
        tags=["ci", "buildbuddy", "executor"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-6h"),
        refresh="1m",
        schema_version=39,
        version=1,
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                mimir_multi_variable(
                    name="worker",
                    label="Worker detail",
                    query=f"label_values(up{{{_EXECUTOR}}}, pod)",
                    regex=_WORKER_NUMBER_REGEX,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    all_value="[0-9]+",
                ),
                dashboard.VariableModel(
                    name="memory_worker",
                    label="Memory worker",
                    datasource=mimir(),
                    query=f"label_values(node_memory_MemTotal_bytes{{{_GUEST_ENDPOINT}}}, pod)",
                    regex=_WORKER_NUMBER_REGEX,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.NUMERICAL_ASC,
                    allow_custom_value=False,
                    current=dashboard.VariableOption(text="0", value="0"),
                ),
            ]
        ),
        panels=[
            _capacity(),
            _activity(),
            dashboard.RowPanel(id_val=6, title="Execution and health", grid_pos=dashboard.GridPos(h=1, w=24, y=19)),
            _chart(
                7,
                "Tasks by stage",
                "Concurrent tasks per stage on the selected workers.",
                dashboard.GridPos(h=8, w=12, y=20),
                [("{{stage}}", f"sum by (stage) ({_tasks(_SELECTED)})")],
            ),
            _chart(
                10,
                "Local queue",
                "Sum of reservations waiting at selected executors. Duplicate reservations can refer to the same action.",
                dashboard.GridPos(h=8, w=12, x=12, y=20),
                [("Queued reservations", f"sum(buildbuddy_remote_execution_queue_length{{{_SELECTED}}} {_CURRENT})")],
            ),
            _health(),
            dashboard.RowPanel(
                id_val=11, title="Worker sizing and disk headroom", grid_pos=dashboard.GridPos(h=1, w=24, y=36)
            ),
            _reservation(18, "CPU reservations", "milli_cpu", "cores", 0),
            _reservation(19, "RAM reservations", "ram_bytes", "bytes", 12),
            _chart(
                12,
                "Busiest guest CPU",
                "Maximum five-minute busy percentage across selected guests, including I/O wait and steal.",
                dashboard.GridPos(h=8, w=12, y=45),
                [
                    (
                        "CPU busy",
                        f'max(100 * (1 - avg by (namespace, pod) (rate(node_cpu_seconds_total{{{_GUEST},mode="idle"}}[5m]))) {_CURRENT})',
                    )
                ],
                "percent",
            ),
            _chart(
                15,
                "Guest memory use",
                "Peak across selected guests at each sample. Used memory is MemTotal minus MemAvailable. Select one worker to compare it to its own total.",
                dashboard.GridPos(h=8, w=12, x=12, y=45),
                [
                    (
                        "Most used",
                        f"max((node_memory_MemTotal_bytes{{{_GUEST}}} - node_memory_MemAvailable_bytes{{{_GUEST}}}) {_CURRENT})",
                    ),
                    ("Guest total", f"max(node_memory_MemTotal_bytes{{{_GUEST}}} {_CURRENT})"),
                ],
                "bytes",
            ),
            _disk(20, "Root disk headroom", "/", 0),
            _disk(14, "Work disk headroom", "/buildbuddy", 12),
            dashboard.RowPanel(id_val=25, title="Memory by worker", grid_pos=dashboard.GridPos(h=1, w=24, y=61)),
            *_guest_memory_detail(),
        ],
    )
