"""Home network overview dashboard."""

from grafana_foundation_sdk.models import common, dashboard, logs, loki, statetimeline

from cluster.cdk8s.monitoring.dashboards.foundation import (
    HeaderLink,
    field_override,
    gateway_error_queries,
    line_style,
    loki_datasource,
    mimir,
    mimir_query,
    stat_options,
    up_down_fields,
)

_SMOKEPING_SELECTOR = 'job="monitoring/smokeping",target=~"gateway|ovh-ns104952"'
_PROBE_GROUP = "source_node, target"
_SENT_PROBES = "smokeping_requests_total"
_RECEIVED_PROBES = "smokeping_response_duration_seconds_count"
_LOSS_THRESHOLD = 0.01
_MIN_PROBES_PER_MINUTE = 240
_MAX_PROBES_PER_MINUTE = 360
_MIN_SAMPLES = 2
_MAX_PROBE_AGE_SECONDS = 45
_HOME_NODE_INFO = 'max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})'
_HOME_ALLOY_PODS = (
    'max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"})'
)
_ALLOY_DELAY = 'clamp_min(max by (pod) (prometheus_remote_storage_highest_timestamp_in_seconds{job="alloy-node"}) - max by (pod) (prometheus_remote_storage_queue_highest_sent_timestamp_seconds{job="alloy-node"}), 0)'
_SWITCH = 'routerboard_name="CRS310"'
_GATEWAY_NAME = "AT&T gateway (192.168.1.254)"
_OPTIPLEX_NAME = "OptiPlex LAN (192.168.1.10)"


def _metric_panel(
    *,
    id_val: int,
    expr: str,
    legend: str,
    title: str,
    description: str,
    grid_pos: dashboard.GridPos,
    field_config: dashboard.FieldConfigSource,
) -> dashboard.Panel:
    return dashboard.Panel(
        type_val="timeseries",
        id_val=id_val,
        targets=[mimir_query(expr, legend=legend).build()],
        title=title,
        description=description,
        datasource=mimir(),
        grid_pos=grid_pos,
        field_config=field_config,
    )


def _line_fields(
    unit: str = "short", min_val: float | None = 0, max_val: float | None = None
) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(unit=unit, min_val=min_val, max_val=max_val, custom=line_style())
    )


def _traffic_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(unit="bps", min_val=0, custom=line_style()),
        overrides=[
            field_override(
                "/ [(]Log[)]$/",
                {"custom.scaleDistribution": {"type": "symlog", "log": 10, "linearThreshold": 1}},
                regex=True,
            )
        ],
    )


def _timeline_options() -> statetimeline.Options:
    return statetimeline.Options(
        per_page=None,
        show_value=common.VisibilityMode.NEVER,
        row_height=0.8,
        tooltip=common.VizTooltipOptions(sort=common.SortOrder.NONE),
    )


def _optics_fields(unit: str = "dBm") -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(unit=unit, custom=line_style()),
        overrides=[
            field_override(
                ".*(warning|alarm).*",
                {
                    "custom.lineStyle": {"dash": [10, 10], "fill": "dash"},
                    "custom.fillOpacity": 0,
                    "custom.lineWidth": 1,
                },
                regex=True,
            )
        ],
    )


def _observed_minute_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
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
    )


def _smokeping_target_names() -> list[dashboard.DashboardFieldConfigSourceOverrides]:
    return [field_override("gateway", {"displayName": _GATEWAY_NAME})]


def _netwatch_target_names() -> list[dashboard.DashboardFieldConfigSourceOverrides]:
    return [
        field_override("att-gateway", {"displayName": _GATEWAY_NAME}),
        field_override("optiplex-lan", {"displayName": _OPTIPLEX_NAME}),
    ]


def _probe_samples(metric: str, operation: str, window: str | None = None) -> str:
    interval = "" if window is None else f"[{window}]"
    current = f'{operation}({metric}{{{_SMOKEPING_SELECTOR},source_node=~"optiplex|wyrm2"}}{interval})'
    legacy = f'{operation}({metric}{{{_SMOKEPING_SELECTOR},source_node="",pod=~"smokeping-[^-]+-[^-]+"}}{interval})'
    # Old OptiPlex scrapes predate source_node; keep their history in the same series.
    return f'({current} or label_replace({legacy}, "source_node", "optiplex", "pod", ".*"))'


def _probe_sum(metric: str, operation: str, window: str) -> str:
    return f"sum by ({_PROBE_GROUP}) ({_probe_samples(metric, operation, window)})"


def _observed_loss_minute() -> str:
    sent = _probe_sum(_SENT_PROBES, "increase", "1m")
    received = _probe_sum(_RECEIVED_PROBES, "increase", "1m")
    loss = f"({sent} - {received}) / {sent} >= bool {_LOSS_THRESHOLD}"
    checks = [f"{sent} >= {_MIN_PROBES_PER_MINUTE} and {sent} <= {_MAX_PROBES_PER_MINUTE}"]
    checks.extend(
        f"{_probe_sum(metric, 'count_over_time', '1m')} >= {_MIN_SAMPLES}"
        for metric in (_SENT_PROBES, _RECEIVED_PROBES)
    )
    checks.extend(f"{_probe_sum(metric, 'resets', '2m')} == 0" for metric in (_SENT_PROBES, _RECEIVED_PROBES))
    checks.extend(
        f"max by ({_PROBE_GROUP}) (time() - {_probe_samples(metric, 'timestamp')}) < {_MAX_PROBE_AGE_SECONDS}"
        for metric in (_SENT_PROBES, _RECEIVED_PROBES)
    )
    checks.append(f"count by ({_PROBE_GROUP}) ({_probe_samples(_SENT_PROBES, 'count_over_time', '2m')}) == 1")
    eligible = f" and on ({_PROBE_GROUP}) ".join(f"({check})" for check in checks)
    return f"({loss}) and on ({_PROBE_GROUP}) ({eligible})"


def _probe_minutes(*, affected: bool) -> str:
    operation = "sum_over_time" if affected else "count_over_time"
    return f"{operation}(({_observed_loss_minute()})[1h:1m])"


def _probe_episode_starts() -> str:
    minute = _observed_loss_minute()
    return (
        f"sum_over_time((delta(({minute})[2m:1m]) > bool 0)[1h:1m])"
        f" or on ({_PROBE_GROUP}) (0 * count_over_time(({minute})[1h:1m]))"
    )


def _uptime_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="s",
            thresholds=dashboard.ThresholdsConfig(
                steps=[dashboard.Threshold(color="orange"), dashboard.Threshold(value=3600, color="green")]
            ),
        )
    )


def _host_probe_rate(metric: str, source_matcher: str) -> str:
    return f'sum by (target) (rate({metric}{{job="monitoring/smokeping",{source_matcher}}}[5m]))'


def _host_probe_loss(source_matcher: str) -> str:
    return (
        f"1 - {_host_probe_rate(_RECEIVED_PROBES, source_matcher)} / {_host_probe_rate(_SENT_PROBES, source_matcher)}"
    )


def _host_probe_rtt(source_matcher: str) -> str:
    return f"{_host_probe_rate('smokeping_response_duration_seconds_sum', source_matcher)} / {_host_probe_rate(_RECEIVED_PROBES, source_matcher)}"


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
                    ).build()
                ],
                title="Home seen from OVH",
                description="Both home nodes report Ready to the API server in OVH. Computed by kube-state-metrics in OVH, so this goes Down live during a home outage, unlike the series scraped at home.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[mimir_query("att_gateway_wan_up", legend="").build()],
                title="WAN",
                description="The gateway's WAN link (broadband status page).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=4, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=4,
                targets=[mimir_query("att_gateway_optical_up", legend="").build()],
                title="Fiber",
                description="The gateway's optical link (fiber status page).",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=8, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=5,
                targets=[mimir_query("att_gateway_uptime_seconds", legend="").build()],
                title="Gateway uptime",
                description="Time since the gateway last booted.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=12, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=_uptime_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=6,
                targets=[mimir_query("resets((max(att_gateway_uptime_seconds))[7d:1m])", legend="").instant().build()],
                title="Gateway reboots (7d)",
                description="Observed decreases in the gateway's uptime in the last 7 days, sampled each minute. Gaps can hide reboots.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=16, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        decimals=0,
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=1, color="orange")]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=7,
                targets=[
                    mimir_query(
                        f"max(max by (node) ({_ALLOY_DELAY} * on (pod) group_left (node) {_HOME_ALLOY_PODS}))",
                        legend="",
                    ).build()
                ],
                title="Home metrics backlog",
                description="Largest delay between the newest sample a home Alloy has collected and the newest it has delivered to Mimir. Grows while home is cut off, drains after it reconnects.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=4, x=20, y=1),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=300, color="orange")]
                        ),
                    )
                ),
            ),
            dashboard.RowPanel(title="Connectivity", grid_pos=dashboard.GridPos(h=1, w=24, y=5), id_val=8),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=9,
                targets=[
                    mimir_query(expr, legend=legend, ref_id=chr(ord("A") + index)).build()
                    for index, (expr, legend) in enumerate(
                        (
                            ("min(att_gateway_wan_up)", "Gateway WAN"),
                            ("min(att_gateway_optical_up)", "Gateway fiber"),
                            ("min(att_gateway_scrape_success)", "Gateway status pages readable"),
                            (
                                'max by (node) (kube_node_status_condition{condition="Ready",status="true",node=~"wyrm2|optiplex"})',
                                "{{node}} Ready (seen from OVH)",
                            ),
                            (
                                'up{job="kubelet",metrics_path="/metrics",node=~"wyrm2|optiplex"}',
                                "{{node}} kubelet scraped",
                            ),
                        )
                    )
                ],
                title="Home connectivity timeline",
                description="Each row is one link or node over time. Gateway link queries combine exporter pod identities so replacements do not add rows; overlapping reports show Down if either reports Down. A blank stretch means Mimir has no samples for it: home was unreachable and the data was not (or not yet) replayed from a home buffer. The OVH-side Ready rows go Down during an outage; the rows scraped at home show what home itself saw once their buffer is replayed.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=24, y=6),
                options=_timeline_options(),
                field_config=up_down_fields(timeline=True),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=10,
                targets=[
                    mimir_query(
                        "rate(att_gateway_wan_receive_bytes_total[5m]) * 8", legend="download (${traffic_scale})"
                    ).build(),
                    mimir_query(
                        "rate(att_gateway_wan_transmit_bytes_total[5m]) * 8",
                        legend="upload (${traffic_scale})",
                        ref_id="B",
                    ).build(),
                ],
                title="Internet throughput (gateway WAN)",
                description="Traffic through the gateway's WAN port. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=14),
                field_config=_traffic_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=11,
                targets=[
                    mimir_query(
                        f'rate(node_network_receive_bytes_total{{job="node-exporter",device=~"nebula.*"}}[5m]) * 8 * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                        legend="{{nodename}} in (${traffic_scale})",
                    ).build(),
                    mimir_query(
                        f'rate(node_network_transmit_bytes_total{{job="node-exporter",device=~"nebula.*"}}[5m]) * 8 * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                        legend="{{nodename}} out (${traffic_scale})",
                        ref_id="B",
                    ).build(),
                ],
                title="Home to OVH tunnel (Nebula)",
                description="Traffic on each home node's Nebula interface, which carries all of its cluster traffic to OVH, including metrics remote-write. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=14),
                field_config=_traffic_fields(),
            ),
            _metric_panel(
                id_val=50,
                expr=f'max(mktxp_interface_rate{{{_SWITCH},name="ether1"}}) * 1000000',
                legend="CRS310 ether1 to AT&T gateway",
                title="AT&T gateway uplink negotiated speed",
                description="Actual negotiated Ethernet speed between CRS310 ether1 and the AT&T BGW320 gateway, sampled every 30 seconds. Compare its transitions with the independent host ping-loss panels beside it.",
                grid_pos=dashboard.GridPos(h=8, w=8, y=22),
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
            _metric_panel(
                id_val=43,
                expr=_host_probe_loss('source_node=~"optiplex|"'),
                legend="{{target}}",
                title="Ping loss from optiplex",
                description="Share of pings from optiplex (5/s per target) that got no reply. Loss to the gateway with none to the switch is the uplink fault in debug/home_network_connectivity/2026_10_gateway_uplink_loss.md.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=22),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=_smokeping_target_names(),
                ),
            ),
            _metric_panel(
                id_val=45,
                expr=_host_probe_loss('source_node="wyrm2"'),
                legend="{{target}}",
                title="Ping loss from wyrm2",
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Loss is the unanswered fraction of 5 pings/s, averaged over 5 minutes. Compare with optiplex beside it; a gap means missing observations.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=22),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=[
                        field_override("gateway", {"displayName": _GATEWAY_NAME}),
                        field_override("optiplex", {"displayName": _OPTIPLEX_NAME}),
                    ],
                ),
            ),
            _metric_panel(
                id_val=44,
                expr=_host_probe_rtt('source_node=~"optiplex|"'),
                legend="{{target}}",
                title="Ping RTT from optiplex",
                description="Mean round-trip time of answered pings from optiplex, per target.",
                grid_pos=dashboard.GridPos(h=8, y=30),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=_smokeping_target_names(),
                ),
            ),
            _metric_panel(
                id_val=46,
                expr=_host_probe_rtt('source_node="wyrm2"'),
                legend="{{target}}",
                title="Ping RTT from wyrm2",
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Mean round-trip time of answered pings, per target.",
                grid_pos=dashboard.GridPos(h=8, x=12, y=30),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=[
                        field_override("gateway", {"displayName": _GATEWAY_NAME}),
                        field_override("optiplex", {"displayName": _OPTIPLEX_NAME}),
                    ],
                ),
            ),
            _metric_panel(
                id_val=47,
                expr=f'max by (name) (mktxp_netwatch_icmp_loss_percent{{{_SWITCH},name=~"att-gateway|optiplex-lan",type="icmp"}}) / 100',
                legend="{{name}}",
                title="UNRELIABLE: Ping batch loss from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Latest reported Netwatch batch, sent by the CRS310 itself: 200 pings at 150 ms intervals once per minute, with a 1 s reply timeout and no early termination. Unlike host smokeping, this is batch loss, not a rolling 5-minute ratio. Scraped every 30 seconds; gaps cannot reconstruct missed batches.",
                grid_pos=dashboard.GridPos(h=8, y=38),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="percentunit", min_val=0, custom=line_style()),
                    overrides=_netwatch_target_names(),
                ),
            ),
            _metric_panel(
                id_val=48,
                expr=f'max by (name) (mktxp_netwatch_icmp_rtt_avg_ms{{{_SWITCH},name=~"att-gateway|optiplex-lan",type="icmp"}}) / 1000',
                legend="{{name}}",
                title="UNRELIABLE: Ping batch RTT from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Mean RTT of answered pings in the latest reported switch-originated Netwatch batch. 200 pings at 150 ms intervals, once per minute; scraped every 30 seconds. A zero RTT during 100% loss does not mean a fast response.",
                grid_pos=dashboard.GridPos(h=8, x=12, y=38),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="s", min_val=0, custom=line_style()),
                    overrides=_netwatch_target_names(),
                ),
            ),
            _metric_panel(
                id_val=49,
                expr=f'max by (name) (increase(mktxp_netwatch_failed_tests{{{_SWITCH},name=~"att-gateway|optiplex-lan",type="icmp"}}[1h]))',
                legend="{{name}}",
                title="UNRELIABLE: Failed probe batches per hour from MikroTik",
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Estimated failed batches in the trailing hour, from reset-aware increase of RouterOS cumulative failed-tests (exported as a gauge by mktxp). Each batch tests 200 pings once per minute; a 5% loss threshold and 1 s RTT thresholds determine failure. This measures failed batches, not distinct outage episodes. Counts can survive missed scrapes unless a probe resets; increase may produce fractional estimates.",
                grid_pos=dashboard.GridPos(h=8, w=24, y=46),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="short", decimals=1, min_val=0, custom=line_style()),
                    overrides=_netwatch_target_names(),
                ),
            ),
            _metric_panel(
                id_val=51,
                expr=_probe_minutes(affected=True),
                legend="{{source_node}} → {{target}}",
                title="Affected probe minutes per trailing hour",
                description="Number of one-minute classifications with at least 1% unanswered host ICMP pings. This is measured affected time, up to 60 minutes per hour, per source and target. A minute is classified only with fresh, reset-free request and reply counters and roughly 5 requests/s. Missing observations are excluded; compare the coverage panel. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                grid_pos=dashboard.GridPos(h=8, y=54),
                field_config=_observed_minute_fields(),
            ),
            _metric_panel(
                id_val=52,
                expr=_probe_episode_starts(),
                legend="{{source_node}} → {{target}}",
                title="Observed episode starts per trailing hour",
                description="An episode starts when an observed healthy minute (under 1% loss) is followed immediately by an affected minute (at least 1% loss). A return from a telemetry gap is left-censored and does not start a new counted episode. Zero requires at least one observed minute. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                grid_pos=dashboard.GridPos(h=8, x=12, y=54),
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
            _metric_panel(
                id_val=53,
                expr=_probe_minutes(affected=False),
                legend="{{source_node}} → {{target}}",
                title="Observed probe minutes per trailing hour",
                description="Coverage for affected-time and episode counts. Each source/target can contribute at most 60 classified minutes per trailing hour. Missing scrapes, stopped probes, and counter resets reduce coverage; low coverage prevents a claim of reliable delivery. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                grid_pos=dashboard.GridPos(h=8, w=24, y=62),
                field_config=_observed_minute_fields(),
            ),
            dashboard.RowPanel(title="Gateway", grid_pos=dashboard.GridPos(h=1, w=24, y=70), id_val=12),
            dashboard.Panel(
                type_val="timeseries",
                id_val=13,
                targets=[
                    mimir_query("att_gateway_optical_rx_power_dbm", legend="Rx power").build(),
                    mimir_query(
                        "att_gateway_optical_rx_power_dbm_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    ).build(),
                ],
                title="Fiber Rx power",
                description="Optical power the gateway receives from the fiber, against the gateway's own warning and alarm bounds. A slow drift toward the low bound points at the fiber or a connector.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, y=71),
                field_config=_optics_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=14,
                targets=[
                    mimir_query("att_gateway_optical_tx_power_dbm", legend="Tx power").build(),
                    mimir_query(
                        "att_gateway_optical_tx_power_dbm_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    ).build(),
                ],
                title="Fiber Tx power",
                description="Optical power the gateway transmits, against its warning and alarm bounds.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=71),
                field_config=_optics_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=15,
                targets=[
                    mimir_query("att_gateway_optical_temperature_celsius", legend="Temperature").build(),
                    mimir_query(
                        "att_gateway_optical_temperature_celsius_threshold", legend="{{level}} ({{bound}})", ref_id="B"
                    ).build(),
                ],
                title="Optics temperature",
                description="Temperature of the gateway's optical module, against its warning and alarm bounds.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=71),
                field_config=_optics_fields("celsius"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=16,
                targets=gateway_error_queries("wan"),
                title="Gateway WAN errors and drops",
                description="Errors and drops on the gateway's WAN interface, per 5 minutes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, y=79),
                field_config=_line_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=17,
                targets=gateway_error_queries("lan_port", legend_prefix="port {{port}} "),
                title="Gateway LAN port errors and drops",
                description="Errors and drops on each gateway LAN port, per 5 minutes. Port 1 is the uplink to the home switch.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=6, y=79),
                field_config=_line_fields(),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=18,
                targets=[mimir_query("att_gateway_lan_port_up", legend="Port {{port}}").build()],
                title="Gateway LAN ports",
                description="Link state of each gateway LAN port. Port 1 is the uplink to the home switch.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=79),
                options=_timeline_options(),
                field_config=up_down_fields(timeline=True),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=19,
                targets=[
                    mimir_query("att_gateway_dhcp_leases_allocated", legend="allocated").build(),
                    mimir_query("att_gateway_dhcp_leases_available", legend="available", ref_id="B").build(),
                ],
                title="Gateway DHCP leases",
                description="Leases the gateway has handed out, and how many it has left.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=79),
                field_config=_line_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=20,
                targets=[
                    mimir_query("att_gateway_nat_sessions_in_use", legend="in use").build(),
                    mimir_query("att_gateway_nat_sessions_available", legend="capacity", ref_id="B").build(),
                ],
                title="Gateway NAT sessions",
                description="Entries in the gateway's NAT session table against its capacity. A full table drops new connections.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, y=87),
                field_config=_line_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=21,
                targets=[mimir_query("att_gateway_speedtest_throughput_bps", legend="{{direction}}").build()],
                title="Speed test throughput",
                description="Result of the gateway's latest successful speed test to AT&T, protocol overhead included.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=12, y=87),
                options=stat_options(common.BigValueColorMode.NONE),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bps")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=22,
                targets=[mimir_query("att_gateway_speedtest_latency_seconds", legend="{{direction}}").build()],
                title="Speed test latency",
                description="Average round-trip latency during the latest successful speed test.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=16, y=87),
                options=stat_options(common.BigValueColorMode.NONE),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="s")),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=23,
                targets=[
                    mimir_query(
                        "att_gateway_speedtest_completed_timestamp_seconds * 1000", legend="{{direction}}"
                    ).build()
                ],
                title="Speed test ran",
                description="When the latest successful speed test in each direction completed.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=4, x=20, y=87),
                options=stat_options(common.BigValueColorMode.NONE),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="dateTimeFromNow")),
            ),
            dashboard.RowPanel(title="Switch (CRS310)", grid_pos=dashboard.GridPos(h=1, w=24, y=93), id_val=24),
            dashboard.Panel(
                type_val="stat",
                id_val=25,
                targets=[
                    mimir_query(
                        f"(max(mktxp_system_uptime{{{_SWITCH}}}) > bool 0) or on () vector(0)", legend=""
                    ).build()
                ],
                title="Switch reachable",
                description="The switch answers the exporter's API queries. mktxp keeps serving its own metrics while the switch is unreachable, so this checks for switch data rather than the scrape.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, y=94),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=up_down_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=26,
                targets=[mimir_query(f"max(mktxp_system_uptime{{{_SWITCH}}})", legend="").build()],
                title="Switch uptime",
                description="Time since the switch last booted.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=6, y=94),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=_uptime_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=27,
                targets=[mimir_query(f"max(mktxp_system_cpu_load{{{_SWITCH}}})", legend="").build()],
                title="Switch CPU load",
                description="",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=12, y=94),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="percent",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=80, color="orange")]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=28,
                targets=[mimir_query(f"max(mktxp_system_cpu_temperature{{{_SWITCH}}})", legend="").build()],
                title="Switch CPU temperature",
                description="",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=18, y=94),
                options=stat_options(common.BigValueColorMode.BACKGROUND),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="celsius",
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[dashboard.Threshold(color="green"), dashboard.Threshold(value=70, color="orange")]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=29,
                targets=[
                    mimir_query(
                        f'max by (name) (rate(mktxp_interface_rx_byte_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m])) * 8',
                        legend="{{name}} in (${traffic_scale})",
                    ).build(),
                    mimir_query(
                        f'max by (name) (rate(mktxp_interface_tx_byte_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m])) * 8',
                        legend="{{name}} out (${traffic_scale})",
                        ref_id="B",
                    ).build(),
                ],
                title="Switch port traffic",
                description="Traffic on each switch port, as the switch counts it. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=98),
                field_config=_traffic_fields(),
            ),
            _metric_panel(
                id_val=30,
                expr=f'max by (name) (increase(mktxp_interface_rx_error_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m])) + max by (name) (increase(mktxp_interface_rx_drop_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m])) + max by (name) (increase(mktxp_interface_tx_error_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m])) + max by (name) (increase(mktxp_interface_tx_drop_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[5m]))',
                legend="{{name}}",
                title="Switch port errors and drops",
                description="Receive and transmit errors plus drops on each switch port, per 5 minutes.",
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=98),
                field_config=_line_fields(),
            ),
            _metric_panel(
                id_val=31,
                expr=f'max by (name) (increase(mktxp_link_downs_total{{{_SWITCH},name=~"ether.*|sfp.*"}}[1h]))',
                legend="{{name}}",
                title="Switch port link flaps",
                description="Times each switch port's link went down, per hour.",
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=98),
                field_config=_line_fields(),
            ),
            dashboard.Panel(
                type_val="state-timeline",
                id_val=32,
                targets=[
                    mimir_query(
                        f'max by (name) (mktxp_interface_running{{{_SWITCH},name=~"ether.*|sfp.*"}})', legend="{{name}}"
                    ).build()
                ],
                title="Switch port links",
                description="Link state of each switch port.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=106),
                options=_timeline_options(),
                field_config=up_down_fields(timeline=True),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=33,
                targets=[
                    mimir_query(expr, legend=legend, ref_id=chr(ord("A") + index)).build()
                    for index, (expr, legend) in enumerate(
                        (
                            (f"max(mktxp_system_cpu_temperature{{{_SWITCH}}})", "CPU"),
                            (f"max(mktxp_system_board_temperature1{{{_SWITCH}}})", "board"),
                            (f"max(mktxp_system_phy_temperature{{{_SWITCH}}})", "PHY"),
                        )
                    )
                ],
                title="Switch temperatures",
                description="Temperatures the switch reports.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=106),
                field_config=_line_fields(unit="celsius", min_val=None),
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
                        f'rate(node_network_receive_bytes_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m]) * 8 * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                        legend="{{nodename}} {{device}} in (${traffic_scale})",
                    ).build(),
                    mimir_query(
                        f'rate(node_network_transmit_bytes_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m]) * 8 * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                        legend="{{nodename}} {{device}} out (${traffic_scale})",
                        ref_id="B",
                    ).build(),
                ],
                title="Home node LAN throughput",
                description="Traffic on each home node's physical NIC. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=125),
                field_config=_traffic_fields(),
            ),
            _metric_panel(
                id_val=37,
                expr=f'sum by (instance, device) (increase(node_network_receive_errs_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m])+increase(node_network_transmit_errs_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m])+increase(node_network_receive_drop_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m])+increase(node_network_transmit_drop_total{{job="node-exporter",device=~"en.*|eth.*"}}[5m])) * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                legend="{{nodename}} {{device}}",
                title="Home node NIC errors and drops",
                description="Receive and transmit errors plus drops on each home node's physical NIC, per 5 minutes.",
                grid_pos=dashboard.GridPos(h=8, w=6, x=12, y=125),
                field_config=_line_fields(),
            ),
            _metric_panel(
                id_val=38,
                expr=f'increase(node_network_carrier_changes_total{{job="node-exporter",device=~"en.*|eth.*"}}[1h]) * on (instance) group_left (nodename) {_HOME_NODE_INFO}',
                legend="{{nodename}} {{device}}",
                title="Home node NIC link flaps",
                description="Carrier changes (cable or switch port going down and up) on each home node's physical NIC, per hour.",
                grid_pos=dashboard.GridPos(h=8, w=6, x=18, y=125),
                field_config=_line_fields(),
            ),
            dashboard.RowPanel(
                title="Outage buffering (home Alloy)", grid_pos=dashboard.GridPos(h=1, w=24, y=133), id_val=39
            ),
            _metric_panel(
                id_val=40,
                expr=f"max by (node) ({_ALLOY_DELAY} * on (pod) group_left (node) {_HOME_ALLOY_PODS})",
                legend="{{node}}",
                title="Remote-write delay per home node",
                description="How far each home Alloy's delivered data trails what it has collected. It is reported by the Alloy itself, so an outage shows up here only once its backlog is replayed.",
                grid_pos=dashboard.GridPos(h=8, w=8, y=134),
                field_config=_line_fields(unit="s"),
            ),
            _metric_panel(
                id_val=41,
                expr=f'max by (node) (max by (pod) (prometheus_remote_storage_samples_pending{{job="alloy-node"}}) * on (pod) group_left (node) {_HOME_ALLOY_PODS})',
                legend="{{node}}",
                title="Samples waiting to send",
                description="Samples each home Alloy holds in its send queue.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=134),
                field_config=_line_fields(),
            ),
            _metric_panel(
                id_val=42,
                expr=f'max by (node) (max by (pod) (prometheus_remote_write_wal_storage_active_series{{job="alloy-node"}}) * on (pod) group_left (node) {_HOME_ALLOY_PODS})',
                legend="{{node}}",
                title="Series in WAL",
                description="Active series in each home Alloy's write-ahead log.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=134),
                field_config=_line_fields(),
            ),
            dashboard.RowPanel(
                title="Outbound application probes", grid_pos=dashboard.GridPos(h=1, w=24, y=142), id_val=54
            ),
            _metric_panel(
                id_val=55,
                expr='100 * (1 - avg_over_time(probe_success{job="monitoring/home-application-probes"}[5m]))',
                legend="{{source_node}} → {{target}}",
                title="Observed probe failures",
                description="Failure percentage among probe samples recorded in the preceding 5 minutes, split by source_node and target. 0% means all recorded probes passed. No data means no probe samples were available and is not treated as success.",
                grid_pos=dashboard.GridPos(h=8, w=8, y=143),
                field_config=_line_fields(unit="percent", max_val=100),
            ),
            _metric_panel(
                id_val=56,
                expr='probe_duration_seconds{job="monitoring/home-application-probes"}',
                legend="{{source_node}} → {{target}}",
                title="Probe duration",
                description="Blackbox exporter probe duration in seconds for each source_node and target. HTTPS duration includes DNS, TCP connection, TLS verification, and the HTTP request.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=8, y=143),
                field_config=_line_fields(unit="s"),
            ),
            _metric_panel(
                id_val=57,
                expr='100 * count_over_time(probe_success{job="monitoring/home-application-probes"}[5m]) / 30',
                legend="{{source_node}} → {{target}}",
                title="Probe sample coverage",
                description="Observed probe samples as a percentage of the 30 expected samples in each 5-minute window (10-second scrape interval). Missing series remain No data; reduced values show missing observations.",
                grid_pos=dashboard.GridPos(h=8, w=8, x=16, y=143),
                field_config=_line_fields(unit="percent", max_val=100),
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
        links=[HeaderLink(title="Home gateway", icon="dashboard", url="/d/home-gateway")],
    )
