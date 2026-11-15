"""Shared native builders for the Foundation SDK dashboard spike."""

from grafana_foundation_sdk.builders import prometheus, timeseries
from grafana_foundation_sdk.builders.common import VizTooltipOptions
from grafana_foundation_sdk.models import common


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
