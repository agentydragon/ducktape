"""Home gateway dashboard."""

from grafana_foundation_sdk.models import common, dashboard, logs, loki, prometheus, stat, statetimeline

from cluster.cdk8s.monitoring.dashboards.foundation import last_value, loki_datasource, mimir, mimir_query


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
                targets=[
                    mimir_query("att_gateway_wan_up", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="WAN",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
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
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query("att_gateway_optical_up", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Fiber link",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=4),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
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
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[
                    mimir_query("att_gateway_pon_state", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="ONU state",
                description="ITU-T G.984/G.9807 ONU state; only O5 (Operation) carries traffic.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=8),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
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
                targets=[
                    mimir_query("att_gateway_uptime_seconds", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Uptime",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=12),
                options=stat.Options(graph_mode=common.BigValueGraphMode.NONE, reduce_options=last_value()),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="s")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=5,
                targets=[
                    mimir_query("att_gateway_optical_rx_power_dbm", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Fiber Rx power",
                description="Gateway warns below -28.5 dBm and alarms below -30 dBm.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=16),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
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
                targets=[
                    mimir_query("min(att_gateway_scrape_success)", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Gateway reachable",
                description="Every status page fetched and parsed on the exporter's last poll.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=20),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
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
                    )
                ),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=7,
                targets=[
                    mimir_query("att_gateway_wan_up", legend="WAN")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_optical_up", legend="Fiber", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_pon_state == bool 5", legend="ONU O5", ref_id="C")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("min(att_gateway_scrape_success)", legend="Gateway reachable", ref_id="D")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
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
                field_config=dashboard.FieldConfigSource(
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
                        custom=statetimeline.FieldConfig(fill_opacity=80),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=8,
                targets=[
                    mimir_query("rate(att_gateway_wan_receive_bytes_total[$__rate_interval]) * 8", legend="Download")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "rate(att_gateway_wan_transmit_bytes_total[$__rate_interval]) * 8", legend="Upload", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
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
                    mimir_query("att_gateway_optical_rx_power_dbm", legend="Rx power")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'att_gateway_optical_rx_power_dbm_threshold{bound="low"}', legend="{{level}} (low)", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Fiber Rx power vs gateway thresholds",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=10),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="dBm", decimals=1)),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[
                    mimir_query("att_gateway_uptime_seconds", legend="Uptime")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
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
                    mimir_query("att_gateway_optical_tx_power_dbm", legend="Tx power (dBm)")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_optical_temperature_celsius", legend="Temperature (°C)", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Fiber Tx power and temperature",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=18),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=12,
                targets=[
                    mimir_query(
                        "increase(att_gateway_lan_port_receive_errors_total[$__rate_interval])",
                        legend="Port {{port}} rx errors",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_receive_drops_total[$__rate_interval])",
                        legend="Port {{port}} rx drops",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_transmit_errors_total[$__rate_interval])",
                        legend="Port {{port}} tx errors",
                        ref_id="C",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_transmit_drops_total[$__rate_interval])",
                        legend="Port {{port}} tx drops",
                        ref_id="D",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="LAN port errors and drops",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=26),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[
                    mimir_query("increase(att_gateway_wan_receive_errors_total[$__rate_interval])", legend="rx errors")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_wan_receive_drops_total[$__rate_interval])", legend="rx drops", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_wan_transmit_errors_total[$__rate_interval])",
                        legend="tx errors",
                        ref_id="C",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_wan_transmit_drops_total[$__rate_interval])",
                        legend="tx drops",
                        ref_id="D",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="WAN errors and drops",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=26),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    mimir_query("att_gateway_nat_sessions_in_use", legend="All")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_nat_ipv4_sessions", legend="{{source}}", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
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
                targets=[
                    mimir_query("att_gateway_speedtest_throughput_bps", legend="{{direction}}")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
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
