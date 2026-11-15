"""Node Memory dashboard."""

from grafana_foundation_sdk.models import barchart, common, dashboard, table, text, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import (
    field_override,
    mimir,
    mimir_query,
    mimir_table_query,
    series_options,
)

_EXPORTER_LABELS = 'namespace="monitoring",pod=~"prometheus-node-exporter-.+"'
_NODE_INFO = f'max by (namespace, pod, node) (kube_pod_info{{{_EXPORTER_LABELS},node=~"$node"}})'
_POD_MEMORY_LABELS = 'node=~"$node",pod!="",container!="",container!="POD"'
_RUNNING_MEMORY_REQUESTS = 'sum by (node) (max by (node, namespace, pod, container) (kube_pod_container_resource_requests{resource="memory",unit="byte",node=~"$node"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))'


def _node_memory(metric: str) -> str:
    return f"max by (node) (node_memory_{metric}_bytes{{{_EXPORTER_LABELS}}} * on (namespace, pod) group_left (node) {_NODE_INFO})"


def _pod_working_sets(*, selected: bool = False) -> str:
    labels = _POD_MEMORY_LABELS + (',namespace=~"$namespace"' if selected else "")
    return (
        "sum by (node, namespace, pod) (max by (node, namespace, pod, container) "
        f"(container_memory_working_set_bytes{{{labels}}}"
        ' and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))'
    )


_NODE_TOTAL = _node_memory("MemTotal")
_NODE_AVAILABLE = _node_memory("MemAvailable")
_ALL_POD_WORKING_SETS = _pod_working_sets()
_TOTAL_POD_WORKING_SET = f"sum by (node) ({_ALL_POD_WORKING_SETS})"
_SELECTED_POD_WORKING_SETS = _pod_working_sets(selected=True)

_TOP_POD_WORKING_SETS = f"({_SELECTED_POD_WORKING_SETS}) and on (node, namespace, pod) topk by (node) (6, max_over_time(({_SELECTED_POD_WORKING_SETS})[${{__range_s}}s:1m] @ end()))"
_OTHER_PODS = f"({_TOTAL_POD_WORKING_SET}) - sum by (node) ({_TOP_POD_WORKING_SETS})"
_NODE_USED = f"({_NODE_TOTAL}) - ({_NODE_AVAILABLE})"
_HOST_REMAINDER = f"({_NODE_USED}) - ({_TOTAL_POD_WORKING_SET})"

_MEMORY_PRESSURE = f'max by (node) (kube_node_status_condition{{condition="MemoryPressure",status="true",node=~"$node"}} and on (node) ({_NODE_TOTAL})) or on (node) (0 * max by (node) (kube_node_status_condition{{condition="MemoryPressure",status="false",node=~"$node"}} and on (node) ({_NODE_TOTAL})))'


_NODE_VARIABLE_QUERY = 'query_result(max by (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+"}))))'

_NAMESPACE_VARIABLE_QUERY = 'query_result(max by (namespace) (container_memory_working_set_bytes{node=~"$node",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))'


def _node_table_transforms(columns: tuple[str, ...]) -> list[dashboard.DataTransformerConfig]:
    values = {f"Value #{chr(65 + index)}": title for index, title in enumerate(columns)}
    return [
        dashboard.DataTransformerConfig(id_val="joinByField", options={"byField": "node", "mode": "outerTabular"}),
        dashboard.DataTransformerConfig(
            id_val="organize",
            options={
                "excludeByName": dict.fromkeys(
                    [
                        *(f"Time {index}" for index in range(1, len(columns) + 1)),
                        *(f"node {index}" for index in range(2, len(columns) + 1)),
                    ],
                    True,
                ),
                "indexByName": {name: index for index, name in enumerate(("node", *values))},
                "renameByName": values,
            },
        ),
    ]


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="node-memory",
        title="Node Memory",
        tags=["cluster", "memory", "nodes", "kubernetes"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-1h"),
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read Node Memory",
                grid_pos=dashboard.GridPos(h=5, w=24),
                options=text.Options(
                    content="The overview compares physical node capacity and Linux `MemAvailable` with running Pod container working sets and declared memory requests. `MemAvailable` is the kernel estimate of RAM available for new workloads; it is not calculated as capacity minus a Grafana working-set total.\n\nThe per-node accounting bars add to node capacity: selected largest Pods, **Other Pods** (all remaining running Pods, including namespaces outside the namespace filter), **Host / unattributed** (node used RAM minus all running Pod working sets), and **Available to Linux** (`MemAvailable`). The host remainder is signed and can be negative when cAdvisor and node memory accounting differ. It can include host processes, runtime memory, and collection/accounting gaps; it is not a pure OS bucket. Pod sizes come from kubelet working-set metrics, deduplicated by node, namespace, Pod, and container.\n\nHistory selects the six largest namespace-filtered Pods by their maximum observed working set across the selected time range (one-minute subquery samples), then holds that membership constant across the graph. Host cache, buffers, slab, and kernel charts are separate overlapping views of node memory and must not be added to the Pod breakdown. The Pod table reports configured container limits only where a current limit series exists; its missing-limit count can mean either no configured limit or a missing kube-state-metrics sample."
                ),
            ),
            dashboard.RowPanel(title="Current cluster snapshot", grid_pos=dashboard.GridPos(h=1, w=24, y=5), id_val=2),
            dashboard.Panel(
                type_val="table",
                id_val=3,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        ("A", _NODE_TOTAL),
                        ("B", _NODE_AVAILABLE),
                        ("C", f"({_TOTAL_POD_WORKING_SET}) and on (node) ({_NODE_TOTAL})"),
                        ("D", f"({_RUNNING_MEMORY_REQUESTS}) and on (node) ({_NODE_TOTAL})"),
                        ("E", _MEMORY_PRESSURE),
                        ("F", _NODE_USED),
                        ("G", _HOST_REMAINDER),
                    )
                ],
                title="Node memory overview",
                description="One row per node with current physical capacity, MemAvailable, running Pod working sets, declared running-Pod memory requests, MemoryPressure, node used RAM, and the signed host/unattributed remainder. Nodes without current node-exporter memory samples are not synthesized as zero.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=6),
                transformations=_node_table_transforms(
                    ("Capacity", "Available", "Pod working sets", "Requests", "Pressure", "Used RAM", "Host / other")
                ),
                options=table.Options(sort_by=[common.TableSortByFieldState(display_name="Used RAM", desc=True)]),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes", decimals=1),
                    overrides=[field_override("Pressure", {"unit": "short", "decimals": 0})],
                ),
            ),
            dashboard.RowPanel(
                title="Current per-node accounting", grid_pos=dashboard.GridPos(h=1, w=24, y=16), id_val=4
            ),
            dashboard.Panel(
                type_val="barchart",
                id_val=5,
                targets=[
                    mimir_table_query(
                        f'(label_join(({_TOP_POD_WORKING_SETS}), "part", "/", "namespace", "pod") or label_replace(({_OTHER_PODS}), "part", "Other Pods", "node", ".*") or label_replace(({_HOST_REMAINDER}), "part", "Host / unattributed", "node", ".*") or label_replace(({_NODE_AVAILABLE}), "part", "Available to Linux (MemAvailable)", "node", ".*")) and on (node) ({_NODE_TOTAL})'
                    ).build()
                ],
                title="Memory accounting: ${node:text}",
                description="One stacked horizontal bar per repeated node. Selected Pods are the six largest over the dashboard time range; Other Pods includes all remaining running Pods across every namespace. Host/unattributed is signed node-used RAM minus all Pod working sets and is not clamped. Available is Linux node_memory_MemAvailable_bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=17),
                repeat="node",
                max_per_row=2,
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="groupingToMatrix",
                        options={
                            "columnField": "part",
                            "rowField": "node",
                            "valueField": "Value",
                            "emptyValue": "null",
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {},
                            "indexByName": {"node\\part": 0},
                            "renameByName": {"node\\part": "Node"},
                        },
                    ),
                ],
                interval="1m",
                options=barchart.Options(
                    x_field="Node",
                    orientation=common.VizOrientation.HORIZONTAL,
                    stacking=common.StackingMode.NORMAL,
                    show_value=common.VisibilityMode.NEVER,
                    bar_width=0.95,
                    legend=common.VizLegendOptions(show_legend=True),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes",
                        decimals=1,
                        min_val=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=6,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        (
                            "A",
                            f'(label_join({_SELECTED_POD_WORKING_SETS}, "pod_key", "/", "node", "namespace", "pod")) and on (node) ({_NODE_TOTAL})',
                        ),
                        (
                            "B",
                            f'(label_join(sum by (node, namespace, pod) (max by (node, namespace, pod, container) (kube_pod_container_resource_requests{{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"}} and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1))), "pod_key", "/", "node", "namespace", "pod")) and on (node) ({_NODE_TOTAL})',
                        ),
                        (
                            "C",
                            f'(label_join(sum by (node, namespace, pod) (max by (node, namespace, pod, container) (kube_pod_container_resource_limits{{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"}} and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1)) and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1)), "pod_key", "/", "node", "namespace", "pod")) and on (node) ({_NODE_TOTAL})',
                        ),
                        (
                            "D",
                            f'(label_join(count by (node, namespace, pod) ((max by (node, namespace, pod, container) (container_memory_working_set_bytes{{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"}} and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1))) unless on (node, namespace, pod, container) (max by (node, namespace, pod, container) (kube_pod_container_resource_limits{{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"}} and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1)) and on (namespace, pod) (kube_pod_status_phase{{phase="Running"}} == 1))), "pod_key", "/", "node", "namespace", "pod")) and on (node) ({_NODE_TOTAL})',
                        ),
                        (
                            "E",
                            f'label_join(100 * ({_SELECTED_POD_WORKING_SETS}) / on (node) group_left() ({_NODE_TOTAL}), "pod_key", "/", "node", "namespace", "pod")',
                        ),
                    )
                ],
                title="Running Pod memory details",
                description="One row per running Pod, scoped by node and namespace selectors. Working set, declared requests, and configured limits are sums across containers. A blank configured-limit cell means no current aggregate limit series; the adjacent count identifies containers without a current limit series, which may mean either an unset limit or a missing kube-state-metrics sample. Node RAM percentage uses physical node capacity. The outer join keeps Pods with missing requests or limits.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=12, w=24, y=25),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="joinByField", options={"byField": "pod_key", "mode": "outerTabular"}
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": dict.fromkeys(
                                (
                                    *("pod_key",),
                                    *(f"Time {index}" for index in range(1, 6)),
                                    *(f"node {index}" for index in range(2, 6)),
                                    *(f"namespace {index}" for index in range(2, 6)),
                                    *(f"pod {index}" for index in range(2, 6)),
                                ),
                                True,
                            ),
                            "indexByName": {
                                name: index
                                for index, name in enumerate(
                                    (
                                        "node 1",
                                        "namespace 1",
                                        "pod 1",
                                        "Value #A",
                                        "Value #B",
                                        "Value #C",
                                        "Value #D",
                                        "Value #E",
                                    )
                                )
                            },
                            "renameByName": {
                                "Value #A": "Pod working set",
                                "Value #B": "Request",
                                "Value #C": "Configured limit",
                                "Value #D": "Containers without limit",
                                "Value #E": "Node RAM %",
                                "node 1": "node",
                                "namespace 1": "namespace",
                                "pod 1": "pod",
                            },
                        },
                    ),
                ],
                options=table.Options(
                    sort_by=[common.TableSortByFieldState(display_name="Pod working set", desc=True)]
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes", decimals=1),
                    overrides=[
                        field_override("Node RAM %", {"unit": "percent", "decimals": 1}),
                        field_override("Containers without limit", {"unit": "short", "decimals": 0}),
                    ],
                ),
            ),
            dashboard.RowPanel(title="Memory history", grid_pos=dashboard.GridPos(h=1, w=24, y=38), id_val=7),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query(expr, legend=legend, ref_id=chr(ord("A") + index)).build()
                    for index, (expr, legend) in enumerate(
                        (
                            (f"({_TOP_POD_WORKING_SETS}) and on (node) ({_NODE_TOTAL})", "{{namespace}}/{{pod}}"),
                            (f"({_OTHER_PODS}) and on (node) ({_NODE_TOTAL})", "Other Pods"),
                            (f"({_HOST_REMAINDER}) and on (node) ({_NODE_TOTAL})", "Host / unattributed"),
                            (_NODE_TOTAL, "Node capacity"),
                        )
                    )
                ],
                title="Pod + host/unattributed working sets: ${node:text}",
                description="Stacked Pod working sets and signed host/unattributed remainder, with physical node capacity as a non-stacked reference. Largest Pods are selected by maximum one-minute samples over the selected dashboard range and the same membership is held at every graph step. Other Pods includes all running Pod working sets outside the selected named series.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, y=39),
                repeat="node",
                max_per_row=2,
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE,
                        show_legend=True,
                        sort_by="Max",
                        sort_desc=True,
                        calcs=["lastNotNull", "max"],
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes",
                        min_val=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC),
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=1,
                            fill_opacity=25,
                            stacking=common.StackingConfig(mode=common.StackingMode.NORMAL, group="A"),
                        ),
                    ),
                    overrides=[
                        field_override(
                            "Node capacity",
                            {
                                "custom.stacking": {"mode": "none", "group": "B"},
                                "custom.fillOpacity": 0,
                                "custom.lineWidth": 2,
                                "color": {"mode": "fixed", "fixedColor": "gray"},
                            },
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[mimir_query(_NODE_AVAILABLE, legend="{{node}}").build()],
                title="Linux available RAM history",
                description="node_memory_MemAvailable_bytes, the kernel estimate of memory available to new workloads. This is not calculated as node capacity minus a Grafana container working-set total.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=49),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes", min_val=0, custom=common.GraphFieldConfig(fill_opacity=10)
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[mimir_query(_MEMORY_PRESSURE, legend="{{node}}").build()],
                title="MemoryPressure state (0 = false, 1 = true)",
                description="Kubernetes node MemoryPressure condition for nodes with current node-exporter memory samples. The false series is represented as zero only when kube-state-metrics reports the false condition; absent node or condition samples are not synthesized.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=7, w=24, y=57),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        decimals=0,
                        min_val=0,
                        max_val=1,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_interpolation=common.LineInterpolation.STEP_AFTER,
                            line_width=2,
                            fill_opacity=10,
                        ),
                    )
                ),
            ),
            dashboard.RowPanel(
                title="Host memory details · overlapping views", grid_pos=dashboard.GridPos(h=1, w=24, y=64), id_val=11
            ),
            dashboard.Panel(
                type_val="table",
                id_val=12,
                targets=[
                    mimir_table_query(_node_memory("Cached")).build(),
                    mimir_table_query(_node_memory("Buffers"), ref_id="B").build(),
                ],
                title="Host cache and buffers · overlaps Pod accounting",
                description="Current node-exporter cache and buffers gauges. These host views can overlap cAdvisor working sets and are not separate buckets to add to the Pod accounting bars.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=65),
                transformations=_node_table_transforms(("Cached", "Buffers")),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes", decimals=1)),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=13,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        ("A", _node_memory("Slab")),
                        ("B", _node_memory("SReclaimable")),
                        ("C", _node_memory("SUnreclaim")),
                        ("D", _node_memory("KernelStack")),
                        ("E", _node_memory("PageTables")),
                        ("F", _node_memory("VmallocUsed")),
                        ("G", _node_memory("Percpu")),
                    )
                ],
                title="Host slab and kernel memory · overlaps Pod accounting",
                description="Current slab, reclaimable and unreclaimable slab, kernel stack, page tables, vmalloc, and per-CPU gauges from node-exporter. Slab contains its reclaimable and unreclaimable components; all these host metrics overlap the broader Pod/host memory accounting and must not be added together as disjoint buckets.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=76),
                transformations=_node_table_transforms(
                    (
                        "Slab",
                        "Reclaimable slab",
                        "Unreclaimable slab",
                        "Kernel stack",
                        "Page tables",
                        "Vmalloc used",
                        "Per-CPU",
                    )
                ),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes", decimals=1)),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="node",
                    label="Node",
                    query=_NODE_VARIABLE_QUERY,
                    datasource=mimir(),
                    current=dashboard.VariableOption(selected=True, text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    regex='/node="([^"]+)"/',
                    definition=_NODE_VARIABLE_QUERY,
                ),
                dashboard.VariableModel(
                    name="namespace",
                    label="Namespace focus",
                    query=_NAMESPACE_VARIABLE_QUERY,
                    datasource=mimir(),
                    current=dashboard.VariableOption(selected=True, text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    regex='/namespace="([^"]+)"/',
                    definition=_NAMESPACE_VARIABLE_QUERY,
                ),
            ]
        ),
    )
