"""GPU telemetry dashboard authored with Grafana Foundation SDK builders."""

from grafana_foundation_sdk.builders import gauge
from grafana_foundation_sdk.builders.common import ReduceDataOptions, VizLegendOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.builders.timeseries import Panel as TimeSeriesPanel
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir, mimir_multi_query_variable, mimir_query

_INSTANCE_QUERY = "label_values(DCGM_FI_DEV_GPU_TEMP, instance)"
_GPU_QUERY = 'label_values(DCGM_FI_DEV_GPU_TEMP{instance=~"$instance"}, gpu)'
_GPU_FILTER = 'instance=~"$instance",gpu=~"$gpu"'


def _gpu_timeseries(
    panel_id: int, title: str, expression: str, legend: str, unit: str, position: dashboard.GridPos
) -> TimeSeriesPanel:
    return (
        line_panel()
        .id(panel_id)
        .title(title)
        .grid_pos(position)
        .unit(unit)
        .legend(VizLegendOptions().display_mode(common.LegendDisplayMode.TABLE).show_legend(True))
        .with_target(mimir_query(expression, legend))
    )


def _gpu_gauge(panel_id: int, title: str, expression: str, unit: str, position: dashboard.GridPos) -> gauge.Panel:
    return (
        gauge.Panel()
        .id(panel_id)
        .title(title)
        .description("Current value across the selected GPUs.")
        .datasource(mimir())
        .grid_pos(position)
        .unit(unit)
        .min(0)
        .reduce_options(ReduceDataOptions().calcs(["lastNotNull"]).fields("").values(False))
        .with_target(mimir_query(expression).instant())
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("NVIDIA DCGM Exporter Dashboard")
        .uid("Oxed_c6Wz")
        .description(
            "GPU temperature, power, clocks, utilization, and framebuffer use from DCGM Exporter. "
            "GPU power and average temperature deduplicate overlapping exporter targets by physical GPU UUID. "
            "Framebuffer use is converted from DCGM's MiB to bytes. Tensor Core utilization is omitted because "
            "the cluster does not expose that optional metric."
        )
        .tags(["gpu", "nvidia", "dcgm", "kubernetes"])
        .version(1)
        .refresh("1m")
        .time("now-6h", "now")
        .with_variable(mimir_multi_query_variable("instance", "Instance", _INSTANCE_QUERY))
        .with_variable(mimir_multi_query_variable("gpu", "GPU", _GPU_QUERY))
        .with_panel(
            _gpu_gauge(
                2,
                "GPU Power Total",
                f"sum(max by (UUID) (DCGM_FI_DEV_POWER_USAGE{{{_GPU_FILTER}}}))",
                "watt",
                dashboard.GridPos(h=8, w=12),
            )
        )
        .with_panel(
            _gpu_gauge(
                3,
                "GPU Avg. Temp",
                f"avg(max by (UUID) (DCGM_FI_DEV_GPU_TEMP{{{_GPU_FILTER}}}))",
                "celsius",
                dashboard.GridPos(h=8, w=12, x=12),
            )
        )
        .with_panel(
            _gpu_timeseries(
                1,
                "GPU Temperature",
                f"max by (UUID, gpu) (DCGM_FI_DEV_GPU_TEMP{{{_GPU_FILTER}}})",
                "GPU {{gpu}} ({{UUID}})",
                "celsius",
                dashboard.GridPos(h=8, w=12, y=8),
            )
        )
        .with_panel(
            _gpu_timeseries(
                4,
                "GPU Power Usage",
                f"max by (UUID, gpu) (DCGM_FI_DEV_POWER_USAGE{{{_GPU_FILTER}}})",
                "GPU {{gpu}} ({{UUID}})",
                "watt",
                dashboard.GridPos(h=8, w=12, x=12, y=8),
            )
        )
        .with_panel(
            _gpu_timeseries(
                5,
                "GPU SM Clocks",
                f"max by (UUID, gpu) (DCGM_FI_DEV_SM_CLOCK{{{_GPU_FILTER}}}) * 1000000",
                "GPU {{gpu}} ({{UUID}})",
                "hertz",
                dashboard.GridPos(h=8, w=12, y=16),
            )
        )
        .with_panel(
            _gpu_timeseries(
                6,
                "GPU Utilization",
                f"max by (UUID, gpu) (DCGM_FI_DEV_GPU_UTIL{{{_GPU_FILTER}}})",
                "GPU {{gpu}} ({{UUID}})",
                "percent",
                dashboard.GridPos(h=8, w=12, x=12, y=16),
            )
        )
        .with_panel(
            _gpu_timeseries(
                7,
                "GPU Framebuffer Mem Used",
                f"max by (UUID, gpu) (DCGM_FI_DEV_FB_USED{{{_GPU_FILTER}}}) * 1024 * 1024",
                "GPU {{gpu}} ({{UUID}})",
                "bytes",
                dashboard.GridPos(h=8, w=24, y=24),
            )
        )
        .build()
    )
    result.schema_version = 39
    return result
