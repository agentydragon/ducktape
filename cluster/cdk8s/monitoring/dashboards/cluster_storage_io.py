"""Storage I/O dashboard."""

from grafana_foundation_sdk.models import common, dashboard, stat, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    hide_time,
    last_value,
    loki_datasource,
    loki_query,
    mimir,
    mimir_multi_variable,
    mimir_query,
    mimir_table_query,
    series_options,
    storage_tier_variable,
    topk_variable,
)

_STORAGE_TIER = 'max by (node, label_storage_allegedly_works_tier) (kube_node_labels{node=~"$node", label_storage_allegedly_works_tier=~"$storage_tier"})'
_EXPORTER_NODES = 'max by (namespace, pod, node) (kube_pod_info{pod=~"prometheus-node-exporter-.+"})'


def _physical_disk_rate(metric: str) -> str:
    return f'sum by (node, device) (((rate(node_disk_{metric}_total{{device!~"^(loop|ram|fd|dm-|sr|drbd).*"}}[5m]) * on (namespace, pod) group_left (node) {_EXPORTER_NODES}) * on (node) group_left () {_STORAGE_TIER}))'


def _top_container_writers(metric: str) -> str:
    return f'topk(${{topk}}, sum by (namespace, pod, container, node, device) (rate(container_fs_{metric}_total{{namespace!="", pod!="", container!="", container!="POD", node=~"$node"}}[5m]) * on (node) group_left () {_STORAGE_TIER}))'


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="cluster-storage-io",
        title="Storage I/O",
        tags=["cluster", "storage", "io"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-1h"),
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read this dashboard",
                grid_pos=dashboard.GridPos(h=5, w=24),
                options=text.Options(
                    content="The **Storage tier** selector scopes node-owned panels to Kubernetes nodes labeled `storage.allegedly.works/tier`; **Both** selects `ssd` and `hdd`. The node selector narrows within that set. Nodes without a tier label, including Wyrm2 and its local OpenEBS volumes, are omitted.\n\nPhysical device panels show block-device writes and exclude DRBD logical devices. Container panels show cAdvisor filesystem writes. SeaweedFS, Loki, and Mimir metrics are grouped under **Cluster-wide service traffic**; the tier selector does not apply because those series have no storage-node ownership. These layers overlap and must not be added together as one total. Rate panels use a fixed 5-minute window so the same PromQL and LogQL remain valid in dashboard rendering and Explore."
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=2,
                targets=[mimir_query(_physical_disk_rate("written_bytes"), legend="{{node}} {{device}}").build()],
                title="Physical disk write throughput",
                description="Block-device writes on nodes in the selected storage tier; DRBD logical devices are excluded.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=5),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[mimir_query(_physical_disk_rate("writes_completed"), legend="{{node}} {{device}}").build()],
                title="Physical disk write IOPS",
                description="Completed block-device writes per second on nodes in the selected storage tier; DRBD logical devices are excluded.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=13),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="ops")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=6,
                targets=[mimir_query(_physical_disk_rate("io_time_seconds"), legend="{{node}} {{device}}").build()],
                title="Physical disk busy fraction",
                description="Fraction of the last five minutes each block device had I/O in flight on nodes in the selected storage tier.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=21),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, max_val=1)
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    mimir_table_query(
                        _top_container_writers("writes_bytes"),
                        legend="{{namespace}}/{{pod}} {{container}} {{node}} {{device}}",
                    ).build()
                ],
                title="Top container writers",
                description="cAdvisor filesystem writes grouped by namespace, pod, container, node, and device on nodes in the selected storage tier. This is producer attribution, not a physical-block-device total.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=29),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=10,
                targets=[
                    mimir_table_query(
                        _top_container_writers("writes"),
                        legend="{{namespace}}/{{pod}} {{container}} {{node}} {{device}}",
                    ).build()
                ],
                title="Top container write IOPS",
                description="cAdvisor filesystem write operations grouped by namespace, pod, container, node, and device on nodes in the selected storage tier.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=39),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="ops")),
            ),
            dashboard.RowPanel(
                title="Cluster-wide service traffic · tier selector does not apply",
                grid_pos=dashboard.GridPos(h=1, w=24, y=49),
                id_val=19,
            ),
            dashboard.Panel(
                type_val="table",
                id_val=12,
                targets=[
                    mimir_table_query(
                        'topk(${topk}, sum by (bucket) (rate(SeaweedFS_s3_bucket_traffic_received_bytes_total{bucket!=""}[5m])))',
                        legend="{{bucket}}",
                    ).build()
                ],
                title="Cluster-wide S3 bytes received by bucket",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Logical bytes received by SeaweedFS S3, broken down by bucket. This is ingress to SeaweedFS and is not the same as physical disk writes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, y=50),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=13,
                targets=[
                    mimir_table_query(
                        'topk(${topk}, sum by (bucket) (rate(SeaweedFS_s3_bucket_traffic_sent_bytes_total{bucket!=""}[5m])))',
                        legend="{{bucket}}",
                    ).build()
                ],
                title="Cluster-wide S3 bytes sent by bucket",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Logical bytes sent by SeaweedFS S3, broken down by bucket.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, x=12, y=50),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    loki_query('sum by (namespace) (bytes_rate({namespace=~".+"}[5m]))').query_type("range").build()
                ],
                title="Cluster-wide Loki log bytes by namespace",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Logical log volume accepted by Loki, grouped by namespace.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=10, y=60),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    loki_query('topk(${topk}, sum by (namespace, pod) (bytes_rate({namespace=~".+"}[5m])))')
                    .query_type("range")
                    .build()
                ],
                title="Cluster-wide Loki log bytes by pod",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Logical log volume accepted by Loki, grouped by namespace and pod.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=10, x=12, y=60),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="Bps")),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=16,
                targets=[
                    mimir_table_query(
                        'topk(${topk}, sum by (job, namespace, service, instance) (sum_over_time(scrape_samples_post_metric_relabeling{job!=""}[5m]) / 300))',
                        legend="{{job}} {{namespace}}/{{service}} {{instance}}",
                    ).build()
                ],
                title="Cluster-wide scraped samples/s by target",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Estimated samples/s per scrape target, derived from the per-scrape sample count over five minutes. Shows only the selected top rows; this pre-ingestion estimate does not include direct OTLP or Tempo metrics-generator writes counted by Mimir.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, y=70),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="ops")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=17,
                targets=[
                    mimir_query(
                        'sum(rate(cortex_distributor_received_samples_total{user="anonymous"}[5m]))', legend="samples/s"
                    ).build()
                ],
                title="Cluster-wide Mimir samples/s",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Samples accepted by Mimir's distributor for the anonymous tenant, including scrape and direct-write sources. Rejected and deduplicated samples are excluded, so this is not expected to equal the scrape-target estimate.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=6, x=12, y=70),
                options=stat.Options(reduce_options=last_value()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="ops", decimals=0)),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=18,
                targets=[
                    mimir_query(
                        'sum(sum_over_time(scrape_samples_post_metric_relabeling{job!=""}[5m])) / 300',
                        legend="samples/s",
                    ).build()
                ],
                title="Cluster-wide scraped samples/s",
                description="Cluster-wide service telemetry; storage tier selection does not apply. Estimated average rate from per-scrape sample counts over five minutes, across all scrape targets. Excludes direct OTLP and Tempo metrics-generator writes sent to Mimir.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=6, x=18, y=70),
                options=stat.Options(reduce_options=last_value()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="ops", decimals=0)),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                storage_tier_variable(),
                mimir_multi_variable(
                    name="node",
                    label="Cluster node",
                    query="label_values(kube_node_info, node)",
                    sort=None,
                    current=dashboard.VariableOption(text="All cluster nodes", value=["$__all"]),
                    all_value="optiplex|ovh-.*",
                    regex="/^(optiplex|ovh-.*)$/",
                ),
                topk_variable("Rows"),
            ]
        ),
    )
