"""Home network overview dashboard."""

from grafana_foundation_sdk.models import common, dashboard, logs, loki, prometheus, stat, statetimeline

from cluster.cdk8s.monitoring.dashboards.foundation import (
    last_value,
    line_style,
    loki_datasource,
    mimir,
    mimir_query,
    series_options,
)


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="home-network-overview",
        title="Home network overview",
        description="Home connectivity at a glance: AT&T gateway, home nodes, and how metrics are buffered through outages.",
        tags=["home-network"],
        editable=False,
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="1m",
        schema_version=41,
        panels=[
            dashboard.RowPanel(title="Right now", grid_pos=dashboard.GridPos(h=1, w=24), id_val=1),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query(
                        'min(kube_node_status_condition{condition="Ready",status="true",node=~"wyrm2|optiplex"})',
                        legend="",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Home seen from OVH",
                description="Both home nodes report Ready to the API server in OVH. Computed by kube-state-metrics in OVH, so this goes Down live during a home outage, unlike the series scraped at home.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, y=1),
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
                    mimir_query("att_gateway_wan_up", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="WAN",
                description="The gateway's WAN link (broadband status page).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=4, y=1),
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
                id_val=4,
                targets=[
                    mimir_query("att_gateway_optical_up", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Fiber",
                description="The gateway's optical link (fiber status page).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=8, y=1),
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
                id_val=5,
                targets=[
                    mimir_query("att_gateway_uptime_seconds", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Gateway uptime",
                description="Time since the gateway last booted.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=12, y=1),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="orange"), dashboard.Threshold(value=3600, color="green")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=6,
                targets=[
                    mimir_query("resets(att_gateway_uptime_seconds[7d])", legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Gateway reboots (7d)",
                description="Times the gateway's uptime went back to zero in the last 7 days.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=16, y=1),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        decimals=0,
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=1, color="orange")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=7,
                targets=[
                    mimir_query(
                        'max(max by (node) (clamp_min(max by (pod) (prometheus_remote_storage_highest_timestamp_in_seconds{job="alloy-node"}) - max by (pod) (prometheus_remote_storage_queue_highest_sent_timestamp_seconds{job="alloy-node"}), 0) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"})))',
                        legend="",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Home metrics backlog",
                description="Largest delay between the newest sample a home Alloy has collected and the newest it has delivered to Mimir. Grows while home is cut off, drains after it reconnects.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=20, y=1),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=300, color="orange")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.RowPanel(title="Connectivity", grid_pos=dashboard.GridPos(h=1, w=24, y=5), id_val=8),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=9,
                targets=[
                    mimir_query("min(att_gateway_wan_up)", legend="Gateway WAN")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("min(att_gateway_optical_up)", legend="Gateway fiber", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("min(att_gateway_scrape_success)", legend="Gateway status pages readable", ref_id="C")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'max by (node) (kube_node_status_condition{condition="Ready",status="true",node=~"wyrm2|optiplex"})',
                        legend="{{node}} Ready (seen from OVH)",
                        ref_id="D",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'up{job="kubelet",metrics_path="/metrics",node=~"wyrm2|optiplex"}',
                        legend="{{node}} kubelet scraped",
                        ref_id="E",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Home connectivity timeline",
                description="Each row is one link or node over time. Gateway link queries combine exporter pod identities so replacements do not add rows; overlapping reports show Down if either reports Down. A blank stretch means Mimir has no samples for it: home was unreachable and the data was not (or not yet) replayed from a home buffer. The OVH-side Ready rows go Down during an outage; the rows scraped at home show what home itself saw once their buffer is replayed.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=6),
                options=statetimeline.Options(
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
                        color=dashboard.FieldColor(),
                        custom=statetimeline.FieldConfig(fill_opacity=80),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[
                    mimir_query(
                        "rate(att_gateway_wan_receive_bytes_total[5m]) * 8", legend="download (${traffic_scale})"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "rate(att_gateway_wan_transmit_bytes_total[5m]) * 8",
                        legend="upload (${traffic_scale})",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Internet throughput (gateway WAN)",
                description="Traffic through the gateway's WAN port. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=14),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bps",
                        min_val=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            scale_distribution=common.ScaleDistributionConfig(),
                            span_nulls=False,
                        ),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options="/ [(]Log[)]$/"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.scaleDistribution",
                                    value={"type": "symlog", "log": 10, "linearThreshold": 1},
                                )
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[
                    mimir_query(
                        'rate(node_network_receive_bytes_total{job="node-exporter",device=~"nebula.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} in (${traffic_scale})",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'rate(node_network_transmit_bytes_total{job="node-exporter",device=~"nebula.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} out (${traffic_scale})",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Home to OVH tunnel (Nebula)",
                description="Traffic on each home node's Nebula interface, which carries all of its cluster traffic to OVH, including metrics remote-write. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=14),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bps",
                        min_val=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            scale_distribution=common.ScaleDistributionConfig(),
                            span_nulls=False,
                        ),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options="/ [(]Log[)]$/"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.scaleDistribution",
                                    value={"type": "symlog", "log": 10, "linearThreshold": 1},
                                )
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=50,
                targets=[
                    mimir_query(
                        'max(mktxp_interface_rate{routerboard_name="CRS310",name="ether1"}) * 1000000',
                        legend="CRS310 ether1 to AT&T gateway",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="AT&T gateway uplink negotiated speed",
                description="Actual negotiated Ethernet speed between CRS310 ether1 and the AT&T BGW320 gateway, sampled every 30 seconds. Compare its transitions with the independent host ping-loss panels beside it.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, y=22),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bps",
                        decimals=1,
                        min_val=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            line_interpolation=common.LineInterpolation.STEP_AFTER,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            axis_label="Negotiated speed",
                            span_nulls=False,
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=43,
                targets=[
                    mimir_query(
                        '1 - sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node=~"optiplex|"}[5m])) / sum by (target) (rate(smokeping_requests_total{job="monitoring/smokeping",source_node=~"optiplex|"}[5m]))',
                        legend="{{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Ping loss from optiplex",
                description="Share of pings from optiplex (5/s per target) that got no reply. Loss to the gateway with none to the switch is the uplink fault in debug/home_network_connectivity/2026_10_gateway_uplink_loss.md.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=22),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=45,
                targets=[
                    mimir_query(
                        '1 - sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node="wyrm2"}[5m])) / sum by (target) (rate(smokeping_requests_total{job="monitoring/smokeping",source_node="wyrm2"}[5m]))',
                        legend="{{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Ping loss from wyrm2",
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Loss is the unanswered fraction of 5 pings/s, averaged over 5 minutes. Compare with optiplex beside it; a gap means missing observations.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=22),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="optiplex"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="OptiPlex LAN (192.168.1.10)")
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=44,
                targets=[
                    mimir_query(
                        'sum by (target) (rate(smokeping_response_duration_seconds_sum{job="monitoring/smokeping",source_node=~"optiplex|"}[5m])) / sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node=~"optiplex|"}[5m]))',
                        legend="{{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Ping RTT from optiplex",
                description="Mean round-trip time of answered pings from optiplex, per target.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=30),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=46,
                targets=[
                    mimir_query(
                        'sum by (target) (rate(smokeping_response_duration_seconds_sum{job="monitoring/smokeping",source_node="wyrm2"}[5m])) / sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node="wyrm2"}[5m]))',
                        legend="{{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Ping RTT from wyrm2",
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Mean round-trip time of answered pings, per target.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=30),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="optiplex"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="OptiPlex LAN (192.168.1.10)")
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=47,
                targets=[
                    mimir_query(
                        'max by (name) (mktxp_netwatch_icmp_loss_percent{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}) / 100',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="UNRELIABLE: Ping batch loss from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Latest reported Netwatch batch, sent by the CRS310 itself: 200 pings at 150 ms intervals once per minute, with a 1 s reply timeout and no early termination. Unlike host smokeping, this is batch loss, not a rolling 5-minute ratio. Scraped every 30 seconds; gaps cannot reconstruct missed batches.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=38),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="att-gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="optiplex-lan"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="OptiPlex LAN (192.168.1.10)")
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=48,
                targets=[
                    mimir_query(
                        'max by (name) (mktxp_netwatch_icmp_rtt_avg_ms{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}) / 1000',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="UNRELIABLE: Ping batch RTT from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Mean RTT of answered pings in the latest reported switch-originated Netwatch batch. 200 pings at 150 ms intervals, once per minute; scraped every 30 seconds. A zero RTT during 100% loss does not mean a fast response.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=38),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="att-gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="optiplex-lan"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="OptiPlex LAN (192.168.1.10)")
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=49,
                targets=[
                    mimir_query(
                        'max by (name) (increase(mktxp_netwatch_failed_tests{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}[1h]))',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="UNRELIABLE: Failed probe batches per hour from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Estimated failed batches in the trailing hour, from reset-aware increase of RouterOS cumulative failed-tests (exported as a gauge by mktxp). Each batch tests 200 pings once per minute; a 5% loss threshold and 1 s RTT thresholds determine failure. This measures failed batches, not distinct outage episodes. Counts can survive missed scrapes unless a probe resets; increase may produce fractional estimates.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=46),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", decimals=1, min_val=0, custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="att-gateway"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="AT&T gateway (192.168.1.254)")
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="optiplex-lan"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="OptiPlex LAN (192.168.1.10)")
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=51,
                targets=[
                    mimir_query(
                        'sum_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m])',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Affected probe minutes per trailing hour",
                description="Number of one-minute classifications with at least 1% unanswered host ICMP pings. This is measured affected time, up to 60 minutes per hour, per source and target. A minute is classified only with fresh, reset-free request and reply counters and roughly 5 requests/s. Missing observations are excluded; compare the coverage panel. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=54),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="m",
                        min_val=0,
                        max_val=60,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            line_interpolation=common.LineInterpolation.STEP_AFTER,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            span_nulls=False,
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=52,
                targets=[
                    mimir_query(
                        'sum_over_time((delta((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[2m:1m]) > bool 0)[1h:1m]) or on (source_node, target) (0 * count_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m]))',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Observed episode starts per trailing hour",
                description="An episode starts when an observed healthy minute (under 1% loss) is followed immediately by an affected minute (at least 1% loss). A return from a telemetry gap is left-censored and does not start a new counted episode. Zero requires at least one observed minute. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=54),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="none",
                        min_val=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            line_interpolation=common.LineInterpolation.STEP_AFTER,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            span_nulls=False,
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=53,
                targets=[
                    mimir_query(
                        'count_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m])',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Observed probe minutes per trailing hour",
                description="Coverage for affected-time and episode counts. Each source/target can contribute at most 60 classified minutes per trailing hour. Missing scrapes, stopped probes, and counter resets reduce coverage; low coverage prevents a claim of reliable delivery. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=62),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="m",
                        min_val=0,
                        max_val=60,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            line_interpolation=common.LineInterpolation.STEP_AFTER,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            span_nulls=False,
                        ),
                    )
                ),
            ),
            dashboard.RowPanel(title="Gateway", grid_pos=dashboard.GridPos(h=1, w=24, y=70), id_val=12),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[
                    mimir_query("att_gateway_optical_rx_power_dbm", legend="Rx power")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "att_gateway_optical_rx_power_dbm_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Fiber Rx power",
                description="Optical power the gateway receives from the fiber, against the gateway's own warning and alarm bounds. A slow drift toward the low bound points at the fiber or a connector.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, y=71),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="dBm", custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options=".*(warning|alarm).*"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.lineStyle", value={"dash": [10, 10], "fill": "dash"}
                                ),
                                dashboard.DynamicConfigValue(id_val="custom.fillOpacity", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.lineWidth", value=1),
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    mimir_query("att_gateway_optical_tx_power_dbm", legend="Tx power")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "att_gateway_optical_tx_power_dbm_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Fiber Tx power",
                description="Optical power the gateway transmits, against its warning and alarm bounds.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=71),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="dBm", custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options=".*(warning|alarm).*"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.lineStyle", value={"dash": [10, 10], "fill": "dash"}
                                ),
                                dashboard.DynamicConfigValue(id_val="custom.fillOpacity", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.lineWidth", value=1),
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    mimir_query("att_gateway_optical_temperature_celsius", legend="Temperature")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "att_gateway_optical_temperature_celsius_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Optics temperature",
                description="Temperature of the gateway's optical module, against its warning and alarm bounds.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=71),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="celsius", custom=line_style()),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options=".*(warning|alarm).*"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.lineStyle", value={"dash": [10, 10], "fill": "dash"}
                                ),
                                dashboard.DynamicConfigValue(id_val="custom.fillOpacity", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.lineWidth", value=1),
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=16,
                targets=[
                    mimir_query("increase(att_gateway_wan_receive_errors_total[5m])", legend="rx errors")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("increase(att_gateway_wan_receive_drops_total[5m])", legend="rx drops", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("increase(att_gateway_wan_transmit_errors_total[5m])", legend="tx errors", ref_id="C")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("increase(att_gateway_wan_transmit_drops_total[5m])", legend="tx drops", ref_id="D")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Gateway WAN errors and drops",
                description="Errors and drops on the gateway's WAN interface, per 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, y=79),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=17,
                targets=[
                    mimir_query(
                        "increase(att_gateway_lan_port_receive_errors_total[5m])", legend="port {{port}} rx errors"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_receive_drops_total[5m])",
                        legend="port {{port}} rx drops",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_transmit_errors_total[5m])",
                        legend="port {{port}} tx errors",
                        ref_id="C",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        "increase(att_gateway_lan_port_transmit_drops_total[5m])",
                        legend="port {{port}} tx drops",
                        ref_id="D",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Gateway LAN port errors and drops",
                description="Errors and drops on each gateway LAN port, per 5 minutes. Port 1 is the uplink to the home switch.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=6, y=79),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=18,
                targets=[
                    mimir_query("att_gateway_lan_port_up", legend="Port {{port}}")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Gateway LAN ports",
                description="Link state of each gateway LAN port. Port 1 is the uplink to the home switch.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=79),
                options=statetimeline.Options(
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
                        color=dashboard.FieldColor(),
                        custom=statetimeline.FieldConfig(fill_opacity=80),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=19,
                targets=[
                    mimir_query("att_gateway_dhcp_leases_allocated", legend="allocated")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_dhcp_leases_available", legend="available", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Gateway DHCP leases",
                description="Leases the gateway has handed out, and how many it has left.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=79),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=20,
                targets=[
                    mimir_query("att_gateway_nat_sessions_in_use", legend="in use")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query("att_gateway_nat_sessions_available", legend="capacity", ref_id="B")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Gateway NAT sessions",
                description="Entries in the gateway's NAT session table against its capacity. A full table drops new connections.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, y=87),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=21,
                targets=[
                    mimir_query("att_gateway_speedtest_throughput_bps", legend="{{direction}}")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Speed test throughput",
                description="Result of the gateway's latest successful speed test to AT&T, protocol overhead included.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=12, y=87),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.NONE,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bps")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=22,
                targets=[
                    mimir_query("att_gateway_speedtest_latency_seconds", legend="{{direction}}")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Speed test latency",
                description="Average round-trip latency during the latest successful speed test.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=16, y=87),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.NONE,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="s")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=23,
                targets=[
                    mimir_query("att_gateway_speedtest_completed_timestamp_seconds * 1000", legend="{{direction}}")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Speed test ran",
                description="When the latest successful speed test in each direction completed.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=20, y=87),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.NONE,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="dateTimeFromNow")),
            ),
            dashboard.RowPanel(title="Switch (CRS310)", grid_pos=dashboard.GridPos(h=1, w=24, y=93), id_val=24),
            dashboard.Panel(
                type_val="stat",
                id_val=25,
                targets=[
                    mimir_query(
                        '(max(mktxp_system_uptime{routerboard_name="CRS310"}) > bool 0) or on () vector(0)', legend=""
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch reachable",
                description="The switch answers the exporter's API queries. mktxp keeps serving its own metrics while the switch is unreachable, so this checks for switch data rather than the scrape.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, y=94),
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
                id_val=26,
                targets=[
                    mimir_query('max(mktxp_system_uptime{routerboard_name="CRS310"})', legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch uptime",
                description="Time since the switch last booted.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=6, y=94),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="orange"), dashboard.Threshold(value=3600, color="green")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=27,
                targets=[
                    mimir_query('max(mktxp_system_cpu_load{routerboard_name="CRS310"})', legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch CPU load",
                description="",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=12, y=94),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="percent",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=80, color="orange")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=28,
                targets=[
                    mimir_query('max(mktxp_system_cpu_temperature{routerboard_name="CRS310"})', legend="")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch CPU temperature",
                description="",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=18, y=94),
                options=stat.Options(
                    graph_mode=common.BigValueGraphMode.NONE,
                    color_mode=common.BigValueColorMode.BACKGROUND,
                    reduce_options=last_value(),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="celsius",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=70, color="orange")]
                        ),
                        color=dashboard.FieldColor(),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=29,
                targets=[
                    mimir_query(
                        'max by (name) (rate(mktxp_interface_rx_byte_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) * 8',
                        legend="{{name}} in (${traffic_scale})",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'max by (name) (rate(mktxp_interface_tx_byte_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) * 8',
                        legend="{{name}} out (${traffic_scale})",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Switch port traffic",
                description="Traffic on each switch port, as the switch counts it. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=98),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bps",
                        min_val=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            scale_distribution=common.ScaleDistributionConfig(),
                            span_nulls=False,
                        ),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options="/ [(]Log[)]$/"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.scaleDistribution",
                                    value={"type": "symlog", "log": 10, "linearThreshold": 1},
                                )
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=30,
                targets=[
                    mimir_query(
                        'max by (name) (increase(mktxp_interface_rx_error_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_rx_drop_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_tx_error_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_tx_drop_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m]))',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch port errors and drops",
                description="Receive and transmit errors plus drops on each switch port, per 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=98),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=31,
                targets=[
                    mimir_query(
                        'max by (name) (increase(mktxp_link_downs_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[1h]))',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch port link flaps",
                description="Times each switch port's link went down, per hour.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=98),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=32,
                targets=[
                    mimir_query(
                        'max by (name) (mktxp_interface_running{routerboard_name="CRS310",name=~"ether.*|sfp.*"})',
                        legend="{{name}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Switch port links",
                description="Link state of each switch port.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=106),
                options=statetimeline.Options(
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
                        color=dashboard.FieldColor(),
                        custom=statetimeline.FieldConfig(fill_opacity=80),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=33,
                targets=[
                    mimir_query('max(mktxp_system_cpu_temperature{routerboard_name="CRS310"})', legend="CPU")
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'max(mktxp_system_board_temperature1{routerboard_name="CRS310"})', legend="board", ref_id="B"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'max(mktxp_system_phy_temperature{routerboard_name="CRS310"})', legend="PHY", ref_id="C"
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Switch temperatures",
                description="Temperatures the switch reports.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=106),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="celsius", custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="logs",
                id_val=34,
                targets=[
                    loki.Dataquery(
                        expr='{job="home-switch", host!="atlas"}',
                        editor_mode=loki.QueryEditorMode.CODE,
                        ref_id="A",
                        query_type="range",
                        datasource=loki_datasource(),
                    )
                ],
                title="Switch log",
                description="The switch's own log, sent by syslog.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=114),
                options=logs.Options(show_time=True, wrap_log_message=True, enable_log_details=True),
            ),
            dashboard.RowPanel(title="Home nodes", grid_pos=dashboard.GridPos(h=1, w=24, y=124), id_val=35),
            dashboard.Panel(
                type_val="timeseries",
                id_val=36,
                targets=[
                    mimir_query(
                        'rate(node_network_receive_bytes_total{job="node-exporter",device=~"en.*|eth.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} {{device}} in (${traffic_scale})",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                    mimir_query(
                        'rate(node_network_transmit_bytes_total{job="node-exporter",device=~"en.*|eth.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} {{device}} out (${traffic_scale})",
                        ref_id="B",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build(),
                ],
                title="Home node LAN throughput",
                description="Traffic on each home node's physical NIC. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=125),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bps",
                        min_val=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            fill_opacity=10,
                            show_points=common.VisibilityMode.NEVER,
                            scale_distribution=common.ScaleDistributionConfig(),
                            span_nulls=False,
                        ),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byRegexp", options="/ [(]Log[)]$/"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="custom.scaleDistribution",
                                    value={"type": "symlog", "log": 10, "linearThreshold": 1},
                                )
                            ],
                        )
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=37,
                targets=[
                    mimir_query(
                        'sum by (instance, device) (increase(node_network_receive_errs_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_transmit_errs_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_receive_drop_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_transmit_drop_total{job="node-exporter",device=~"en.*|eth.*"}[5m])) * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} {{device}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Home node NIC errors and drops",
                description="Receive and transmit errors plus drops on each home node's physical NIC, per 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=125),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=38,
                targets=[
                    mimir_query(
                        'increase(node_network_carrier_changes_total{job="node-exporter",device=~"en.*|eth.*"}[1h]) * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        legend="{{nodename}} {{device}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Home node NIC link flaps",
                description="Carrier changes (cable or switch port going down and up) on each home node's physical NIC, per hour.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=125),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.RowPanel(
                title="Outage buffering (home Alloy)", grid_pos=dashboard.GridPos(h=1, w=24, y=133), id_val=39
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=40,
                targets=[
                    mimir_query(
                        'max by (node) (clamp_min(max by (pod) (prometheus_remote_storage_highest_timestamp_in_seconds{job="alloy-node"}) - max by (pod) (prometheus_remote_storage_queue_highest_sent_timestamp_seconds{job="alloy-node"}), 0) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        legend="{{node}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Remote-write delay per home node",
                description="How far each home Alloy's delivered data trails what it has collected. It is reported by the Alloy itself, so an outage shows up here only once its backlog is replayed.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, y=134),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=41,
                targets=[
                    mimir_query(
                        'max by (node) (max by (pod) (prometheus_remote_storage_samples_pending{job="alloy-node"}) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        legend="{{node}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Samples waiting to send",
                description="Samples each home Alloy holds in its send queue.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=134),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=42,
                targets=[
                    mimir_query(
                        'max by (node) (max by (pod) (prometheus_remote_write_wal_storage_active_series{job="alloy-node"}) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        legend="{{node}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Series in WAL",
                description="Active series in each home Alloy's write-ahead log.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=134),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", min_val=0, custom=line_style())
                ),
            ),
            dashboard.RowPanel(
                title="Outbound application probes", grid_pos=dashboard.GridPos(h=1, w=24, y=142), id_val=54
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=55,
                targets=[
                    mimir_query(
                        '100 * (1 - avg_over_time(probe_success{job="monitoring/home-application-probes"}[5m]))',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Observed probe failures",
                description="Failure percentage among probe samples recorded in the preceding 5 minutes, split by source_node and target. 0% means all recorded probes passed. No data means no probe samples were available and is not treated as success.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, y=143),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percent", min_val=0, max_val=100, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=56,
                targets=[
                    mimir_query(
                        'probe_duration_seconds{job="monitoring/home-application-probes"}',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Probe duration",
                description="Blackbox exporter probe duration in seconds for each source_node and target. HTTPS duration includes DNS, TCP connection, TLS verification, and the HTTP request.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=143),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style())
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=57,
                targets=[
                    mimir_query(
                        '100 * count_over_time(probe_success{job="monitoring/home-application-probes"}[5m]) / 30',
                        legend="{{source_node}} → {{target}}",
                    )
                    .editor_mode(prometheus.QueryEditorMode.CODE)
                    .range()
                    .build()
                ],
                title="Probe sample coverage",
                description="Observed probe samples as a percentage of the 30 expected samples in each 5-minute window (10-second scrape interval). Missing series remain No data; reduced values show missing observations.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=143),
                options=series_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percent", min_val=0, max_val=100, custom=line_style())
                ),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="traffic_scale",
                    label="Traffic scale",
                    hide=dashboard.VariableHide.DONT_HIDE,
                    query="Linear,Log",
                    current=dashboard.VariableOption(text="Linear", value="Linear"),
                    options=[
                        dashboard.VariableOption(selected=True, text="Linear", value="Linear"),
                        dashboard.VariableOption(selected=False, text="Log", value="Log"),
                    ],
                )
            ]
        ),
        annotations=dashboard.AnnotationContainer(list_val=[]),
        links=[dashboard.DashboardLink(title="Home gateway", icon="dashboard", url="/d/home-gateway")],
    )
