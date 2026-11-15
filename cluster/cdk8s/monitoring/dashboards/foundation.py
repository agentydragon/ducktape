"""Shared query and presentation defaults for authored Grafana dashboards."""

from grafana_foundation_sdk.builders import loki, prometheus, timeseries
from grafana_foundation_sdk.builders.common import VizTooltipOptions
from grafana_foundation_sdk.cog.runtime import UnknownDataquery
from grafana_foundation_sdk.models import (  # Builder/model name collision.
    common,
    dashboard,
    timeseries as timeseries_model,
)


def mimir() -> common.DataSourceRef:
    """The Operator replaces this input with the provisioned datasource UID."""
    return common.DataSourceRef(type_val="prometheus", uid="${DS_MIMIR}")


def mimir_query(expr: str, legend: str | None = None, ref_id: str = "A") -> prometheus.Dataquery:
    """Return a native query builder; callers choose instant/range/format explicitly."""
    query = prometheus.Dataquery().datasource(mimir()).expr(expr).ref_id(ref_id)
    if legend is not None:
        query.legend_format(legend)
    return query


def line_panel() -> timeseries.Panel:
    return (
        timeseries.Panel()
        .datasource(mimir())
        .draw_style(common.GraphDrawStyle.LINE)
        .line_width(1)
        .fill_opacity(10)
        .show_points(common.VisibilityMode.NEVER)
        .tooltip(VizTooltipOptions().mode(common.TooltipDisplayMode.MULTI).sort(common.SortOrder.DESCENDING))
    )


def loki_datasource() -> common.DataSourceRef:
    return common.DataSourceRef(type_val="loki", uid="${DS_LOKI}")


def loki_query(expr: str, legend: str | None = None, ref_id: str = "A") -> loki.Dataquery:
    query = loki.Dataquery().datasource(loki_datasource()).expr(expr).ref_id(ref_id)
    if legend is not None:
        query.legend_format(legend)
    return query


class HeaderLink(dashboard.DashboardLink):
    def to_json(self) -> dict[str, object]:
        payload = super().to_json()
        # The SDK cannot omit placement; Grafana requires it absent for header links.
        payload.pop("placement")
        return payload


def last_value() -> common.ReduceDataOptions:
    return common.ReduceDataOptions(values=False, calcs=["lastNotNull"], fields="")


def series_options(sort: common.SortOrder = common.SortOrder.NONE) -> timeseries_model.Options:
    return timeseries_model.Options(
        legend=common.VizLegendOptions(show_legend=True),
        tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI, sort=sort),
    )


def line_style(draw_style: common.GraphDrawStyle | None = common.GraphDrawStyle.LINE) -> common.GraphFieldConfig:
    return common.GraphFieldConfig(
        draw_style=draw_style, line_width=2, fill_opacity=10, show_points=common.VisibilityMode.NEVER, span_nulls=False
    )


def postgres_query(sql: str, format: str = "table", ref_id: str = "A") -> UnknownDataquery:
    # The SDK has no PostgreSQL query model; keep the plugin's native query shape.
    return UnknownDataquery(
        {
            "datasource": common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
            "editorMode": "code",
            "format": format,
            "rawQuery": True,
            "rawSql": sql,
            "refId": ref_id,
        }
    )


def clickhouse_query(sql: str, format: int = 0, ref_id: str = "A") -> UnknownDataquery:
    # The SDK has no ClickHouse query model; keep the plugin's native query shape.
    return UnknownDataquery(
        {
            "datasource": common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse"),
            "format": format,
            "queryType": "sql",
            "rawSql": sql,
            "refId": ref_id,
        }
    )
