"""Shared query and presentation defaults for authored Grafana dashboards."""

from collections.abc import Mapping, Sequence

from grafana_foundation_sdk.builders import loki, prometheus, timeseries
from grafana_foundation_sdk.builders.common import VizLegendOptions, VizTooltipOptions
from grafana_foundation_sdk.builders.dashboard import QueryVariable
from grafana_foundation_sdk.cog.runtime import UnknownDataquery
from grafana_foundation_sdk.cog.variants import Dataquery
from grafana_foundation_sdk.models import (  # Builder/model name collision.
    common,
    dashboard,
    prometheus as prometheus_model,
    stat,
    statetimeline,
    timeseries as timeseries_model,
)


def mimir() -> common.DataSourceRef:
    """The Operator replaces this input with the provisioned datasource UID."""
    return common.DataSourceRef(type_val="prometheus", uid="${DS_MIMIR}")


def mimir_query(expr: str, legend: str | None = None, ref_id: str = "A") -> prometheus.Dataquery:
    """Grafana defaults to range queries; use .instant() for a current-value query."""
    query = prometheus.Dataquery().datasource(mimir()).expr(expr).ref_id(ref_id)
    if legend is not None:
        query.legend_format(legend)
    return query


def mimir_table_query(expr: str, legend: str | None = None, ref_id: str = "A") -> prometheus.Dataquery:
    return mimir_query(expr, legend, ref_id).format(prometheus_model.PromQueryFormat.TABLE).instant()


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


def mimir_timeseries_panel(
    panel_id: int,
    title: str,
    expression: str,
    legend: str | None = None,
    *,
    unit: str = "short",
    description: str | None = None,
) -> timeseries.Panel:
    x = 0 if panel_id % 2 else 12
    y = ((panel_id - 1) // 2) * 8
    panel = (
        line_panel()
        .id(panel_id)
        .title(title)
        .grid_pos(dashboard.GridPos(h=8, x=x, y=y, w=12))
        .unit(unit)
        .legend(VizLegendOptions().display_mode(common.LegendDisplayMode.LIST).show_legend(True))
        .with_target(mimir_query(expression, legend))
    )
    if description:
        panel.description(description)
    return panel


def loki_datasource() -> common.DataSourceRef:
    return common.DataSourceRef(type_val="loki", uid="${DS_LOKI}")


def loki_query(expr: str, legend: str | None = None) -> loki.Dataquery:
    query = loki.Dataquery().datasource(loki_datasource()).expr(expr).ref_id("A")
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


def series_options(
    sort: common.SortOrder = common.SortOrder.NONE, *, legend_calcs: Sequence[str] | None = None
) -> timeseries_model.Options:
    return timeseries_model.Options(
        legend=common.VizLegendOptions(
            show_legend=True,
            display_mode=common.LegendDisplayMode.TABLE if legend_calcs is not None else common.LegendDisplayMode.LIST,
            calcs=list(legend_calcs) if legend_calcs is not None else None,
        ),
        tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI, sort=sort),
    )


def stat_options(color_mode: common.BigValueColorMode = common.BigValueColorMode.VALUE) -> stat.Options:
    return stat.Options(graph_mode=common.BigValueGraphMode.NONE, color_mode=color_mode, reduce_options=last_value())


def up_down_fields(*, timeline: bool = False) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            mappings=[
                dashboard.ValueMap(
                    options={
                        "0": dashboard.ValueMappingResult(text="Down", color="red"),
                        "1": dashboard.ValueMappingResult(text="Up", color="green"),
                    }
                )
            ],
            thresholds=dashboard.ThresholdsConfig(
                steps=[dashboard.Threshold(color="red"), dashboard.Threshold(value=1, color="green")]
            ),
            custom=statetimeline.FieldConfig(fill_opacity=80) if timeline else None,
        )
    )


def hide_time() -> dashboard.DataTransformerConfig:
    return dashboard.DataTransformerConfig(id_val="organize", options={"excludeByName": {"Time": True}})


def field_override(
    name: str, properties: Mapping[str, object], *, regex: bool = False
) -> dashboard.DashboardFieldConfigSourceOverrides:
    return dashboard.DashboardFieldConfigSourceOverrides(
        matcher=dashboard.MatcherConfig(id_val="byRegexp" if regex else "byName", options=name),
        properties=[dashboard.DynamicConfigValue(id_val=key, value=value) for key, value in properties.items()],
    )


def line_style(draw_style: common.GraphDrawStyle | None = common.GraphDrawStyle.LINE) -> common.GraphFieldConfig:
    return common.GraphFieldConfig(
        draw_style=draw_style, line_width=2, fill_opacity=10, show_points=common.VisibilityMode.NEVER, span_nulls=False
    )


def postgres_datasource() -> common.DataSourceRef:
    return common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz")


def postgres_query(sql: str, format: str = "table", ref_id: str = "A") -> UnknownDataquery:
    # The SDK has no PostgreSQL query model; keep the plugin's native query shape.
    return UnknownDataquery(
        {
            "datasource": postgres_datasource(),
            "editorMode": "code",
            "format": format,
            "rawQuery": True,
            "rawSql": sql,
            "refId": ref_id,
        }
    )


def clickhouse_datasource() -> common.DataSourceRef:
    return common.DataSourceRef(type_val="grafana-clickhouse-datasource", uid="clickhouse")


def clickhouse_query(sql: str, format: int = 0) -> UnknownDataquery:
    # The SDK has no ClickHouse query model; keep the plugin's native query shape.
    return UnknownDataquery(
        {"datasource": clickhouse_datasource(), "format": format, "queryType": "sql", "rawSql": sql, "refId": "A"}
    )


def mimir_multi_variable(
    *,
    name: str,
    label: str | None,
    query: str,
    current: dashboard.VariableOption | None = None,
    refresh: dashboard.VariableRefresh = dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
    sort: dashboard.VariableSort | None = dashboard.VariableSort.ALPHABETICAL_ASC,
    all_value: str | None = None,
    regex: str | None = None,
    hide: dashboard.VariableHide | None = None,
) -> dashboard.VariableModel:
    return dashboard.VariableModel(
        name=name,
        label=label,
        query=query,
        datasource=mimir(),
        current=current if current is not None else dashboard.VariableOption(text="All", value=["$__all"]),
        multi=True,
        refresh=refresh,
        sort=sort,
        include_all=True,
        all_value=all_value,
        regex=regex,
        hide=hide,
        definition=query,
    )


def mimir_multi_query_variable(name: str, label: str, query: str) -> QueryVariable:
    """`mimir_multi_variable`'s builder twin, since `Dashboard.with_variable` takes builders: multi-select,
    All by default and matching anything, unsorted."""
    return (
        QueryVariable(name)
        .label(label)
        .datasource(mimir())
        .definition(query)
        .query(query)
        .include_all(True)
        .multi(True)
        .all_value(".*")
        .refresh(dashboard.VariableRefresh.ON_DASHBOARD_LOAD)
        .current(dashboard.VariableOption(text="All", value=["$__all"]))
    )


def storage_tier_variable() -> dashboard.VariableModel:
    return dashboard.VariableModel(
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
    )


def topk_variable(label: str) -> dashboard.VariableModel:
    """The `$topk` row-count choice that a panel's `topk(${topk}, ...)` reads."""
    return dashboard.VariableModel(
        type_val=dashboard.VariableType.CUSTOM,
        name="topk",
        label=label,
        query="10,25,50",
        current=dashboard.VariableOption(text="10", value="10"),
        options=[
            dashboard.VariableOption(selected=True, text="10", value="10"),
            dashboard.VariableOption(selected=False, text="25", value="25"),
            dashboard.VariableOption(selected=False, text="50", value="50"),
        ],
    )


def gateway_error_queries(interface: str, *, legend_prefix: str = "") -> list[Dataquery]:
    return [
        mimir_query(
            f"increase(att_gateway_{interface}_{direction}_{kind}_total[5m])",
            legend=f"{legend_prefix}{label} {kind}",
            ref_id=chr(ord("A") + index),
        ).build()
        for index, (direction, label, kind) in enumerate(
            (direction, label, kind)
            for direction, label in (("receive", "rx"), ("transmit", "tx"))
            for kind in ("errors", "drops")
        )
    ]
