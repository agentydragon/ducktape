"""Storage workload and collector details dashboard."""

from grafana_foundation_sdk.models import common, dashboard, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    hide_time,
    mimir,
    mimir_multi_variable,
    mimir_query,
    mimir_table_query,
    series_options,
    storage_tier_variable,
)

_CNPG_BYTES = 'max by (node, namespace, job, datname) (cnpg_pg_database_size_bytes{datname!~"template0|template1"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{node=~"$node"}))'

_CNPG_TABLE_LABELS = "namespace, job, datname, schemaname, relname"
_CNPG_TABLE_SELECTOR = 'cnpg_pg_table_total_bytes{namespace=~"$pg_namespace", job=~"$pg_job", datname=~"$pg_database", schemaname=~"$pg_schema"}'
# This is a primary-only gauge. Collapsing scrape labels makes the logical table key
# stable and prevents a primary transition from temporarily counting two pods.
_CNPG_TABLE_BYTES = f"max by ({_CNPG_TABLE_LABELS}) ({_CNPG_TABLE_SELECTOR})"
_CNPG_TABLE_BYTES_AT_END = f"max by ({_CNPG_TABLE_LABELS}) ({_CNPG_TABLE_SELECTOR} @ end())"
_CNPG_TABLE_BYTES_TOP_TEN = f"topk(10, {_CNPG_TABLE_BYTES_AT_END})"
_CNPG_TABLE_BYTES_OVER_TIME = f"({_CNPG_TABLE_BYTES} and on ({_CNPG_TABLE_LABELS}) {_CNPG_TABLE_BYTES_TOP_TEN})"
_CNPG_TABLE_GROWTH_1H = f"delta(({_CNPG_TABLE_BYTES})[1h:])"
_CNPG_TABLE_GROWTH_1H_AT_END = f"delta(({_CNPG_TABLE_BYTES})[1h:] @ end())"
_CNPG_TABLE_GROWTH_TOP_TEN = f"topk(10, {_CNPG_TABLE_GROWTH_1H_AT_END} > 0)"
_CNPG_TABLE_GROWTH_OVER_TIME = f"({_CNPG_TABLE_GROWTH_1H} and on ({_CNPG_TABLE_LABELS}) {_CNPG_TABLE_GROWTH_TOP_TEN})"

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
                    content="These are workload and collector measurements, not a third layer to add to host filesystem or Piraeus pool totals. CNPG database size is not total PGDATA filesystem use. SeaweedFS logical bucket bytes omit replicas; physical bucket and collection values overlap. ClickHouse MergeTree bytes are per-pod collector values.\n\nMachine and Storage tier filter node-owned panels. SeaweedFS S3 bucket panels are cluster-wide and are not filtered by node ownership. CNPG table panels are also cluster-wide; their filters use namespace, job, database, and schema.\n\nThe Storage tier selector filters by node labels; Machine filters node-owned panels. This is node-level filtering. If a node later has both SSD and HDD, per-device media labels will be needed to separate those disks."
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
                title="CNPG table detail · cluster-wide", grid_pos=dashboard.GridPos(h=1, w=24, y=16), id_val=25
            ),
            _bytes_panel(
                18,
                "Largest CNPG tables now",
                f"sort_desc(topk(10, {_CNPG_TABLE_BYTES}))",
                legend="{{job}} {{datname}}.{{schemaname}}.{{relname}}",
                description="Current allocated bytes for the ten largest tables and materialized views across the selected CNPG namespaces and jobs. The exporter reports the primary only. Grouping by namespace, job, database, schema, and relation collapses pod labels and avoids counting overlapping primary scrapes twice. The Machine and Storage tier selectors do not apply.",
                position=dashboard.GridPos(y=17, w=24, h=8),
                table_view=True,
            ),
            _bytes_panel(
                19,
                "Largest CNPG tables over time",
                _CNPG_TABLE_BYTES_OVER_TIME,
                legend="{{job}} {{datname}}.{{schemaname}}.{{relname}}",
                description="Allocated bytes over time for the ten largest tables at the end of the selected time range. The end-ranked set stays fixed for the whole graph. The exporter reports the primary only; grouping by the logical table labels collapses pod labels and avoids counting overlapping primary scrapes twice.",
                position=dashboard.GridPos(x=0, y=26, w=12, h=8),
            ),
            _bytes_panel(
                20,
                "Fastest-growing CNPG tables · 1h byte change",
                _CNPG_TABLE_GROWTH_OVER_TIME,
                legend="{{job}} {{datname}}.{{schemaname}}.{{relname}}",
                description="One-hour change in allocated table bytes, calculated with delta because the source is a gauge. The ten fastest growers at the end of the selected range stay fixed for the whole graph; earlier points for those tables may be flat or negative. The metric is newly collected, so until a full hour is available this rolling estimate uses the samples collected so far and extrapolates across the one-hour window. The exporter reports the primary only; grouping by logical table labels avoids counting overlapping primary scrapes twice.",
                position=dashboard.GridPos(x=12, y=26, w=12, h=8),
            ),
            dashboard.RowPanel(
                title="SeaweedFS volume-server detail", grid_pos=dashboard.GridPos(h=1, w=24, y=36), id_val=40
            ),
            _bytes_panel(
                8,
                "SeaweedFS volume-server disks by machine now",
                'sort_desc(max by (node, pod, name, type) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type=~"all|avail|used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"})))',
                legend="{{node}} {{pod}} {{name}} {{type}}",
                description="Volume-server all/available/used resource gauges from pods on the selected machine. These per-server values are collector detail and are not additive to the filesystem or Piraeus pool totals.",
                position=dashboard.GridPos(y=37),
                table_view=True,
            ),
            _bytes_panel(
                9,
                "SeaweedFS volume-server disk use by machine over time",
                'max by (node, pod, name) (SeaweedFS_volumeServer_resource{namespace="seaweedfs",type="used"} * on (namespace, pod) group_left (node) max by (namespace, pod, node) (kube_pod_info{namespace="seaweedfs",node=~"$node"}))',
                legend="{{node}} {{pod}} {{name}}",
                description="Volume-server reported used bytes per selected storage node and server disk. This is a per-collector view; do not add it to the independent pool and filesystem accounting layers.",
                position=dashboard.GridPos(x=12, y=37),
            ),
            _bytes_panel(
                14,
                "SeaweedFS collection bytes by machine now",
                f"sort_desc({_COLLECTION_BYTES})",
                legend="{{node}} {{collection}}",
                description="Collection bytes reported by volume-server pods on the selected machine. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                position=dashboard.GridPos(y=46),
                table_view=True,
            ),
            _bytes_panel(
                15,
                "Largest SeaweedFS collections by machine over time",
                f"topk(10, {_COLLECTION_BYTES})",
                legend="{{node}} {{collection}}",
                description="Collection bytes reported by volume-server pods on the selected machine over time. Each replica is counted on its storage node; these collector values overlap physical bucket bytes.",
                position=dashboard.GridPos(x=12, y=46),
            ),
            dashboard.RowPanel(
                title="SeaweedFS S3 bucket detail · cluster-wide",
                grid_pos=dashboard.GridPos(h=1, w=24, y=55),
                id_val=41,
            ),
            _bytes_panel(
                10,
                "SeaweedFS S3 logical bucket size now (cluster-wide)",
                f"sort_desc({_LOGICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                position=dashboard.GridPos(y=56),
                table_view=True,
            ),
            _bytes_panel(
                11,
                "Largest SeaweedFS S3 logical buckets over time (cluster-wide)",
                f"topk(10, {_LOGICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="Logical S3 object bytes by bucket across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; logical bytes omit replicas.",
                position=dashboard.GridPos(x=12, y=56),
            ),
            _bytes_panel(
                12,
                "SeaweedFS S3 physical bucket size now (cluster-wide)",
                f"sort_desc({_PHYSICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership.",
                position=dashboard.GridPos(y=65),
                table_view=True,
            ),
            _bytes_panel(
                13,
                "Largest SeaweedFS S3 physical buckets over time (cluster-wide)",
                f"topk(10, {_PHYSICAL_BUCKET_BYTES})",
                legend="{{bucket}}",
                description="S3-reported physical bucket bytes across the cluster. The Machine selector does not apply because this metric has no storage-node ownership; these overlap with collection-level replica bytes.",
                position=dashboard.GridPos(x=12, y=65),
            ),
            dashboard.RowPanel(
                title="ClickHouse MergeTree detail", grid_pos=dashboard.GridPos(h=1, w=24, y=74), id_val=42
            ),
            _bytes_panel(
                16,
                "ClickHouse MergeTree bytes by machine now",
                f"sort_desc({_CLICKHOUSE_BYTES})",
                legend="{{node}} {{pod}}",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                position=dashboard.GridPos(y=75),
                table_view=True,
            ),
            _bytes_panel(
                17,
                "ClickHouse MergeTree bytes by machine over time",
                _CLICKHOUSE_BYTES,
                legend="{{node}} {{pod}}",
                description="MergeTree bytes reported by ClickHouse pods on the selected machine, including the existing system-table metric. This per-pod collector detail is separate from host filesystem and Piraeus pool use.",
                position=dashboard.GridPos(x=12, y=75),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                storage_tier_variable(),
                mimir_multi_variable(
                    name="node",
                    label="Machine",
                    query='label_values(kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"}, node)',
                ),
                mimir_multi_variable(
                    name="pg_namespace",
                    label="PG namespace",
                    query="label_values(cnpg_pg_table_total_bytes, namespace)",
                ),
                mimir_multi_variable(
                    name="pg_job",
                    label="PG cluster job",
                    query='label_values(cnpg_pg_table_total_bytes{namespace=~"$pg_namespace"}, job)',
                ),
                mimir_multi_variable(
                    name="pg_database",
                    label="Database",
                    query='label_values(cnpg_pg_table_total_bytes{namespace=~"$pg_namespace",job=~"$pg_job"}, datname)',
                ),
                mimir_multi_variable(
                    name="pg_schema",
                    label="Schema",
                    query='label_values(cnpg_pg_table_total_bytes{namespace=~"$pg_namespace",job=~"$pg_job",datname=~"$pg_database"}, schemaname)',
                ),
            ]
        ),
    )
