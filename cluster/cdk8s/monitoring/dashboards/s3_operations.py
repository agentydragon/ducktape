"""S3 Operations dashboard."""

from grafana_foundation_sdk.models import common, dashboard, stat, text, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import last_value, mimir, mimir_query


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="s3-operations",
        title="S3 Operations",
        tags=["cluster", "seaweedfs", "s3", "operations"],
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
                        'max by (service, pod) (up{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$"}) or on (service) label_replace(label_replace(vector(2), "service", "public-s3", "", ""), "collection", "not-scraped", "", "") or on (service) label_replace(label_replace(vector(2), "service", "seaweedfs-s3", "", ""), "collection", "not-scraped", "", "")',
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
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=3,
                targets=[
                    mimir_query(
                        'sum by (service, type) (rate(SeaweedFS_s3_request_total{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m]))',
                        legend="{{service}} / {{type}}",
                    ).build()
                ],
                title="S3 requests/s",
                description="Request counter rate by S3 service and operation. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=10),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="ops", decimals=2, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    mimir_query(
                        'sum by (service, type) (rate(SeaweedFS_s3_request_total{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket", code=~"5.."}[5m])) or on (service, type) (0 * sum by (service, type) (rate(SeaweedFS_s3_request_total{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m])))',
                        legend="{{service}} / {{type}}",
                    ).build()
                ],
                title="HTTP 5xx requests/s",
                description="Rate of S3 requests whose response code is 5xx, grouped by service and operation. A real zero is allowed when there are no recent errors. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=10),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="ops", decimals=3, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    mimir_query(
                        'histogram_quantile(0.95, sum by (service, type, le) (rate(SeaweedFS_s3_request_seconds_bucket{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m])))',
                        legend="{{service}} / {{type}}",
                    ).build()
                ],
                title="Request duration p95",
                description="95th percentile of full S3 request processing duration in seconds. This includes response transfer time; large object downloads can dominate it. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=18),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", decimals=3, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=6,
                targets=[
                    mimir_query(
                        'histogram_quantile(0.95, sum by (service, type, le) (rate(SeaweedFS_s3_time_to_first_byte_millisecond_bucket{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m]))) / 1000',
                        legend="{{service}} / {{type}}",
                    ).build()
                ],
                title="Time to first byte p95",
                description="95th percentile time to first byte, converted from the exporter's millisecond buckets to seconds. Compare with full request duration above. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=18),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", decimals=3, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=7,
                targets=[
                    mimir_query(
                        'sum by (service, bucket) (rate(SeaweedFS_s3_bucket_traffic_received_bytes_total{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m]))',
                        legend="{{service}} / {{bucket}}",
                    ).build()
                ],
                title="S3 bytes received by bucket",
                description="S3 client-to-gateway bytes per second, grouped by service and bucket. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=26),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="Bps", decimals=1, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query(
                        'sum by (service, bucket) (rate(SeaweedFS_s3_bucket_traffic_sent_bytes_total{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$", bucket=~"$bucket"}[5m]))',
                        legend="{{service}} / {{bucket}}",
                    ).build()
                ],
                title="S3 bytes sent by bucket",
                description="S3 gateway-to-client bytes per second, grouped by service and bucket. The Bucket selector applies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=26),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="Bps", decimals=1, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query(
                        'sum by (service, type) (SeaweedFS_s3_in_flight_requests{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$"})',
                        legend="{{service}} / {{type}}",
                    ).build()
                ],
                title="S3 requests in flight",
                description="Current in-flight requests grouped by service and operation. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=34),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", decimals=0, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[
                    mimir_query(
                        'sum by (service) (SeaweedFS_s3_in_flight_upload_count{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$"})',
                        legend="{{service}}",
                    ).build()
                ],
                title="S3 uploads in flight",
                description="Current upload count grouped by service. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=34),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", decimals=0, min_val=0)
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[
                    mimir_query(
                        'sum by (service) (SeaweedFS_s3_in_flight_upload_bytes{namespace="seaweedfs", service=~"^(public-s3|seaweedfs-s3)$"})',
                        legend="{{service}}",
                    ).build()
                ],
                title="S3 upload bytes in flight",
                description="Current bytes being uploaded, grouped by service. This gauge has no bucket label and is gateway-wide; the Bucket selector does not affect it.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=42),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, show_legend=True, calcs=["lastNotNull", "max"]
                    ),
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes", decimals=0, min_val=0)
                ),
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
