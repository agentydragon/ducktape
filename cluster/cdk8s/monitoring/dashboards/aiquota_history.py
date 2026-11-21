"""AIQuota history dashboard."""

from textwrap import dedent

from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import clickhouse_datasource, clickhouse_query, series_options

_WINDOW_NAME = "if(empty(window_name), '(default)', window_name)"
_OBSERVATION_COLUMNS = "observed_at, source, account_id, status_code, content_type, raw_body_size_bytes, raw_body_truncated, error, raw_body_sha256"


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
                        sql=dedent(f"""\
                            SELECT
                              provider,
                              if(empty(account_id), 'unknown (legacy)', account_id) AS account,
                              {_WINDOW_NAME} AS window,
                              formatReadableTimeDelta(toUInt64(window_seconds)) AS duration,
                              round(latest.1, 1) AS remaining_percent,
                              round(latest.2, 1) AS used_percent,
                              if(dateDiff('second', latest.5, now64(3)) > 900, 'stale (>15m)', 'fresh') AS freshness,
                              multiIf(
                                isNull(reset_deadline), 'unknown',
                                reset_deadline <= now64(3), 'awaiting update',
                                formatReadableTimeDelta(toUInt64(greatest(0, dateDiff('second', now64(3), reset_deadline))))
                              ) AS resets_in,
                              formatDateTime(latest.5, '%Y-%m-%d %H:%i:%S') AS last_observed_at
                            FROM (
                              SELECT
                                provider, account_id, window_name, window_seconds,
                                argMax(tuple(remaining_percent, used_percent, reset_at, reset_seconds, observed_at), observed_at) AS latest,
                                coalesce(latest.3, if(latest.4 > 0, latest.5 + toIntervalSecond(toInt64(latest.4)), NULL)) AS reset_deadline
                              FROM aiquota.aiquota_windows
                              WHERE $__timeFilter(observed_at)
                              GROUP BY provider, account_id, window_name, window_seconds
                            )
                            ORDER BY provider, account_id, window_seconds, window_name
                            """).strip()
                    )
                ],
                title="Latest quota observation by provider and window",
                description="Latest observation in the selected range. Freshness is relative to now; the collector polls every five minutes. After 15 minutes without an observation it is marked stale. A passed reset deadline means awaiting an updated quota, not that usage has reset.",
                datasource=clickhouse_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=2,
                targets=[
                    clickhouse_query(
                        sql=dedent(f"""\
                            SELECT
                              observed_at AS time,
                              remaining_percent AS value,
                              concat(provider, ' / ', if(empty(account_id), 'unknown (legacy)', account_id), ' / ', {_WINDOW_NAME}, ' / ', formatReadableTimeDelta(toUInt64(window_seconds))) AS metric
                            FROM aiquota.aiquota_windows
                            WHERE $__timeFilter(observed_at)
                            ORDER BY time, metric
                            """).strip()
                    )
                ],
                title="Quota remaining by provider and window",
                datasource=clickhouse_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=10),
                options=series_options(legend_calcs=("lastNotNull", "min")),
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
                        sql=dedent(f"""\
                            SELECT {_OBSERVATION_COLUMNS}
                            FROM (
                              SELECT {_OBSERVATION_COLUMNS}
                              FROM aiquota.raw_http_observations
                              WHERE $__timeFilter(observed_at)
                              ORDER BY observed_at DESC
                              LIMIT 200
                            ) AS latest_observations
                            ORDER BY observed_at ASC
                            """).strip(),
                        format=1,
                    )
                ],
                title="Raw upstream observation metadata",
                datasource=clickhouse_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=20),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    clickhouse_query(
                        sql=dedent("""\
                            SELECT
                              observed_at AS time,
                              amount.2 AS value,
                              concat(if(empty(account_id), 'unknown (legacy)', account_id), ' / ', amount.1) AS metric
                            FROM aiquota.aiquota_windows
                            ARRAY JOIN [('used', extra_spend_used_usd), ('monthly limit', extra_spend_limit_usd)] AS amount
                            WHERE provider = 'claude' AND extra_spend_enabled = true AND $__timeFilter(observed_at)
                            ORDER BY time, metric
                            """).strip()
                    )
                ],
                title="Claude extra usage by account (current billing month)",
                datasource=clickhouse_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=30),
                options=series_options(legend_calcs=("lastNotNull", "max")),
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
