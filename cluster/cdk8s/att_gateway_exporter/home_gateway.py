"""Home gateway dashboard."""

from grafana_foundation_sdk.models import common, dashboard, logs, loki, statetimeline

from cluster.cdk8s.monitoring.dashboards.foundation import (
    gateway_error_queries,
    loki_datasource,
    mimir,
    mimir_query,
    stat_options,
    up_down_fields,
)


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="home-gateway",
        title="Home gateway",
        tags=["home-network", "att-gateway"],
        editable=False,
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="1m",
        schema_version=41,
        panels=[
            dashboard.Panel(
                type_val="stat",
                id_val=1,
                targets=[mimir_query("att_gateway_wan_up", legend="").build()],
                title="WAN",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[mimir_query("att_gateway_optical_up", legend="").build()],
                title="Fiber link",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=4),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[mimir_query("att_gateway_pon_state", legend="").build()],
                title="ONU state",
                description="ITU-T G.984/G.9807 ONU state; only O5 (Operation) carries traffic.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=8),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        mappings=[
                            dashboard.ValueMap(
                                options={"5": dashboard.ValueMappingResult(text="O5 Operation", color="green")}
                            ),
                            dashboard.RangeMap(
                                options=dashboard.DashboardRangeMapOptions(
                                    from_val=1, to=4, result=dashboard.ValueMappingResult(text="Ranging", color="red")
                                )
                            ),
                        ],
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="red"), dashboard.Threshold(value=5, color="green")]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=4,
                targets=[mimir_query("att_gateway_uptime_seconds", legend="").build()],
                title="Uptime",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=12),
                options=stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="orange"), dashboard.Threshold(value=3600, color="green")]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=5,
                targets=[mimir_query("att_gateway_optical_rx_power_dbm", legend="").build()],
                title="Fiber Rx power",
                description="Gateway warns below -28.5 dBm and alarms below -30 dBm.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=16),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="dBm",
                        decimals=1,
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[
                                dashboard.Threshold(color="red"),
                                dashboard.Threshold(value=-28.5, color="yellow"),
                                dashboard.Threshold(value=-25, color="green"),
                            ]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=6,
                targets=[mimir_query("min(att_gateway_scrape_success)", legend="").build()],
                title="Gateway reachable",
                description="Every status page fetched and parsed on the exporter's last poll.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=20),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=7,
                targets=[
                    mimir_query(expr, legend=legend, ref_id=chr(ord("A") + index)).build()
                    for index, (expr, legend) in enumerate(
                        (
                            ("att_gateway_wan_up", "WAN"),
                            ("att_gateway_optical_up", "Fiber"),
                            ("att_gateway_pon_state == bool 5", "ONU O5"),
                            ("min(att_gateway_scrape_success)", "Gateway reachable"),
                        )
                    )
                ],
                title="Link state",
                description="Gaps mean no data reached Mimir for that time.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=24, y=4),
                options=statetimeline.Options(
                    per_page=None,
                    show_value=common.VisibilityMode.NEVER,
                    row_height=0.8,
                    tooltip=common.VizTooltipOptions(sort=common.SortOrder.NONE),
                ),
                field_config=up_down_fields(timeline=True),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query("rate(att_gateway_wan_receive_bytes_total[5m]) * 8", legend="Download").build(),
                    mimir_query(
                        "rate(att_gateway_wan_transmit_bytes_total[5m]) * 8", legend="Upload", ref_id="B"
                    ).build(),
                ],
                title="WAN throughput",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=10),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bps")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query("att_gateway_optical_rx_power_dbm", legend="Rx power").build(),
                    mimir_query(
                        'att_gateway_optical_rx_power_dbm_threshold{bound="low"}', legend="{{level}} (low)", ref_id="B"
                    ).build(),
                ],
                title="Fiber Rx power vs gateway thresholds",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=10),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="dBm", decimals=1)),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[mimir_query("att_gateway_uptime_seconds", legend="Uptime").build()],
                title="Gateway uptime",
                description="Each drop to zero is a gateway reboot.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=18),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="s")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[
                    mimir_query("att_gateway_optical_tx_power_dbm", legend="Tx power (dBm)").build(),
                    mimir_query(
                        "att_gateway_optical_temperature_celsius", legend="Temperature (°C)", ref_id="B"
                    ).build(),
                ],
                title="Fiber Tx power and temperature",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=18),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=12,
                targets=gateway_error_queries("lan_port", legend_prefix="Port {{port}} "),
                title="LAN port errors and drops",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=26),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=gateway_error_queries("wan"),
                title="WAN errors and drops",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=26),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    mimir_query("att_gateway_nat_sessions_in_use", legend="All").build(),
                    mimir_query("att_gateway_nat_ipv4_sessions", legend="{{source}}", ref_id="B").build(),
                ],
                title="NAT sessions",
                description="Entries in the gateway's session table, of att_gateway_nat_sessions_available; IPv4 entries per source address.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=34),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="short")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[mimir_query("att_gateway_speedtest_throughput_bps", legend="{{direction}}").build()],
                title="Gateway speed tests",
                description="The gateway's own latest speed test to AT&T's server: line capacity, independent of home traffic.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=34),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bps")),
            ),
            dashboard.Panel(
                type_val="logs",
                id_val=16,
                targets=[
                    loki.Dataquery(
                        expr='{job="home-gateway"}',
                        editor_mode=loki.QueryEditorMode.CODE,
                        ref_id="A",
                        query_type="range",
                        datasource=loki_datasource(),
                    )
                ],
                title="Firewall log",
                description="The gateway's firewall log, sent by syslog and set by the att-gateway-exporter-syslog CronJob.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=42),
                options=logs.Options(show_time=True, wrap_log_message=True, enable_log_details=True),
            ),
        ],
    )
