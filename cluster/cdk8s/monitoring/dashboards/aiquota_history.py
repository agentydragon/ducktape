"""AIQuota history dashboard."""

from grafana_foundation_sdk.models import common, dashboard, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import clickhouse_query


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="aiquota-history",
        title="AIQuota history",
        tags=["analytics", "aiquota", "clickhouse"],
        editable=False,
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-30d"),
        refresh="5m",
        schema_version=41,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="table",
                id_val=1,
                targets=[
                    clickhouse_query(
                        sql="SELECT provider, if(empty(window_name), '(default)', window_name) AS window, formatReadableTimeDelta(toUInt64(argMax(window_seconds, observed_at))) AS duration, round(argMax(remaining_percent, observed_at), 1) AS remaining_percent, round(argMax(used_percent, observed_at), 1) AS used_percent, formatReadableTimeDelta(toUInt64(argMax(reset_seconds, observed_at))) AS resets_in, formatDateTime(max(observed_at), '%Y-%m-%d %H:%i:%S') AS last_observed_at FROM aiquota.aiquota_windows WHERE $__timeFilter(observed_at) GROUP BY provider, window_name, window_seconds ORDER BY provider, window_seconds, window_name"
                    )
                ],
                title="Current quota by provider and window",
                datasource=common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse"),
                grid_pos=dashboard.GridPos(h=10, w=24),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=2,
                targets=[
                    clickhouse_query(
                        sql="SELECT observed_at AS time, remaining_percent AS value, concat(provider, ' / ', if(empty(window_name), '(default)', window_name), ' / ', formatReadableTimeDelta(toUInt64(window_seconds))) AS metric FROM aiquota.aiquota_windows WHERE $__timeFilter(observed_at) ORDER BY time, metric"
                    )
                ],
                title="Quota remaining by provider and window",
                datasource=common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse"),
                grid_pos=dashboard.GridPos(h=10, w=24, y=10),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, calcs=["lastNotNull", "min"], show_legend=True
                    ),
                    tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.NONE),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="percent",
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, fill_opacity=15
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=3,
                targets=[
                    clickhouse_query(
                        sql="SELECT observed_at, source, status_code, content_type, raw_body_size_bytes, raw_body_truncated, error, raw_body_sha256 FROM (SELECT observed_at, source, status_code, content_type, raw_body_size_bytes, raw_body_truncated, error, raw_body_sha256 FROM aiquota.raw_http_observations WHERE $__timeFilter(observed_at) ORDER BY observed_at DESC LIMIT 200) AS latest_observations ORDER BY observed_at ASC",
                        format=1,
                    )
                ],
                title="Raw upstream observation metadata",
                datasource=common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse"),
                grid_pos=dashboard.GridPos(h=10, w=24, y=20),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    clickhouse_query(
                        sql="SELECT observed_at AS time, max(extra_spend_used_usd) AS used_usd, max(extra_spend_limit_usd) AS monthly_limit_usd FROM aiquota.aiquota_windows WHERE provider = 'claude' AND extra_spend_enabled = true AND $__timeFilter(observed_at) GROUP BY observed_at ORDER BY time"
                    )
                ],
                title="Claude extra usage (current billing month)",
                datasource=common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse"),
                grid_pos=dashboard.GridPos(h=10, w=24, y=30),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(
                        display_mode=common.LegendDisplayMode.TABLE, calcs=["lastNotNull", "max"], show_legend=True
                    ),
                    tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.NONE),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="currencyUSD",
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, fill_opacity=15
                        ),
                    )
                ),
            ),
        ],
    )
