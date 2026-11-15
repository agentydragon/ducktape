"""Small Grafana Foundation SDK spike using the existing classic dashboard format."""

from grafana_foundation_sdk.builders import prometheus, timeseries
from grafana_foundation_sdk.builders.common import VizLegendOptions, VizTooltipOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard, TimePicker
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common, dashboard

_MIMIR = common.DataSourceRef(type_val="prometheus", uid="mimir")
_SIZE = 'max by (mountpoint) (node_filesystem_size_bytes{namespace="cpap-sync",service="cpap-gateway-metrics",endpoint="node-metrics",mountpoint="/"})'
_FREE = 'max by (mountpoint) (node_filesystem_free_bytes{namespace="cpap-sync",service="cpap-gateway-metrics",endpoint="node-metrics",mountpoint="/"})'


def _filesystem_panel(id: int, title: str, x: int) -> timeseries.Panel:
    return (
        timeseries.Panel()
        .id(id)
        .title(title)
        .description(
            "Guest root filesystem. Used bytes are size minus free; root-reserved blocks are included in free. "
            "Capacity is the filesystem visible inside the VM, separate from KubeVirt and host storage. "
            "Only the CPAP gateway guest filesystem collector is scraped by monitoring Alloy."
        )
        .datasource(_MIMIR)
        .grid_pos(dashboard.GridPos(h=8, w=12, x=x, y=0))
        .draw_style(common.GraphDrawStyle.LINE)
        .line_interpolation(common.LineInterpolation.LINEAR)
        .line_width(1)
        .show_points(common.VisibilityMode.NEVER)
        .overrides([])
        .legend(
            VizLegendOptions()
            .display_mode(common.LegendDisplayMode.TABLE)
            .placement(common.LegendPlacement.BOTTOM)
            .show_legend(True)
            .calcs(["lastNotNull"])
        )
        .tooltip(VizTooltipOptions().mode(common.TooltipDisplayMode.MULTI).sort(common.SortOrder.DESCENDING))
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("CPAP gateway disk")
        .uid("cpap-gateway-disk")
        .description(
            "Guest root filesystem capacity and use for the stateless CPAP gateway VM. "
            "The current guest has no separate /boot filesystem mount, so boot-partition usage is not exposed here. "
            "This does not measure OptiPlex host storage or Kubernetes pod storage."
        )
        .editable()
        .tooltip(dashboard.DashboardCursorSync.CROSSHAIR)
        .tags(["cpap", "kubevirt", "storage"])
        .refresh("1m")
        .time("now-24h", "now")
        .timezone(common.TimeZoneBrowser)
        .timepicker(TimePicker())
        .annotations([])
        .variables([])
        .links([])
        .version(1)
        .with_panel(
            _filesystem_panel(1, "Guest root filesystem: used and capacity", 0)
            .unit("bytes")
            .fill_opacity(10)
            .with_target(prometheus.Dataquery().datasource(_MIMIR).expr(_SIZE).legend_format("Capacity").ref_id("A"))
            .with_target(
                prometheus.Dataquery().datasource(_MIMIR).expr(f"{_SIZE} - {_FREE}").legend_format("Used").ref_id("B")
            )
        )
        .with_panel(
            _filesystem_panel(2, "Guest root filesystem: used percent", 12)
            .unit("percent")
            .fill_opacity(20)
            .min(0)
            .max(100)
            .decimals(1)
            .with_target(
                prometheus.Dataquery()
                .datasource(_MIMIR)
                .expr(f"100 * (1 - {_FREE} / {_SIZE})")
                .legend_format("Used")
                .ref_id("A")
            )
        )
        .build()
    )
    # The classic builder has no schema-version setter; retain the deployed format.
    result.schema_version = 39
    return result


def main() -> None:
    print(JSONEncoder(indent=2).encode(build_dashboard()))


if __name__ == "__main__":
    main()
