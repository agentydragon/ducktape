"""Node Memory dashboard."""

from grafana_foundation_sdk.models import barchart, common, dashboard, prometheus, table, text, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import mimir, mimir_query, series_options


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="node-memory",
        title="Node Memory",
        tags=["cluster", "memory", "nodes", "kubernetes"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-1h"),
        timepicker=dashboard.TimePickerConfig(),
        week_start="",
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
                    mimir_query(
                        'max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))'
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="B",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="C",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(sum by (node) (max by (node, namespace, pod, container) (kube_pod_container_resource_requests{resource="memory",unit="byte",node=~"$node"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="D",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (kube_node_status_condition{condition="MemoryPressure",status="true",node=~"$node"} and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))) or on (node) (0 * max by (node) (kube_node_status_condition{condition="MemoryPressure",status="false",node=~"$node"} and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))))',
                        ref_id="E",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))) - (max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="F",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '((max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))) - (max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))) - (sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))))',
                        ref_id="G",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                ],
                title="Node memory overview",
                description="One row per node with current physical capacity, MemAvailable, running Pod working sets, declared running-Pod memory requests, MemoryPressure, node used RAM, and the signed host/unattributed remainder. Nodes without current node-exporter memory samples are not synthesized as zero.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=6),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="joinByField", options={"byField": "node", "mode": "outerTabular"}
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {
                                "Time 1": True,
                                "Time 2": True,
                                "Time 3": True,
                                "Time 4": True,
                                "Time 5": True,
                                "Time 6": True,
                                "Time 7": True,
                                "node 2": True,
                                "node 3": True,
                                "node 4": True,
                                "node 5": True,
                                "node 6": True,
                                "node 7": True,
                            },
                            "indexByName": {
                                "node": 0,
                                "Value #A": 1,
                                "Value #B": 2,
                                "Value #C": 3,
                                "Value #D": 4,
                                "Value #E": 5,
                                "Value #F": 6,
                                "Value #G": 7,
                            },
                            "renameByName": {
                                "Value #A": "Capacity",
                                "Value #B": "Available",
                                "Value #C": "Pod working sets",
                                "Value #D": "Requests",
                                "Value #E": "Pressure",
                                "Value #F": "Used RAM",
                                "Value #G": "Host / other",
                            },
                        },
                    ),
                ],
                options=table.Options(
                    sort_by=[common.TableSortByFieldState(display_name="Used RAM", desc=True)],
                    footer=common.TableFooterOptions(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes",
                        decimals=1,
                        custom=common.TableFieldOptions(cell_options=common.TableAutoCellOptions()),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Pressure"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                            ],
                        )
                    ],
                ),
            ),
            dashboard.RowPanel(
                title="Current per-node accounting", grid_pos=dashboard.GridPos(h=1, w=24, y=16), id_val=4
            ),
            dashboard.Panel(
                type_val="barchart",
                id_val=5,
                targets=[
                    mimir_query(
                        '(label_join(((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) and on (node, namespace, pod) topk by (node) (6, max_over_time((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))[${__range_s}s:1m] @ end()))), "part", "/", "namespace", "pod") or label_replace(((sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))) - sum by (node) ((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) and on (node, namespace, pod) topk by (node) (6, max_over_time((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))[${__range_s}s:1m] @ end())))), "part", "Other Pods", "node", ".*") or label_replace((((max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))) - (max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))) - (sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))))), "part", "Host / unattributed", "node", ".*") or label_replace((max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))), "part", "Available to Linux (MemAvailable)", "node", ".*")) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))'
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
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
                    mimir_query(
                        '(label_join(sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))), "pod_key", "/", "node", "namespace", "pod")) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))'
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(label_join(sum by (node, namespace, pod) (max by (node, namespace, pod, container) (kube_pod_container_resource_requests{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))), "pod_key", "/", "node", "namespace", "pod")) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="B",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(label_join(sum by (node, namespace, pod) (max by (node, namespace, pod, container) (kube_pod_container_resource_limits{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)) and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)), "pod_key", "/", "node", "namespace", "pod")) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="C",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        '(label_join(count by (node, namespace, pod) ((max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))) unless on (node, namespace, pod, container) (max by (node, namespace, pod, container) (kube_pod_container_resource_limits{resource="memory",unit="byte",node=~"$node",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)) and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))), "pod_key", "/", "node", "namespace", "pod")) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        ref_id="D",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'label_join(100 * (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) / on (node) group_left() (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))), "pod_key", "/", "node", "namespace", "pod")',
                        ref_id="E",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
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
                            "excludeByName": {
                                "pod_key": True,
                                "Time 1": True,
                                "Time 2": True,
                                "Time 3": True,
                                "Time 4": True,
                                "Time 5": True,
                                "node 2": True,
                                "node 3": True,
                                "node 4": True,
                                "node 5": True,
                                "namespace 2": True,
                                "namespace 3": True,
                                "namespace 4": True,
                                "namespace 5": True,
                                "pod 2": True,
                                "pod 3": True,
                                "pod 4": True,
                                "pod 5": True,
                            },
                            "indexByName": {
                                "node 1": 0,
                                "namespace 1": 1,
                                "pod 1": 2,
                                "Value #A": 3,
                                "Value #B": 4,
                                "Value #C": 5,
                                "Value #D": 6,
                                "Value #E": 7,
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
                    sort_by=[common.TableSortByFieldState(display_name="Pod working set", desc=True)],
                    footer=common.TableFooterOptions(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes", decimals=1),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Node RAM %"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="percent"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Containers without limit"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.RowPanel(title="Memory history", grid_pos=dashboard.GridPos(h=1, w=24, y=38), id_val=7),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query(
                        '((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) and on (node, namespace, pod) topk by (node) (6, max_over_time((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))[${__range_s}s:1m] @ end()))) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        legend="{{namespace}}/{{pod}}",
                    ).build(),
                    mimir_query(
                        '((sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))) - sum by (node) ((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))) and on (node, namespace, pod) topk by (node) (6, max_over_time((sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD",namespace=~"$namespace"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))))[${__range_s}s:1m] @ end())))) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        legend="Other Pods",
                        ref_id="B",
                    ).build(),
                    mimir_query(
                        '(((max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))) - (max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))) - (sum by (node) (sum by (node, namespace, pod) (max by (node, namespace, pod, container) (container_memory_working_set_bytes{node=~"$node",pod!="",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))))) and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))',
                        legend="Host / unattributed",
                        ref_id="C",
                    ).build(),
                    mimir_query(
                        'max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        legend="Node capacity",
                        ref_id="D",
                    ).build(),
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
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Node capacity"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.stacking", value={"mode": "none", "group": "B"}
                                ),
                                dashboard.DynamicConfigValue(id_val="custom.fillOpacity", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.lineWidth", value=2),
                                dashboard.DynamicConfigValue(
                                    id_val="color", value={"mode": "fixed", "fixedColor": "gray"}
                                ),
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query(
                        'max by (node) (node_memory_MemAvailable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        legend="{{node}}",
                    ).build()
                ],
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
                targets=[
                    mimir_query(
                        'max by (node) (kube_node_status_condition{condition="MemoryPressure",status="true",node=~"$node"} and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))) or on (node) (0 * max by (node) (kube_node_status_condition{condition="MemoryPressure",status="false",node=~"$node"} and on (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"})))))',
                        legend="{{node}}",
                    ).build()
                ],
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
                        custom={"drawStyle": "stepAfter", "lineWidth": 2, "fillOpacity": 10},
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
                    mimir_query(
                        'max by (node) (node_memory_Cached_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))'
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_Buffers_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="B",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                ],
                title="Host cache and buffers · overlaps Pod accounting",
                description="Current node-exporter cache and buffers gauges. These host views can overlap cAdvisor working sets and are not separate buckets to add to the Pod accounting bars.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=65),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="joinByField", options={"byField": "node", "mode": "outerTabular"}
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time 1": True, "Time 2": True, "node 2": True},
                            "indexByName": {"node": 0, "Value #A": 1, "Value #B": 2},
                            "renameByName": {"Value #A": "Cached", "Value #B": "Buffers"},
                        },
                    ),
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes", decimals=1)),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=13,
                targets=[
                    mimir_query(
                        'max by (node) (node_memory_Slab_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))'
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_SReclaimable_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="B",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_SUnreclaim_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="C",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_KernelStack_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="D",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_PageTables_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="E",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_VmallocUsed_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="F",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                    mimir_query(
                        'max by (node) (node_memory_Percpu_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+",node=~"$node"}))',
                        ref_id="G",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build(),
                ],
                title="Host slab and kernel memory · overlaps Pod accounting",
                description="Current slab, reclaimable and unreclaimable slab, kernel stack, page tables, vmalloc, and per-CPU gauges from node-exporter. Slab contains its reclaimable and unreclaimable components; all these host metrics overlap the broader Pod/host memory accounting and must not be added together as disjoint buckets.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=76),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="joinByField", options={"byField": "node", "mode": "outerTabular"}
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {
                                "Time 1": True,
                                "Time 2": True,
                                "Time 3": True,
                                "Time 4": True,
                                "Time 5": True,
                                "Time 6": True,
                                "Time 7": True,
                                "node 2": True,
                                "node 3": True,
                                "node 4": True,
                                "node 5": True,
                                "node 6": True,
                                "node 7": True,
                            },
                            "indexByName": {
                                "node": 0,
                                "Value #A": 1,
                                "Value #B": 2,
                                "Value #C": 3,
                                "Value #D": 4,
                                "Value #E": 5,
                                "Value #F": 6,
                                "Value #G": 7,
                            },
                            "renameByName": {
                                "Value #A": "Slab",
                                "Value #B": "Reclaimable slab",
                                "Value #C": "Unreclaimable slab",
                                "Value #D": "Kernel stack",
                                "Value #E": "Page tables",
                                "Value #F": "Vmalloc used",
                                "Value #G": "Per-CPU",
                            },
                        },
                    ),
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes", decimals=1)),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="node",
                    label="Node",
                    query='query_result(max by (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+"}))))',
                    datasource=mimir(),
                    current=dashboard.VariableOption(selected=True, text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    regex='/node="([^"]+)"/',
                    definition='query_result(max by (node) (max by (node) (node_memory_MemTotal_bytes{namespace="monitoring",pod=~"prometheus-node-exporter-.+"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="monitoring",pod=~"prometheus-node-exporter-.+"}))))',
                ),
                dashboard.VariableModel(
                    name="namespace",
                    label="Namespace focus",
                    query='query_result(max by (namespace) (container_memory_working_set_bytes{node=~"$node",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))',
                    datasource=mimir(),
                    current=dashboard.VariableOption(selected=True, text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    regex='/namespace="([^"]+)"/',
                    definition='query_result(max by (namespace) (container_memory_working_set_bytes{node=~"$node",container!="",container!="POD"} and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)))',
                ),
            ]
        ),
        annotations=dashboard.AnnotationContainer(
            list_val=[
                dashboard.AnnotationQuery(
                    name="Annotations & Alerts",
                    datasource=common.DataSourceRef(type_val="grafana", uid="-- Grafana --"),
                    hide=True,
                    icon_color="rgba(0, 211, 255, 1)",
                    type_val="dashboard",
                    built_in=1,
                )
            ]
        ),
    )
