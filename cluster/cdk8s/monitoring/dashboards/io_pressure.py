"""IO pressure dashboard."""

from grafana_foundation_sdk.models import bargauge, common, dashboard, table, text, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import (
    field_override,
    last_value,
    mimir,
    mimir_multi_variable,
    mimir_query,
    mimir_table_query,
    storage_tier_variable,
    topk_variable,
)

_PHYSICAL_DISKS = 'device!~"^(loop|ram|fd|dm-|sr|nbd|drbd).*"'
_DISK_MEDIA = 'label_replace(label_replace(max by (namespace, pod, device, rotational) (node_disk_info), "media", "hdd", "rotational", "1"), "media", "ssd", "rotational", "0")'
_EXPORTER_NODES = 'max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})'


def _io_fields(unit: str = "short", max_val: float | None = None) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit=unit, min_val=0, max_val=max_val, custom=common.GraphFieldConfig(fill_opacity=10)
        )
    )


def _io_options(placement: common.LegendPlacement = common.LegendPlacement.BOTTOM) -> timeseries.Options:
    return timeseries.Options(
        legend=common.VizLegendOptions(
            display_mode=common.LegendDisplayMode.TABLE,
            placement=placement,
            show_legend=True,
            sort_by="Max",
            sort_desc=True,
            calcs=["mean", "max"],
        ),
        tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING),
    )


def _disk_rate(metric: str) -> str:
    return f"rate({metric}{{{_PHYSICAL_DISKS}}}[5m])"


def _disk_series(value: str) -> str:
    return (
        f"max by (node, media, device) ((({value})"
        f" * on (namespace, pod, device) group_left (media) {_DISK_MEDIA})"
        f" * on (namespace, pod) group_left (node) {_EXPORTER_NODES})"
    )


_DISK_UTILIZATION = _disk_series(_disk_rate("node_disk_io_time_seconds_total"))
_READ_LATENCY = _disk_series(
    f"{_disk_rate('node_disk_read_time_seconds_total')} / {_disk_rate('node_disk_reads_completed_total')}"
)
_WRITE_LATENCY = _disk_series(
    f"{_disk_rate('node_disk_write_time_seconds_total')} / {_disk_rate('node_disk_writes_completed_total')}"
)
_DISK_IOPS = _disk_series(
    f"{_disk_rate('node_disk_reads_completed_total')} + {_disk_rate('node_disk_writes_completed_total')}"
)


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="io-pressure",
        title="IO pressure",
        tags=["cluster", "storage", "io", "pressure"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read this dashboard",
                grid_pos=dashboard.GridPos(h=6, w=24),
                options=text.Options(
                    content='IO pressure is Linux PSI: the share of wall time in which tasks were stalled waiting for block IO. **Some** means at least one task was stalled; **full** means every non-idle task was stalled at once, so the workload made no progress; **full** is never above **some**. The **IO stall** picker switches the node and pod graphs between the two; the pod table shows both. Node PSI comes from node-exporter (`node_pressure_io_*`); pod PSI comes from cAdvisor\'s pod-level cgroup (`container_pressure_io_*{container=""}`). Pod values are not additive: two pods stalled at the same instant each show 100%.\n\nThe **Storage tier** selector scopes the node list to Kubernetes nodes labeled `storage.allegedly.works/tier`; **Both** selects `ssd` and `hdd`. Nodes without a tier label, including Wyrm2 and its local OpenEBS volumes, are omitted. The disk rows repeat once per selected node. Disk panels come from node-exporter `node_disk_*`, mapped to the Kubernetes node via its exporter pod. Their `hdd`/`ssd` label is the per-device rotational flag from `node_disk_info`, which can differ from the node storage tier. Virtual disks (`vd*`) usually report rotational regardless of what backs them. Device-mapper, DRBD, loop, and optical devices are left out of physical-device panels. The separate DRBD logical throughput panel overlaps backing-device I/O and must not be added to physical throughput. Latency is the mean service time per completed request over 5 minutes, empty when nothing completed; utilization is the fraction of time the device had IO in flight, which saturates at 100% on a spinning disk but not reliably on SSDs with deep queues.'
                ),
            ),
            dashboard.RowPanel(title="Node IO pressure", grid_pos=dashboard.GridPos(h=1, w=24, y=6), id_val=2),
            dashboard.Panel(
                type_val="timeseries",
                id_val=3,
                targets=[
                    mimir_query(
                        'max by (node) (rate(node_pressure_io_${stall}_seconds_total[5m]) * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        legend="{{node}}",
                    ).build()
                ],
                title="Node IO stall: ${stall:text}",
                description="Share of time tasks on the node waited on IO; the IO stall picker at the top switches between some and full.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=7),
                options=_io_options(common.LegendPlacement.RIGHT),
                field_config=_io_fields(unit="percentunit"),
            ),
            dashboard.RowPanel(title="Pod IO pressure", grid_pos=dashboard.GridPos(h=1, w=24, y=15), id_val=4),
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    mimir_query(
                        'topk(${topk}, max by (namespace, pod, node) (rate(container_pressure_io_${stall}_seconds_total{container="", pod!="", node=~"$node"}[5m])))',
                        legend="{{namespace}}/{{pod}} ({{node}})",
                    ).build()
                ],
                title="Top pods by IO stall: ${stall:text}",
                description="Pod cgroup PSI from cAdvisor; the ${topk} pods with the highest current value.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=24, y=16),
                options=_io_options(common.LegendPlacement.RIGHT),
                field_config=_io_fields(unit="percentunit"),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=6,
                targets=[
                    mimir_table_query(
                        'topk(${topk}, max by (namespace, pod, node) (rate(container_pressure_io_waiting_seconds_total{container="", pod!="", node=~"$node"}[5m])))'
                    ).build(),
                    mimir_table_query(
                        'topk(1000, max by (namespace, pod, node) (rate(container_pressure_io_stalled_seconds_total{container="", pod!="", node=~"$node"}[5m])))',
                        ref_id="B",
                    ).build(),
                ],
                title="Pods by IO stall now",
                description="Current 5-minute pod PSI. Rows are the ${topk} pods with the highest 'some' stall.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=24, y=25),
                transformations=[
                    dashboard.DataTransformerConfig(id_val="joinByField", options={"byField": "pod", "mode": "inner"}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time 1": True, "Time 2": True, "namespace 2": True, "node 2": True},
                            "indexByName": {
                                name: index
                                for index, name in enumerate(("namespace 1", "pod", "node 1", "Value #A", "Value #B"))
                            },
                            "renameByName": {
                                "namespace 1": "namespace",
                                "node 1": "node",
                                "Value #A": "Some",
                                "Value #B": "Full",
                            },
                        },
                    ),
                ],
                options=table.Options(sort_by=[common.TableSortByFieldState(display_name="Some", desc=True)]),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", decimals=1)
                ),
            ),
            dashboard.RowPanel(title="Disks, all nodes", grid_pos=dashboard.GridPos(h=1, w=24, y=34), id_val=7),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        ("A", _DISK_UTILIZATION),
                        ("B", _READ_LATENCY),
                        ("C", _WRITE_LATENCY),
                        ("D", _DISK_IOPS),
                    )
                ],
                title="Disks now",
                description="Current 5-minute values per disk. Latency is empty for a disk with no completed requests in the window.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=35),
                transformations=[
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "indexByName": {"node": 0, "device": 1, "media": 2},
                            "renameByName": {
                                "Value #A": "Utilization",
                                "Value #B": "Read latency",
                                "Value #C": "Write latency",
                                "Value #D": "IOPS",
                            },
                        },
                    ),
                ],
                options=table.Options(sort_by=[common.TableSortByFieldState(display_name="Utilization", desc=True)]),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(decimals=2),
                    overrides=[
                        field_override("Utilization", {"unit": "percentunit"}),
                        field_override("Read latency", {"unit": "s"}),
                        field_override("Write latency", {"unit": "s"}),
                        field_override("IOPS", {"unit": "iops"}),
                    ],
                ),
            ),
            dashboard.RowPanel(
                title="Disks on $node", grid_pos=dashboard.GridPos(h=1, w=24, y=45), id_val=9, repeat="node"
            ),
            dashboard.Panel(
                type_val="bargauge",
                id_val=10,
                targets=[mimir_query(_DISK_UTILIZATION, legend="{{device}} {{media}}").instant().build()],
                title="Utilization now",
                description="Fraction of the last 5 minutes each disk had IO in flight.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, y=46),
                options=bargauge.Options(reduce_options=last_value(), orientation=common.VizOrientation.HORIZONTAL),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        display_name="${__field.labels.device} ${__field.labels.media}",
                        unit="percentunit",
                        min_val=0,
                        max_val=1,
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[
                                dashboard.Threshold(color="green"),
                                dashboard.Threshold(value=0.5, color="orange"),
                                dashboard.Threshold(value=0.8, color="red"),
                            ]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[mimir_query(_DISK_UTILIZATION, legend="{{device}} ({{media}})").build()],
                title="Disk utilization",
                description="Fraction of time the device had IO in flight (node_disk_io_time_seconds_total).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=16, x=8, y=46),
                options=_io_options(common.LegendPlacement.RIGHT),
                field_config=_io_fields(unit="percentunit", max_val=1),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=12,
                targets=[mimir_query(_READ_LATENCY, legend="{{device}} ({{media}})").build()],
                title="Disk read latency",
                description="Mean time per completed read over 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=55),
                options=_io_options(common.LegendPlacement.RIGHT),
                field_config=_io_fields(unit="s"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[mimir_query(_WRITE_LATENCY, legend="{{device}} ({{media}})").build()],
                title="Disk write latency",
                description="Mean time per completed write over 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=55),
                options=_io_options(common.LegendPlacement.RIGHT),
                field_config=_io_fields(unit="s"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[mimir_query(_DISK_IOPS, legend="{{device}} ({{media}})").build()],
                title="Disk IOPS",
                description="Completed reads plus writes per second.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, y=64),
                options=_io_options(),
                field_config=_io_fields(unit="iops"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    mimir_query(
                        _disk_series(
                            f"{_disk_rate('node_disk_read_bytes_total')} + {_disk_rate('node_disk_written_bytes_total')}"
                        ),
                        legend="{{device}} ({{media}})",
                    ).build()
                ],
                title="Disk throughput",
                description="Bytes read plus written per second.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, x=8, y=64),
                options=_io_options(),
                field_config=_io_fields(unit="Bps"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=16,
                targets=[
                    mimir_query(
                        _disk_series(_disk_rate("node_disk_io_time_weighted_seconds_total")),
                        legend="{{device}} ({{media}})",
                    ).build()
                ],
                title="Disk queue depth",
                description="Average number of requests in flight (node_disk_io_time_weighted_seconds_total).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=8, x=16, y=64),
                options=_io_options(),
                field_config=_io_fields(),
            ),
            dashboard.RowPanel(title="DRBD logical I/O", grid_pos=dashboard.GridPos(h=1, w=24, y=73), id_val=17),
            dashboard.Panel(
                type_val="timeseries",
                id_val=18,
                targets=[
                    mimir_query(
                        'max by (node, device) ((rate(node_disk_read_bytes_total{device=~"^drbd.*"}[5m]) + rate(node_disk_written_bytes_total{device=~"^drbd.*"}[5m])) * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        legend="{{node}} {{device}}",
                    ).build()
                ],
                title="Logical throughput by node and device",
                description="Read plus written bytes per second by DRBD device. This is logical block-device I/O and overlaps the backing physical devices; do not add it to physical throughput.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(w=24, y=74),
                options=_io_options(),
                field_config=_io_fields(unit="Bps"),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                storage_tier_variable(),
                mimir_multi_variable(
                    name="node",
                    label="Node",
                    query='query_result(max by (node) (node_pressure_io_stalled_seconds_total * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+"}) * on (node) group_left () max by (node) (kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"})))',
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    regex='/node="([^"]+)"/',
                ),
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="stall",
                    label="IO stall",
                    query="some : waiting,full : stalled",
                    current=dashboard.VariableOption(text="some", value="waiting"),
                    options=[
                        dashboard.VariableOption(selected=True, text="some", value="waiting"),
                        dashboard.VariableOption(selected=False, text="full", value="stalled"),
                    ],
                ),
                topk_variable("Pods"),
            ]
        ),
    )
