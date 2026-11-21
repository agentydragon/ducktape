"""The CPAP gateway guest filesystem dashboard (Grafana Foundation SDK, classic dashboard format)."""

from grafana_foundation_sdk.builders import timeseries
from grafana_foundation_sdk.builders.common import VizLegendOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir_query

_SIZE = 'max by (mountpoint) (node_filesystem_size_bytes{namespace="cpap-sync",service="cpap-gateway-metrics",endpoint="node-metrics",mountpoint="/"})'
_FREE = 'max by (mountpoint) (node_filesystem_free_bytes{namespace="cpap-sync",service="cpap-gateway-metrics",endpoint="node-metrics",mountpoint="/"})'


def _filesystem_panel(id: int, title: str, x: int) -> timeseries.Panel:
    return (
        line_panel()
        .id(id)
        .title(title)
        .description(
            "Guest root filesystem. Used bytes are size minus free; root-reserved blocks are included in free. Capacity is the filesystem visible inside the VM, separate from KubeVirt and host storage. Only the CPAP gateway guest filesystem collector is scraped by monitoring Alloy."
        )
        .grid_pos(dashboard.GridPos(h=8, x=x))
        .line_interpolation(common.LineInterpolation.LINEAR)
        .legend(
            VizLegendOptions().display_mode(common.LegendDisplayMode.TABLE).show_legend(True).calcs(["lastNotNull"])
        )
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("CPAP gateway disk")
        .uid("cpap-gateway-disk")
        .description(
            "Guest root filesystem capacity and use for the stateless CPAP gateway VM. The current guest has no separate /boot filesystem mount, so boot-partition usage is not exposed here. This does not measure OptiPlex host storage or Kubernetes pod storage."
        )
        .tooltip(dashboard.DashboardCursorSync.CROSSHAIR)
        .tags(["cpap", "kubevirt", "storage"])
        .refresh("1m")
        .time("now-24h", "now")
        .version(1)
        .with_panel(
            _filesystem_panel(1, "Guest root filesystem: used and capacity", 0)
            .unit("bytes")
            .with_target(mimir_query(_SIZE, "Capacity"))
            .with_target(mimir_query(f"{_SIZE} - {_FREE}", "Used", "B"))
        )
        .with_panel(
            _filesystem_panel(2, "Guest root filesystem: used percent", 12)
            .unit("percent")
            .fill_opacity(20)
            .min(0)
            .max(100)
            .decimals(1)
            .with_target(mimir_query(f"100 * (1 - {_FREE} / {_SIZE})", "Used"))
        )
        .build()
    )
    result.schema_version = 39
    return result
