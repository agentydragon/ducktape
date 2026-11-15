"""Storage workload and collector details dashboard."""

from grafana_foundation_sdk.models import common, dashboard, prometheus, table, text

from cluster.cdk8s.monitoring.dashboards.foundation import mimir, mimir_query, series_options


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="storage-service-details",
        title="Storage workload and collector details",
        tags=["cluster", "storage", "workload", "collector"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        timepicker=dashboard.TimePickerConfig(),
        week_start="",
        refresh="5m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read this dashboard",
                grid_pos=dashboard.GridPos(h=6, w=24),
                options=text.Options(
                    content="These are workload and collector measurements, not a third layer to add to host filesystem or Piraeus pool totals. CNPG database size is not total PGDATA filesystem use. SeaweedFS logical bucket bytes omit replicas; physical bucket and collection values overlap. ClickHouse MergeTree bytes are per-pod collector values.\n\nMachine and Storage tier filter node-owned panels. SeaweedFS S3 bucket panels are cluster-wide and are not filtered by node ownership.\n\nThe Storage tier selector filters by node labels; Machine filters node-owned panels. This is node-level filtering. If a node later has both SSD and HDD, per-device media labels will be needed to separate those disks."
                ),
            ),
            dashboard.RowPanel(title="CNPG database detail", grid_pos=dashboard.GridPos(h=1, w=24, y=6), id_val=24),
            dashboard.Panel(
                type_val="table",
                id_val=6,
                targets=[
                    mimir_query(
                        'sort_desc(max by (node, namespace, job, datname) (cnpg_pg_database_size_bytes{datname!~"template0|template1"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{node=~"$node"})))',
                        legend="{{node}} {{namespace}} {{job}} {{datname}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="CNPG database size by machine now",
                description="CNPG exporter database-size values from pods on the selected machine. This database metric is not total PGDATA filesystem use; replicas report their own copies and must not be added across machines as unique data.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=7),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=7,
                targets=[
                    mimir_query(
                        'topk(10, max by (node, namespace, job, datname) (cnpg_pg_database_size_bytes{datname!~"template0|template1"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{node=~"$node"})))',
                        legend="{{node}} {{namespace}} {{job}} {{datname}}",
                    ).build()
                ],
                title="Largest CNPG databases by machine over time",
                description="CNPG exporter database-size values from pods on the selected machine. This database metric is not total PGDATA filesystem use; replicas report their own copies and must not be added across machines as unique data.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=7),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.RowPanel(
                title="SeaweedFS volume-server detail", grid_pos=dashboard.GridPos(h=1, w=24, y=16), id_val=40
            ),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    mimir_query(
                        'sort_desc(max by (node, pod, name, type) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type=~"all|avail|used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"})))',
                        legend="{{node}} {{pod}} {{name}} {{type}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="SeaweedFS volume-server disks by machine now",
                description="Volume-server all/available/used resource gauges from pods on the selected machine. These per-server values are collector detail and are not additive to the filesystem or Piraeus pool totals.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=17),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query(
                        'max by (node, pod, name) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type="used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"}))',
                        legend="{{node}} {{pod}} {{name}}",
                    ).build()
                ],
                title="SeaweedFS volume-server disk use by machine over time",
                description="Volume-server reported used bytes per selected storage node and server disk. This is a per-collector view; do not add it to the independent pool and filesystem accounting layers.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=17),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=14,
                targets=[
                    mimir_query(
                        'sort_desc(sum by (node, collection) (max by (node, collection, pod) (SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="normal",collection!=""} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"}))))',
                        legend="{{node}} {{collection}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="SeaweedFS collection bytes by machine now",
                description="Collection bytes reported by volume-server pods on the selected machine. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=26),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    mimir_query(
                        'topk(10, sum by (node, collection) (max by (node, collection, pod) (SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="normal",collection!=""} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"}))))',
                        legend="{{node}} {{collection}}",
                    ).build()
                ],
                title="Largest SeaweedFS collections by machine over time",
                description="Collection bytes reported by volume-server pods on the selected machine over time. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.RowPanel(
                title="SeaweedFS S3 bucket detail · cluster-wide",
                grid_pos=dashboard.GridPos(h=1, w=24, y=35),
                id_val=41,
            ),
            dashboard.Panel(
                type_val="table",
                id_val=10,
                targets=[
                    mimir_query(
                        'sort_desc(max by (bucket) (SeaweedFS_s3_bucket_size_bytes{namespace="seaweedfs",bucket!=""}))',
                        legend="{{bucket}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="SeaweedFS S3 logical bucket size now (cluster-wide)",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=36),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[
                    mimir_query(
                        'topk(10, max by (bucket) (SeaweedFS_s3_bucket_size_bytes{namespace="seaweedfs",bucket!=""}))',
                        legend="{{bucket}}",
                    ).build()
                ],
                title="Largest SeaweedFS S3 logical buckets over time (cluster-wide)",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; logical bytes omit replicas.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=36),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=12,
                targets=[
                    mimir_query(
                        'sort_desc(max by (bucket) (SeaweedFS_s3_bucket_physical_size_bytes{namespace="seaweedfs",bucket!=""}))',
                        legend="{{bucket}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="SeaweedFS S3 physical bucket size now (cluster-wide)",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=45),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[
                    mimir_query(
                        'topk(10, max by (bucket) (SeaweedFS_s3_bucket_physical_size_bytes{namespace="seaweedfs",bucket!=""}))',
                        legend="{{bucket}}",
                    ).build()
                ],
                title="Largest SeaweedFS S3 physical buckets over time (cluster-wide)",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; these overlap with collection-level replica bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=45),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.RowPanel(
                title="ClickHouse MergeTree detail", grid_pos=dashboard.GridPos(h=1, w=24, y=54), id_val=42
            ),
            dashboard.Panel(
                type_val="table",
                id_val=16,
                targets=[
                    mimir_query(
                        'sort_desc((max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTables{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"})) + max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTablesSystem{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"}))))',
                        legend="{{node}} {{pod}}",
                    )
                    .format(prometheus.PromQueryFormat.TABLE)
                    .instant()
                    .build()
                ],
                title="ClickHouse MergeTree bytes by machine now",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=55),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={"excludeByName": {"Time": True}, "indexByName": {}, "renameByName": {}},
                    )
                ],
                options=table.Options(footer=common.TableFooterOptions()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=17,
                targets=[
                    mimir_query(
                        '(max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTables{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"})) + max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTablesSystem{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"})))',
                        legend="{{node}} {{pod}}",
                    ).build()
                ],
                title="ClickHouse MergeTree bytes by machine over time",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=55),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="storage_tier",
                    label="Storage tier",
                    query="SSD : ssd,HDD : hdd,Both : ssd|hdd",
                    current=dashboard.VariableOption(selected=True, text="Both", value="ssd|hdd"),
                    options=[
                        dashboard.VariableOption(selected=False, text="SSD", value="ssd"),
                        dashboard.VariableOption(selected=False, text="HDD", value="hdd"),
                        dashboard.VariableOption(selected=True, text="Both", value="ssd|hdd"),
                    ],
                ),
                dashboard.VariableModel(
                    name="node",
                    label="Machine",
                    query='label_values(kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"}, node)',
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition='label_values(kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"}, node)',
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
