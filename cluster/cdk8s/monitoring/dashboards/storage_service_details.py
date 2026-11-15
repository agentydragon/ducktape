"""Storage workload and collector details dashboard."""

from grafana_foundation_sdk.models import common, dashboard, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    hide_time,
    mimir,
    mimir_query,
    mimir_table_query,
    series_options,
    storage_tier_variable,
)

_CNPG_BYTES = 'max by (node, namespace, job, datname) (cnpg_pg_database_size_bytes{datname!~"template0|template1"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{node=~"$node"}))'

_COLLECTION_BYTES = 'sum by (node, collection) (max by (node, collection, pod) (SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="normal",collection!=""} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"})))'

_LOGICAL_BUCKET_BYTES = 'max by (bucket) (SeaweedFS_s3_bucket_size_bytes{namespace="seaweedfs",bucket!=""})'

_PHYSICAL_BUCKET_BYTES = 'max by (bucket) (SeaweedFS_s3_bucket_physical_size_bytes{namespace="seaweedfs",bucket!=""})'

_CLICKHOUSE_BYTES = '(max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTables{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"})) + max by (node, namespace, pod) (ClickHouseAsyncMetrics_TotalBytesOfMergeTreeTablesSystem{namespace="clickhouse"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="clickhouse",node=~"$node"})))'


def _bytes_panel(
    id: int,
    title: str,
    expr: str,
    *,
    legend: str,
    description: str,
    position: dashboard.GridPos,
    table_view: bool = False,
) -> dashboard.Panel:
    query = mimir_table_query(expr, legend=legend) if table_view else mimir_query(expr, legend=legend)
    return dashboard.Panel(
        type_val="table" if table_view else "timeseries",
        id_val=id,
        title=title,
        description=description,
        datasource=mimir(),
        grid_pos=position,
        targets=[query.build()],
        transformations=[hide_time()] if table_view else None,
        options=None if table_view else series_options(sort=common.SortOrder.DESCENDING),
        field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
    )


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="storage-service-details",
        title="Storage workload and collector details",
        tags=["cluster", "storage", "workload", "collector"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
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
            _bytes_panel(
                6,
                "CNPG database size by machine now",
                f"sort_desc({_CNPG_BYTES})",
                legend="{{node}} {{namespace}} {{job}} {{datname}}",
                description="CNPG exporter database-size values from pods on the selected machine. This database metric is not total PGDATA filesystem use; replicas report their own copies and must not be added across machines as unique data.",
                position=dashboard.GridPos(y=7),
                table_view=True,
            ),
            _bytes_panel(
                7,
                "Largest CNPG databases by machine over time",
                f"topk(10, {_CNPG_BYTES})",
                legend="{{node}} {{namespace}} {{job}} {{datname}}",
                description="CNPG exporter database-size values from pods on the selected machine. This database metric is not total PGDATA filesystem use; replicas report their own copies and must not be added across machines as unique data.",
                position=dashboard.GridPos(x=12, y=7),
            ),
            dashboard.RowPanel(
                title="SeaweedFS volume-server detail", grid_pos=dashboard.GridPos(h=1, w=24, y=16), id_val=40
            ),
            _bytes_panel(
                8,
                "SeaweedFS volume-server disks by machine now",
                'sort_desc(max by (node, pod, name, type) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type=~"all|avail|used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"})))',
                legend="{{node}} {{pod}} {{name}} {{type}}",
                description="Volume-server all/available/used resource gauges from pods on the selected machine. These per-server values are collector detail and are not additive to the filesystem or Piraeus pool totals.",
                position=dashboard.GridPos(y=17),
                table_view=True,
            ),
            _bytes_panel(
                9,
                "SeaweedFS volume-server disk use by machine over time",
                'max by (node, pod, name) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type="used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"}))',
                legend="{{node}} {{pod}} {{name}}",
                description="Volume-server reported used bytes per selected storage node and server disk. This is a per-collector view; do not add it to the independent pool and filesystem accounting layers.",
                position=dashboard.GridPos(x=12, y=17),
            ),
            _bytes_panel(
                14,
                "SeaweedFS collection bytes by machine now",
                f"sort_desc({_COLLECTION_BYTES})",
                legend="{{node}} {{collection}}",
                description="Collection bytes reported by volume-server pods on the selected machine. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                position=dashboard.GridPos(y=26),
                table_view=True,
            ),
            _bytes_panel(
                15,
                "Largest SeaweedFS collections by machine over time",
                f"topk(10, {_COLLECTION_BYTES})",
                legend="{{node}} {{collection}}",
                description="Collection bytes reported by volume-server pods on the selected machine over time. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                position=dashboard.GridPos(x=12, y=26),
            ),
            dashboard.RowPanel(
                title="SeaweedFS S3 bucket detail · cluster-wide",
                grid_pos=dashboard.GridPos(h=1, w=24, y=35),
                id_val=41,
            ),
            _bytes_panel(
                10,
                "SeaweedFS S3 logical bucket size now (cluster-wide)",
                f"sort_desc({_LOGICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                position=dashboard.GridPos(y=36),
                table_view=True,
            ),
            _bytes_panel(
                11,
                "Largest SeaweedFS S3 logical buckets over time (cluster-wide)",
                f"topk(10, {_LOGICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; logical bytes omit replicas.",
                position=dashboard.GridPos(x=12, y=36),
            ),
            _bytes_panel(
                12,
                "SeaweedFS S3 physical bucket size now (cluster-wide)",
                f"sort_desc({_PHYSICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                position=dashboard.GridPos(y=45),
                table_view=True,
            ),
            _bytes_panel(
                13,
                "Largest SeaweedFS S3 physical buckets over time (cluster-wide)",
                f"topk(10, {_PHYSICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; these overlap with collection-level replica bytes.",
                position=dashboard.GridPos(x=12, y=45),
            ),
            dashboard.RowPanel(
                title="ClickHouse MergeTree detail", grid_pos=dashboard.GridPos(h=1, w=24, y=54), id_val=42
            ),
            _bytes_panel(
                16,
                "ClickHouse MergeTree bytes by machine now",
                f"sort_desc({_CLICKHOUSE_BYTES})",
                legend="{{node}} {{pod}}",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                position=dashboard.GridPos(y=55),
                table_view=True,
            ),
            _bytes_panel(
                17,
                "ClickHouse MergeTree bytes by machine over time",
                _CLICKHOUSE_BYTES,
                legend="{{node}} {{pod}}",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                position=dashboard.GridPos(x=12, y=55),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                storage_tier_variable(),
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
    )
