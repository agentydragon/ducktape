"""Battery and RAPL telemetry authored with Grafana Foundation SDK builders."""

from grafana_foundation_sdk.builders import bargauge, table, timeseries
from grafana_foundation_sdk.builders.common import ReduceDataOptions, VizLegendOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard, QueryVariable, ThresholdsConfig, TimePicker
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir, mimir_query, mimir_table_query

_INSTANCE_QUERY = 'label_values(node_uname_info{nodename="rugged"}, instance)'
_CHARGE_QUERY = (
    'max by (instance, power_supply) (node_power_supply_capacity{instance=~"$instance",power_supply=~"BAT.*"})'
)


def _power_panel(id: int, title: str, x: int, y: int, w: int, calcs: list[str]) -> timeseries.Panel:
    return (
        line_panel()
        .id(id)
        .title(title)
        .grid_pos(dashboard.GridPos(h=8, w=w, x=x, y=y))
        .unit("watt")
        .line_interpolation(common.LineInterpolation.LINEAR)
        .legend(VizLegendOptions().display_mode(common.LegendDisplayMode.TABLE).show_legend(True).calcs(calcs))
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("Rugged Power")
        .uid("rugged-power")
        .description(
            "Laptop battery and RAPL telemetry for Rugged. Adapted from Grafana dashboard 12542; RAPL panels deliberately select the canonical intel-rapl path to exclude duplicate MMIO counters."
        )
        .tooltip(dashboard.DashboardCursorSync.CROSSHAIR)
        .tags(["laptop", "power", "rapl", "rugged"])
        .refresh("1m")
        .time("now-6h", "now")
        .timepicker(TimePicker().refresh_intervals(["10s", "30s", "1m", "5m", "15m", "1h"]))
        .with_variable(
            QueryVariable("instance")
            .label("Rugged instance")
            .datasource(mimir())
            .query(_INSTANCE_QUERY)
            .definition(_INSTANCE_QUERY)
            .refresh(dashboard.VariableRefresh.ON_DASHBOARD_LOAD)
            .sort(dashboard.VariableSort.ALPHABETICAL_ASC)
        )
        .with_panel(
            bargauge.Panel()
            .id(1)
            .title("Battery charge")
            .datasource(mimir())
            .grid_pos(dashboard.GridPos(h=8))
            .unit("percent")
            .min(0)
            .max(100)
            .thresholds(
                ThresholdsConfig().steps(
                    [
                        dashboard.Threshold(color="red"),
                        dashboard.Threshold(color="orange", value=20),
                        dashboard.Threshold(color="green", value=50),
                    ]
                )
            )
            .display_mode(common.BarGaugeDisplayMode.LCD)
            .orientation(common.VizOrientation.HORIZONTAL)
            .reduce_options(ReduceDataOptions().calcs(["lastNotNull"]).fields("").values(False))
            .with_target(mimir_query(_CHARGE_QUERY, "{{power_supply}}").instant())
        )
        .with_panel(
            table.Panel()
            .id(2)
            .title("Battery and external-power state")
            .datasource(mimir())
            .grid_pos(dashboard.GridPos(h=8, x=12))
            .with_target(mimir_table_query('node_power_supply_info{instance=~"$instance",type=~"Battery|Mains|USB"}'))
            .with_transformation(dashboard.DataTransformerConfig(id_val="labelsToFields", options={}))
        )
        .with_panel(
            _power_panel(3, "RAPL power draw", 0, 8, 24, ["lastNotNull", "mean", "max"])
            .with_target(
                mimir_query(
                    'max by (instance, index, path) (rate(node_rapl_package_joules_total{instance=~"$instance",path=~".*/intel-rapl:[^/]+$"}[5m]))',
                    "package",
                )
            )
            .with_target(
                mimir_query(
                    'max by (instance, index, path) (rate(node_rapl_dram_joules_total{instance=~"$instance",path=~".*/intel-rapl:[^/]+:[^/]+$"}[5m]))',
                    "DRAM",
                    "B",
                )
            )
            .with_target(
                mimir_query(
                    'max by (instance, index, path) (rate(node_rapl_psys_joules_total{instance=~"$instance",path=~".*/intel-rapl:[^/]+$"}[5m]))',
                    "platform",
                    "C",
                )
            )
        )
        .with_panel(
            _power_panel(
                4, "Battery electrical power (sign follows kernel current)", 0, 16, 12, ["lastNotNull"]
            ).with_target(
                mimir_query(
                    'max by (instance, power_supply) (node_power_supply_current_ampere{instance=~"$instance",power_supply=~"BAT.*"} * node_power_supply_voltage_volt{instance=~"$instance",power_supply=~"BAT.*"})',
                    "{{power_supply}}",
                )
            )
        )
        .with_panel(
            _power_panel(5, "Battery temperature", 12, 16, 12, ["lastNotNull", "max"])
            .unit("celsius")
            .with_target(
                mimir_query(
                    'max by (instance, power_supply) (node_power_supply_temp_celsius{instance=~"$instance",power_supply=~"BAT.*"})',
                    "{{power_supply}}",
                )
            )
        )
        .with_panel(
            line_panel()
            .id(6)
            .title("Battery charge over time")
            .grid_pos(dashboard.GridPos(h=8, w=24, x=0, y=24))
            .unit("percent")
            .min(0)
            .max(100)
            .legend(
                VizLegendOptions().display_mode(common.LegendDisplayMode.TABLE).show_legend(True).calcs(["lastNotNull"])
            )
            .with_target(mimir_query(_CHARGE_QUERY, "{{power_supply}}"))
        )
        .build()
    )
    result.schema_version = 39
    return result
