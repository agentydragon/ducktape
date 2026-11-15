"""Interface Flap Frequency dashboard."""

from grafana_foundation_sdk.models import common, dashboard, statetimeline, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import (
    loki_datasource,
    loki_query,
    mimir,
    mimir_query,
    series_options,
)


def _event_options() -> timeseries.Options:
    return timeseries.Options(
        legend=common.VizLegendOptions(
            display_mode=common.LegendDisplayMode.TABLE, calcs=["lastNotNull", "max", "sum"], show_legend=True
        )
    )


def _event_panel(
    id: int, title: str, message: str, legend: str, position: dashboard.GridPos, fill_opacity: int
) -> dashboard.Panel:
    return dashboard.Panel(
        type_val="timeseries",
        id_val=id,
        title=title,
        datasource=loki_datasource(),
        grid_pos=position,
        targets=[
            loki_query(
                f'sum(count_over_time({{job="systemd-journal",node=~"$node"}} |= "{message}" [5m]))', legend=legend
            ).build()
        ],
        options=_event_options(),
        field_config=dashboard.FieldConfigSource(
            defaults=dashboard.FieldConfig(
                unit="short",
                decimals=0,
                custom=common.GraphFieldConfig(
                    draw_style=common.GraphDrawStyle.BARS, line_width=1, fill_opacity=fill_opacity, span_nulls=True
                ),
            )
        ),
    )


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="interface-flap-frequency",
        title="Interface Flap Frequency",
        description="Host-journal interface lifecycle activity from Loki, with node-exporter carrier state as a secondary signal. A veth bring-up/teardown is usually container or pod lifecycle churn, not a physical NIC fault.",
        tags=["network", "interfaces", "flaps", "journal", "loki"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        timepicker=dashboard.TimePickerConfig(refresh_intervals=["30s", "1m", "5m", "15m", "1h"]),
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            *[
                _event_panel(id, title, message, legend, position, fill_opacity)
                for id, title, message, legend, position, fill_opacity in [
                    (
                        1,
                        "Veth bring-up events per 5m",
                        "entered forwarding state",
                        "veth bring-up events",
                        dashboard.GridPos(h=8),
                        80,
                    ),
                    (
                        2,
                        "Veth teardown events per 5m",
                        "unregistering",
                        "veth teardown events",
                        dashboard.GridPos(h=8, x=12),
                        80,
                    ),
                    (
                        3,
                        "CNI interface recreations per 5m",
                        "eth0: renamed from tmp",
                        "CNI interface recreations",
                        dashboard.GridPos(h=8, w=8, y=8),
                        60,
                    ),
                    (
                        4,
                        "KubeVirt tap0 events per 5m",
                        "tap0",
                        "tap0 events",
                        dashboard.GridPos(h=8, w=8, x=8, y=8),
                        60,
                    ),
                ]
            ],
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    mimir_query(
                        'sum by (instance) (rate(node_network_carrier_changes_total{instance=~"$instance",device=~"$interface_scope",device=~"$device"}[$__rate_interval])) * 3600',
                        legend="{{instance}}",
                    ).build()
                ],
                title="Observed carrier changes per hour",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=8),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull", "max")),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        decimals=2,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=1,
                            line_interpolation=common.LineInterpolation.LINEAR,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            span_nulls=True,
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=6,
                targets=[
                    mimir_query(
                        'node_network_carrier{instance=~"$instance",device=~"$interface_scope",device=~"$device"}',
                        legend="{{instance}} / {{device}}",
                    ).build()
                ],
                title="Current carrier state (physical by default)",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=16),
                options=statetimeline.Options(
                    per_page=None,
                    show_value=common.VisibilityMode.NEVER,
                    row_height=0.7,
                    align_value=common.TimelineValueAlignment.CENTER,
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bool",
                        min_val=0,
                        max_val=1,
                        mappings=[
                            dashboard.ValueMap(
                                options={"0": dashboard.ValueMappingResult(text="No carrier", color="red")}
                            ),
                            dashboard.ValueMap(
                                options={"1": dashboard.ValueMappingResult(text="Carrier", color="green")}
                            ),
                        ],
                        custom=statetimeline.FieldConfig(line_width=1, fill_opacity=30, span_nulls=False),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="logs",
                id_val=7,
                targets=[
                    loki_query(
                        '{job="systemd-journal",node=~"$node"} |~ "veth|tap[0-9]+|eth0: renamed from tmp"'
                    ).build()
                ],
                title="Recent interface lifecycle journal",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=26),
                # These legacy log options have no SDK model fields.
                options={
                    "deduplication": "none",
                    "enableLogDetails": True,
                    "prettifyLogMessage": False,
                    "showCommonLabels": False,
                    "showLabels": False,
                    "sortOrder": "Descending",
                    "wrapLines": False,
                },
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="node",
                    label="Node",
                    query='label_values({job="systemd-journal"}, node)',
                    datasource=loki_datasource(),
                    current=dashboard.VariableOption(text="wyrm2", value="wyrm2"),
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    definition='label_values({job="systemd-journal"}, node)',
                ),
                dashboard.VariableModel(
                    name="instance",
                    label="Instance",
                    hide=dashboard.VariableHide.HIDE_VARIABLE,
                    query='label_values(node_uname_info{nodename=~"$node"}, instance)',
                    datasource=mimir(),
                    current=dashboard.VariableOption(),
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition='label_values(node_uname_info{nodename=~"$node"}, instance)',
                ),
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="interface_scope",
                    label="Interface scope",
                    query="Physical : ^(eno|enp|ens|enx|eth|em|wl|wwan|bond).*,All : .*",
                    current=dashboard.VariableOption(text="Physical", value="^(eno|enp|ens|enx|eth|em|wl|wwan|bond).*"),
                    options=[
                        dashboard.VariableOption(
                            selected=True, text="Physical", value="^(eno|enp|ens|enx|eth|em|wl|wwan|bond).*"
                        ),
                        dashboard.VariableOption(selected=False, text="All", value=".*"),
                    ],
                ),
                dashboard.VariableModel(
                    name="device",
                    label="Interface",
                    query='label_values(node_network_carrier{instance=~"$instance",device=~"$interface_scope"}, device)',
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value=".*",
                    definition='label_values(node_network_carrier{instance=~"$instance",device=~"$interface_scope"}, device)',
                ),
            ]
        ),
    )
