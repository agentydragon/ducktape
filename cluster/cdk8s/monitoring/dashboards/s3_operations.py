"""S3 Operations dashboard."""

from grafana_foundation_sdk.models import common, dashboard, stat, text

from cluster.cdk8s.monitoring.dashboards.foundation import last_value, mimir, mimir_query, series_options

_SERIES_HEIGHT = 8
_SERIES_WIDTH = 12
_S3_SERVICES = 'namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$"'
_BUCKET_FILTER = 'bucket=~"$bucket"'


def _metric_panel(
    id: int, title: str, expr: str, *, description: str, legend: str, unit: str, decimals: int
) -> dashboard.Panel:
    index = id - 3
    return dashboard.Panel(
        type_val="timeseries",
        id_val=id,
        title=title,
        description=description,
        datasource=mimir(),
        targets=[mimir_query(expr, legend=legend).build()],
        grid_pos=dashboard.GridPos(
            h=_SERIES_HEIGHT,
            w=24 if id == 11 else _SERIES_WIDTH,
            x=(index % 2) * _SERIES_WIDTH,
            y=10 + (index // 2) * _SERIES_HEIGHT,
        ),
        options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull", "max")),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(unit=unit, decimals=decimals, min_val=0)
        ),
    )


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="s3-operations",
        title="S3 Operations",
        tags=["cluster", "seaweedfs", "s3", "operations"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-1h"),
        week_start="",
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="Reading this dashboard",
                grid_pos=dashboard.GridPos(h=4, w=24),
                options=text.Options(
                    content="Choose a bucket to filter request/error rates, p95s, and per-bucket traffic. `seaweedfs-s3` is internal; `public-s3` is public. Missing request series are not proof of zero traffic; check scrape status.\n\nRequest duration includes full response transfer; TTFB is time to first byte. Both percentiles use a rolling five-minute window. In-flight gauges have no bucket label and stay gateway-wide."
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query(
                        f'max by (service, pod) (up{{{_S3_SERVICES}}}) or on (service) label_replace(label_replace(vector(2), "service", "public-s3", "", ""), "collection", "not-scraped", "", "") or on (service) label_replace(label_replace(vector(2), "service", "seaweedfs-s3", "", ""), "collection", "not-scraped", "", "")',
                        legend="{{service}} / {{pod}} {{collection}}",
                    )
                    .instant()
                    .build()
                ],
                title="S3 metrics scrape status",
                description="Each pod shows its current scrape state: 1 = Up, 0 = Down. An expected service without any target series is added as 2 = Not collected and labeled `not-scraped`. These values describe collection, not traffic.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=24, y=4),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    text_mode=common.BigValueTextMode.VALUE_AND_NAME,
                    reduce_options=last_value(),
                    orientation=common.VizOrientation.HORIZONTAL,
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        decimals=0,
                        mappings=[
                            dashboard.ValueMap(
                                options={
                                    "0": dashboard.ValueMappingResult(text="Down", color="red"),
                                    "1": dashboard.ValueMappingResult(text="Up", color="green"),
                                    "2": dashboard.ValueMappingResult(text="Not collected", color="orange"),
                                }
                            )
                        ],
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[
                                dashboard.Threshold(color="red"),
                                dashboard.Threshold(value=1, color="green"),
                                dashboard.Threshold(value=2, color="orange"),
                            ]
                        ),
                    )
                ),
            ),
            _metric_panel(
                3,
                "S3 requests/s",
                f"sum by (service, type) (rate(SeaweedFS_s3_request_total{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m]))",
                description="Request counter rate by S3 service and operation. The Bucket selector applies.",
                legend="{{service}} / {{type}}",
                unit="ops",
                decimals=2,
            ),
            _metric_panel(
                4,
                "HTTP 5xx requests/s",
                f'sum by (service, type) (rate(SeaweedFS_s3_request_total{{{_S3_SERVICES}, {_BUCKET_FILTER}, code=~"5.."}}[5m])) or on (service, type) (0 * sum by (service, type) (rate(SeaweedFS_s3_request_total{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m])))',
                description="Rate of S3 requests whose response code is 5xx, grouped by service and operation. A real zero is allowed when there are no recent errors. The Bucket selector applies.",
                legend="{{service}} / {{type}}",
                unit="ops",
                decimals=3,
            ),
            _metric_panel(
                5,
                "Request duration p95",
                f"histogram_quantile(0.95, sum by (service, type, le) (rate(SeaweedFS_s3_request_seconds_bucket{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m])))",
                description="95th percentile of full S3 request processing duration in seconds. This includes response transfer time; large object downloads can dominate it. The Bucket selector applies.",
                legend="{{service}} / {{type}}",
                unit="s",
                decimals=3,
            ),
            _metric_panel(
                6,
                "Time to first byte p95",
                f"histogram_quantile(0.95, sum by (service, type, le) (rate(SeaweedFS_s3_time_to_first_byte_millisecond_bucket{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m]))) / 1000",
                description="95th percentile time to first byte, converted from the exporter's millisecond buckets to seconds. Compare with full request duration above. The Bucket selector applies.",
                legend="{{service}} / {{type}}",
                unit="s",
                decimals=3,
            ),
            _metric_panel(
                7,
                "S3 bytes received by bucket",
                f"sum by (service, bucket) (rate(SeaweedFS_s3_bucket_traffic_received_bytes_total{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m]))",
                description="S3 client-to-gateway bytes per second, grouped by service and bucket. The Bucket selector applies.",
                legend="{{service}} / {{bucket}}",
                unit="Bps",
                decimals=1,
            ),
            _metric_panel(
                8,
                "S3 bytes sent by bucket",
                f"sum by (service, bucket) (rate(SeaweedFS_s3_bucket_traffic_sent_bytes_total{{{_S3_SERVICES}, {_BUCKET_FILTER}}}[5m]))",
                description="S3 gateway-to-client bytes per second, grouped by service and bucket. The Bucket selector applies.",
                legend="{{service}} / {{bucket}}",
                unit="Bps",
                decimals=1,
            ),
            _metric_panel(
                9,
                "S3 requests in flight",
                f"sum by (service, type) (SeaweedFS_s3_in_flight_requests{{{_S3_SERVICES}}})",
                description="Current in-flight requests grouped by service and operation. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                legend="{{service}} / {{type}}",
                unit="short",
                decimals=0,
            ),
            _metric_panel(
                10,
                "S3 uploads in flight",
                f"sum by (service) (SeaweedFS_s3_in_flight_upload_count{{{_S3_SERVICES}}})",
                description="Current upload count grouped by service. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                legend="{{service}}",
                unit="short",
                decimals=0,
            ),
            _metric_panel(
                11,
                "S3 upload bytes in flight",
                f"sum by (service) (SeaweedFS_s3_in_flight_upload_bytes{{{_S3_SERVICES}}})",
                description="Current bytes being uploaded, grouped by service. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                legend="{{service}}",
                unit="bytes",
                decimals=0,
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="bucket",
                    label="Bucket",
                    query='label_values(SeaweedFS_s3_request_total{namespace="seaweedfs"}, bucket)',
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All buckets", value="$__all"),
                    options=[dashboard.VariableOption(selected=True, text="All buckets", value="$__all")],
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    include_all=True,
                    all_value=".*",
                    definition='label_values(SeaweedFS_s3_request_total{namespace="seaweedfs"}, bucket)',
                )
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
