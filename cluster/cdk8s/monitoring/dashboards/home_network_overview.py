"""Home network overview, authored with ordinary panel constructors.

The constructors return native dashboard JSON. Unusual panels and any complete
fieldConfig/options/target remain ordinary dictionaries at their call sites.
"""

import json
from typing import Any

_MIMIR = {"type": "prometheus", "uid": "mimir"}
_TIMESERIES_OPTIONS = {
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
    "tooltip": {"mode": "multi", "sort": "none"},
}


def _mimir_target(expr: str, legend: str, ref_id: str = "A") -> dict[str, Any]:
    return {
        "datasource": _MIMIR,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend,
        "refId": ref_id,
        "range": True,
    }


def _row(id: int, title: str, grid_pos: dict[str, int]) -> dict[str, Any]:
    return {"collapsed": False, "gridPos": grid_pos, "id": id, "panels": [], "title": title, "type": "row"}


def _panel(
    id: int,
    title: str,
    kind: str,
    grid_pos: dict[str, int],
    *,
    description: str,
    targets: list[dict[str, Any]],
    field_config: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    return {
        "datasource": _MIMIR,
        "description": description,
        "gridPos": grid_pos,
        "id": id,
        "title": title,
        "type": kind,
        "targets": targets,
        "fieldConfig": field_config,
        "options": options,
    }


def _timeseries(
    id: int,
    title: str,
    grid_pos: dict[str, int],
    *,
    description: str,
    targets: list[dict[str, Any]],
    field_config: dict[str, Any],
) -> dict[str, Any]:
    return _panel(
        id,
        title,
        "timeseries",
        grid_pos,
        description=description,
        targets=targets,
        field_config=field_config,
        options=_TIMESERIES_OPTIONS,
    )


def _stat(
    id: int,
    title: str,
    grid_pos: dict[str, int],
    *,
    description: str,
    targets: list[dict[str, Any]],
    field_config: dict[str, Any],
    color_mode: str,
) -> dict[str, Any]:
    return _panel(
        id,
        title,
        "stat",
        grid_pos,
        description=description,
        targets=targets,
        field_config=field_config,
        options={
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
    )


def dashboard() -> dict[str, Any]:
    return {
        "annotations": {"list": []},
        "description": "Home connectivity at a glance: AT&T gateway, home nodes, and how metrics are buffered through outages.",
        "editable": False,
        "graphTooltip": 1,
        "links": [{"title": "Home gateway", "type": "link", "url": "/d/home-gateway", "icon": "dashboard"}],
        "panels": [
            _row(1, "Right now", {"h": 1, "w": 24, "x": 0, "y": 0}),
            _stat(
                2,
                "Home seen from OVH",
                {"h": 4, "w": 4, "x": 0, "y": 1},
                description="Both home nodes report Ready to the API server in OVH. Computed by kube-state-metrics in OVH, so this goes Down live during a home outage, unlike the series scraped at home.",
                targets=[
                    _mimir_target(
                        'min(kube_node_status_condition{condition="Ready",status="true",node=~"wyrm2|optiplex"})', ""
                    )
                ],
                field_config={
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                3,
                "WAN",
                {"h": 4, "w": 4, "x": 4, "y": 1},
                description="The gateway's WAN link (broadband status page).",
                targets=[_mimir_target("att_gateway_wan_up", "")],
                field_config={
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                4,
                "Fiber",
                {"h": 4, "w": 4, "x": 8, "y": 1},
                description="The gateway's optical link (fiber status page).",
                targets=[_mimir_target("att_gateway_optical_up", "")],
                field_config={
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                5,
                "Gateway uptime",
                {"h": 4, "w": 4, "x": 12, "y": 1},
                description="Time since the gateway last booted.",
                targets=[_mimir_target("att_gateway_uptime_seconds", "")],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "orange", "value": None}, {"color": "green", "value": 3600}],
                        },
                        "color": {"mode": "thresholds"},
                        "unit": "s",
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                6,
                "Gateway reboots (7d)",
                {"h": 4, "w": 4, "x": 16, "y": 1},
                description="Times the gateway's uptime went back to zero in the last 7 days.",
                targets=[_mimir_target("resets(att_gateway_uptime_seconds[7d])", "")],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 1}],
                        },
                        "color": {"mode": "thresholds"},
                        "decimals": 0,
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                7,
                "Home metrics backlog",
                {"h": 4, "w": 4, "x": 20, "y": 1},
                description="Largest delay between the newest sample a home Alloy has collected and the newest it has delivered to Mimir. Grows while home is cut off, drains after it reconnects.",
                targets=[
                    _mimir_target(
                        'max(max by (node) (clamp_min(max by (pod) (prometheus_remote_storage_highest_timestamp_in_seconds{job="alloy-node"}) - max by (pod) (prometheus_remote_storage_queue_highest_sent_timestamp_seconds{job="alloy-node"}), 0) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"})))',
                        "",
                    )
                ],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 300}],
                        },
                        "color": {"mode": "thresholds"},
                        "unit": "s",
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _row(8, "Connectivity", {"h": 1, "w": 24, "x": 0, "y": 5}),
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "description": "Each row is one link or node over time. Gateway link queries combine exporter pod identities "
                "so replacements do not add rows; overlapping reports show Down if either reports Down. A "
                "blank stretch means Mimir has no samples for it: home was unreachable and the data was not "
                "(or not yet) replayed from a home buffer. The OVH-side Ready rows go Down during an outage; "
                "the rows scraped at home show what home itself saw once their buffer is replayed.",
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 6},
                "id": 9,
                "title": "Home connectivity timeline",
                "type": "state-timeline",
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "min(att_gateway_wan_up)",
                        "legendFormat": "Gateway WAN",
                        "refId": "A",
                        "range": True,
                    },
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "min(att_gateway_optical_up)",
                        "legendFormat": "Gateway fiber",
                        "refId": "B",
                        "range": True,
                    },
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "min(att_gateway_scrape_success)",
                        "legendFormat": "Gateway status pages readable",
                        "refId": "C",
                        "range": True,
                    },
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "max by (node) "
                        '(kube_node_status_condition{condition="Ready",status="true",node=~"wyrm2|optiplex"})',
                        "legendFormat": "{{node}} Ready (seen from OVH)",
                        "refId": "D",
                        "range": True,
                    },
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": 'up{job="kubelet",metrics_path="/metrics",node=~"wyrm2|optiplex"}',
                        "legendFormat": "{{node}} kubelet scraped",
                        "refId": "E",
                        "range": True,
                    },
                ],
                "fieldConfig": {
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                        "color": {"mode": "thresholds"},
                        "custom": {"fillOpacity": 80, "lineWidth": 0},
                    },
                    "overrides": [],
                },
                "options": {
                    "showValue": "never",
                    "mergeValues": True,
                    "alignValue": "left",
                    "rowHeight": 0.8,
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                    "tooltip": {"mode": "single", "sort": "none"},
                },
            },
            _timeseries(
                10,
                "Internet throughput (gateway WAN)",
                {"h": 8, "w": 12, "x": 0, "y": 14},
                description="Traffic through the gateway's WAN port. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                targets=[
                    _mimir_target("rate(att_gateway_wan_receive_bytes_total[5m]) * 8", "download (${traffic_scale})"),
                    _mimir_target(
                        "rate(att_gateway_wan_transmit_bytes_total[5m]) * 8", "upload (${traffic_scale})", "B"
                    ),
                ],
                field_config={
                    "defaults": {
                        "unit": "bps",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "scaleDistribution": {"type": "linear"},
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": "/ [(]Log[)]$/"},
                            "properties": [
                                {
                                    "id": "custom.scaleDistribution",
                                    "value": {"type": "symlog", "log": 10, "linearThreshold": 1},
                                }
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                11,
                "Home to OVH tunnel (Nebula)",
                {"h": 8, "w": 12, "x": 12, "y": 14},
                description="Traffic on each home node's Nebula interface, which carries all of its cluster traffic to OVH, including metrics remote-write. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                targets=[
                    _mimir_target(
                        'rate(node_network_receive_bytes_total{job="node-exporter",device=~"nebula.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} in (${traffic_scale})",
                    ),
                    _mimir_target(
                        'rate(node_network_transmit_bytes_total{job="node-exporter",device=~"nebula.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} out (${traffic_scale})",
                        "B",
                    ),
                ],
                field_config={
                    "defaults": {
                        "unit": "bps",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "scaleDistribution": {"type": "linear"},
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": "/ [(]Log[)]$/"},
                            "properties": [
                                {
                                    "id": "custom.scaleDistribution",
                                    "value": {"type": "symlog", "log": 10, "linearThreshold": 1},
                                }
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                50,
                "AT&T gateway uplink negotiated speed",
                {"h": 8, "w": 8, "x": 0, "y": 22},
                description="Actual negotiated Ethernet speed between CRS310 ether1 and the AT&T BGW320 gateway, sampled every 30 seconds. Compare its transitions with the independent host ping-loss panels beside it.",
                targets=[
                    _mimir_target(
                        'max(mktxp_interface_rate{routerboard_name="CRS310",name="ether1"}) * 1000000',
                        "CRS310 ether1 to AT&T gateway",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "bps",
                        "decimals": 1,
                        "min": 0,
                        "color": {"mode": "fixed", "fixedColor": "blue"},
                        "custom": {
                            "drawStyle": "line",
                            "lineInterpolation": "stepAfter",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "axisLabel": "Negotiated speed",
                        },
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                43,
                "Ping loss from optiplex",
                {"h": 8, "w": 8, "x": 8, "y": 22},
                description="Share of pings from optiplex (5/s per target) that got no reply. Loss to the gateway with none to the switch is the uplink fault in debug/home_network_connectivity/2026_10_gateway_uplink_loss.md.",
                targets=[
                    _mimir_target(
                        '1 - sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node=~"optiplex|"}[5m])) / sum by (target) (rate(smokeping_requests_total{job="monitoring/smokeping",source_node=~"optiplex|"}[5m]))',
                        "{{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "percentunit",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        }
                    ],
                },
            ),
            _timeseries(
                45,
                "Ping loss from wyrm2",
                {"h": 8, "w": 8, "x": 16, "y": 22},
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Loss is the unanswered fraction of 5 pings/s, averaged over 5 minutes. Compare with optiplex beside it; a gap means missing observations.",
                targets=[
                    _mimir_target(
                        '1 - sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node="wyrm2"}[5m])) / sum by (target) (rate(smokeping_requests_total{job="monitoring/smokeping",source_node="wyrm2"}[5m]))',
                        "{{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "percentunit",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "optiplex"},
                            "properties": [{"id": "displayName", "value": "OptiPlex LAN (192.168.1.10)"}],
                        },
                    ],
                },
            ),
            _timeseries(
                44,
                "Ping RTT from optiplex",
                {"h": 8, "w": 12, "x": 0, "y": 30},
                description="Mean round-trip time of answered pings from optiplex, per target.",
                targets=[
                    _mimir_target(
                        'sum by (target) (rate(smokeping_response_duration_seconds_sum{job="monitoring/smokeping",source_node=~"optiplex|"}[5m])) / sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node=~"optiplex|"}[5m]))',
                        "{{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "s",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        }
                    ],
                },
            ),
            _timeseries(
                46,
                "Ping RTT from wyrm2",
                {"h": 8, "w": 12, "x": 12, "y": 30},
                description="ICMP probes from wyrm2 to the AT&T gateway, MikroTik switch, optiplex LAN address (192.168.1.10), and ovh-ns104952 over Nebula. Mean round-trip time of answered pings, per target.",
                targets=[
                    _mimir_target(
                        'sum by (target) (rate(smokeping_response_duration_seconds_sum{job="monitoring/smokeping",source_node="wyrm2"}[5m])) / sum by (target) (rate(smokeping_response_duration_seconds_count{job="monitoring/smokeping",source_node="wyrm2"}[5m]))',
                        "{{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "s",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "optiplex"},
                            "properties": [{"id": "displayName", "value": "OptiPlex LAN (192.168.1.10)"}],
                        },
                    ],
                },
            ),
            _timeseries(
                47,
                "UNRELIABLE: Ping batch loss from MikroTik",
                {"h": 8, "w": 12, "x": 0, "y": 38},
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Latest reported Netwatch batch, sent by the CRS310 itself: 200 pings at 150 ms intervals once per minute, with a 1 s reply timeout and no early termination. Unlike host smokeping, this is batch loss, not a rolling 5-minute ratio. Scraped every 30 seconds; gaps cannot reconstruct missed batches.",
                targets=[
                    _mimir_target(
                        'max by (name) (mktxp_netwatch_icmp_loss_percent{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}) / 100',
                        "{{name}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "percentunit",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "att-gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "optiplex-lan"},
                            "properties": [{"id": "displayName", "value": "OptiPlex LAN (192.168.1.10)"}],
                        },
                    ],
                },
            ),
            _timeseries(
                48,
                "UNRELIABLE: Ping batch RTT from MikroTik",
                {"h": 8, "w": 12, "x": 12, "y": 38},
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Mean RTT of answered pings in the latest reported switch-originated Netwatch batch. 200 pings at 150 ms intervals, once per minute; scraped every 30 seconds. A zero RTT during 100% loss does not mean a fast response.",
                targets=[
                    _mimir_target(
                        'max by (name) (mktxp_netwatch_icmp_rtt_avg_ms{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}) / 1000',
                        "{{name}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "s",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "att-gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "optiplex-lan"},
                            "properties": [{"id": "displayName", "value": "OptiPlex LAN (192.168.1.10)"}],
                        },
                    ],
                },
            ),
            _timeseries(
                49,
                "UNRELIABLE: Failed probe batches per hour from MikroTik",
                {"h": 8, "w": 24, "x": 0, "y": 46},
                description="UNRELIABLE: captures at OptiPlex found invalid ICMP checksums in Netwatch requests; reported loss matched the malformed requests, and enabling a concurrent probe changed the pattern. These measurements cannot establish network delivery or outage frequency. Use the independent OptiPlex and wyrm2 host probes for reliability. Estimated failed batches in the trailing hour, from reset-aware increase of RouterOS cumulative failed-tests (exported as a gauge by mktxp). Each batch tests 200 pings once per minute; a 5% loss threshold and 1 s RTT thresholds determine failure. This measures failed batches, not distinct outage episodes. Counts can survive missed scrapes unless a probe resets; increase may produce fractional estimates.",
                targets=[
                    _mimir_target(
                        'max by (name) (increase(mktxp_netwatch_failed_tests{routerboard_name="CRS310",name=~"att-gateway|optiplex-lan",type="icmp"}[1h]))',
                        "{{name}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                        "decimals": 1,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "att-gateway"},
                            "properties": [{"id": "displayName", "value": "AT&T gateway (192.168.1.254)"}],
                        },
                        {
                            "matcher": {"id": "byName", "options": "optiplex-lan"},
                            "properties": [{"id": "displayName", "value": "OptiPlex LAN (192.168.1.10)"}],
                        },
                    ],
                },
            ),
            _timeseries(
                51,
                "Affected probe minutes per trailing hour",
                {"h": 8, "w": 12, "x": 0, "y": 54},
                description="Number of one-minute classifications with at least 1% unanswered host ICMP pings. This is measured affected time, up to 60 minutes per hour, per source and target. A minute is classified only with fresh, reset-free request and reply counters and roughly 5 requests/s. Missing observations are excluded; compare the coverage panel. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                targets=[
                    _mimir_target(
                        'sum_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m])',
                        "{{source_node}} → {{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "m",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "lineInterpolation": "stepAfter",
                        },
                        "min": 0,
                        "max": 60,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                52,
                "Observed episode starts per trailing hour",
                {"h": 8, "w": 12, "x": 12, "y": 54},
                description="An episode starts when an observed healthy minute (under 1% loss) is followed immediately by an affected minute (at least 1% loss). A return from a telemetry gap is left-censored and does not start a new counted episode. Zero requires at least one observed minute. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                targets=[
                    _mimir_target(
                        'sum_over_time((delta((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[2m:1m]) > bool 0)[1h:1m]) or on (source_node, target) (0 * count_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m]))',
                        "{{source_node}} → {{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "none",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "lineInterpolation": "stepAfter",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                53,
                "Observed probe minutes per trailing hour",
                {"h": 8, "w": 24, "x": 0, "y": 62},
                description="Coverage for affected-time and episode counts. Each source/target can contribute at most 60 classified minutes per trailing hour. Missing scrapes, stopped probes, and counter resets reduce coverage; low coverage prevents a claim of reliable delivery. Computed from raw probe samples when queried, so buffered samples can fill historical gaps after delivery. Legacy unlabelled smokeping Pod samples are attributed to optiplex. A two-minute window spanning more than one Pod stays unclassified during a rollout.",
                targets=[
                    _mimir_target(
                        'count_over_time((((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) - sum by (source_node, target) ((increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*")))) / sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= bool 0.01) and on (source_node, target) ((sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 240 and sum by (source_node, target) ((increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(increase(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) <= 360) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[1m]) or label_replace(count_over_time(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[1m]), "source_node", "optiplex", "pod", ".*"))) >= 2) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (sum by (source_node, target) ((resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(resets(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 0) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (max by (source_node, target) (time() - (timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}) or label_replace(timestamp(smokeping_response_duration_seconds_count{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}), "source_node", "optiplex", "pod", ".*"))) < 45) and on (source_node, target) (count by (source_node, target) ((count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node=~"optiplex|wyrm2"}[2m]) or label_replace(count_over_time(smokeping_requests_total{job="monitoring/smokeping",target=~"gateway|ovh-ns104952",source_node="",pod=~"smokeping-[^-]+-[^-]+"}[2m]), "source_node", "optiplex", "pod", ".*"))) == 1)))[1h:1m])',
                        "{{source_node}} → {{target}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "m",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "lineInterpolation": "stepAfter",
                        },
                        "min": 0,
                        "max": 60,
                    },
                    "overrides": [],
                },
            ),
            _row(12, "Gateway", {"h": 1, "w": 24, "x": 0, "y": 70}),
            _timeseries(
                13,
                "Fiber Rx power",
                {"h": 8, "w": 8, "x": 0, "y": 71},
                description="Optical power the gateway receives from the fiber, against the gateway's own warning and alarm bounds. A slow drift toward the low bound points at the fiber or a connector.",
                targets=[
                    _mimir_target("att_gateway_optical_rx_power_dbm", "Rx power"),
                    _mimir_target("att_gateway_optical_rx_power_dbm_threshold", "{{level}} ({{bound}})", "B"),
                ],
                field_config={
                    "defaults": {
                        "unit": "dBm",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": ".*(warning|alarm).*"},
                            "properties": [
                                {"id": "custom.lineStyle", "value": {"dash": [10, 10], "fill": "dash"}},
                                {"id": "custom.fillOpacity", "value": 0},
                                {"id": "custom.lineWidth", "value": 1},
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                14,
                "Fiber Tx power",
                {"h": 8, "w": 8, "x": 8, "y": 71},
                description="Optical power the gateway transmits, against its warning and alarm bounds.",
                targets=[
                    _mimir_target("att_gateway_optical_tx_power_dbm", "Tx power"),
                    _mimir_target("att_gateway_optical_tx_power_dbm_threshold", "{{level}} ({{bound}})", "B"),
                ],
                field_config={
                    "defaults": {
                        "unit": "dBm",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": ".*(warning|alarm).*"},
                            "properties": [
                                {"id": "custom.lineStyle", "value": {"dash": [10, 10], "fill": "dash"}},
                                {"id": "custom.fillOpacity", "value": 0},
                                {"id": "custom.lineWidth", "value": 1},
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                15,
                "Optics temperature",
                {"h": 8, "w": 8, "x": 16, "y": 71},
                description="Temperature of the gateway's optical module, against its warning and alarm bounds.",
                targets=[
                    _mimir_target("att_gateway_optical_temperature_celsius", "Temperature"),
                    _mimir_target("att_gateway_optical_temperature_celsius_threshold", "{{level}} ({{bound}})", "B"),
                ],
                field_config={
                    "defaults": {
                        "unit": "celsius",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": ".*(warning|alarm).*"},
                            "properties": [
                                {"id": "custom.lineStyle", "value": {"dash": [10, 10], "fill": "dash"}},
                                {"id": "custom.fillOpacity", "value": 0},
                                {"id": "custom.lineWidth", "value": 1},
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                16,
                "Gateway WAN errors and drops",
                {"h": 8, "w": 6, "x": 0, "y": 79},
                description="Errors and drops on the gateway's WAN interface, per 5 minutes.",
                targets=[
                    _mimir_target("increase(att_gateway_wan_receive_errors_total[5m])", "rx errors"),
                    _mimir_target("increase(att_gateway_wan_receive_drops_total[5m])", "rx drops", "B"),
                    _mimir_target("increase(att_gateway_wan_transmit_errors_total[5m])", "tx errors", "C"),
                    _mimir_target("increase(att_gateway_wan_transmit_drops_total[5m])", "tx drops", "D"),
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                17,
                "Gateway LAN port errors and drops",
                {"h": 8, "w": 6, "x": 6, "y": 79},
                description="Errors and drops on each gateway LAN port, per 5 minutes. Port 1 is the uplink to the home switch.",
                targets=[
                    _mimir_target("increase(att_gateway_lan_port_receive_errors_total[5m])", "port {{port}} rx errors"),
                    _mimir_target(
                        "increase(att_gateway_lan_port_receive_drops_total[5m])", "port {{port}} rx drops", "B"
                    ),
                    _mimir_target(
                        "increase(att_gateway_lan_port_transmit_errors_total[5m])", "port {{port}} tx errors", "C"
                    ),
                    _mimir_target(
                        "increase(att_gateway_lan_port_transmit_drops_total[5m])", "port {{port}} tx drops", "D"
                    ),
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "description": "Link state of each gateway LAN port. Port 1 is the uplink to the home switch.",
                "gridPos": {"h": 8, "w": 6, "x": 12, "y": 79},
                "id": 18,
                "title": "Gateway LAN ports",
                "type": "state-timeline",
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "att_gateway_lan_port_up",
                        "legendFormat": "Port {{port}}",
                        "refId": "A",
                        "range": True,
                    }
                ],
                "fieldConfig": {
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                        "color": {"mode": "thresholds"},
                        "custom": {"fillOpacity": 80, "lineWidth": 0},
                    },
                    "overrides": [],
                },
                "options": {
                    "showValue": "never",
                    "mergeValues": True,
                    "alignValue": "left",
                    "rowHeight": 0.8,
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                    "tooltip": {"mode": "single", "sort": "none"},
                },
            },
            _timeseries(
                19,
                "Gateway DHCP leases",
                {"h": 8, "w": 6, "x": 18, "y": 79},
                description="Leases the gateway has handed out, and how many it has left.",
                targets=[
                    _mimir_target("att_gateway_dhcp_leases_allocated", "allocated"),
                    _mimir_target("att_gateway_dhcp_leases_available", "available", "B"),
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                20,
                "Gateway NAT sessions",
                {"h": 6, "w": 12, "x": 0, "y": 87},
                description="Entries in the gateway's NAT session table against its capacity. A full table drops new connections.",
                targets=[
                    _mimir_target("att_gateway_nat_sessions_in_use", "in use"),
                    _mimir_target("att_gateway_nat_sessions_available", "capacity", "B"),
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _stat(
                21,
                "Speed test throughput",
                {"h": 6, "w": 4, "x": 12, "y": 87},
                description="Result of the gateway's latest successful speed test to AT&T, protocol overhead included.",
                targets=[_mimir_target("att_gateway_speedtest_throughput_bps", "{{direction}}")],
                field_config={"defaults": {"unit": "bps"}, "overrides": []},
                color_mode="none",
            ),
            _stat(
                22,
                "Speed test latency",
                {"h": 6, "w": 4, "x": 16, "y": 87},
                description="Average round-trip latency during the latest successful speed test.",
                targets=[_mimir_target("att_gateway_speedtest_latency_seconds", "{{direction}}")],
                field_config={"defaults": {"unit": "s"}, "overrides": []},
                color_mode="none",
            ),
            _stat(
                23,
                "Speed test ran",
                {"h": 6, "w": 4, "x": 20, "y": 87},
                description="When the latest successful speed test in each direction completed.",
                targets=[_mimir_target("att_gateway_speedtest_completed_timestamp_seconds * 1000", "{{direction}}")],
                field_config={"defaults": {"unit": "dateTimeFromNow"}, "overrides": []},
                color_mode="none",
            ),
            _row(24, "Switch (CRS310)", {"h": 1, "w": 24, "x": 0, "y": 93}),
            _stat(
                25,
                "Switch reachable",
                {"h": 4, "w": 6, "x": 0, "y": 94},
                description="The switch answers the exporter's API queries. mktxp keeps serving its own metrics while the switch is unreachable, so this checks for switch data rather than the scrape.",
                targets=[
                    _mimir_target(
                        '(max(mktxp_system_uptime{routerboard_name="CRS310"}) > bool 0) or on () vector(0)', ""
                    )
                ],
                field_config={
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                26,
                "Switch uptime",
                {"h": 4, "w": 6, "x": 6, "y": 94},
                description="Time since the switch last booted.",
                targets=[_mimir_target('max(mktxp_system_uptime{routerboard_name="CRS310"})', "")],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "orange", "value": None}, {"color": "green", "value": 3600}],
                        },
                        "color": {"mode": "thresholds"},
                        "unit": "s",
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                27,
                "Switch CPU load",
                {"h": 4, "w": 6, "x": 12, "y": 94},
                description="",
                targets=[_mimir_target('max(mktxp_system_cpu_load{routerboard_name="CRS310"})', "")],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 80}],
                        },
                        "color": {"mode": "thresholds"},
                        "unit": "percent",
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _stat(
                28,
                "Switch CPU temperature",
                {"h": 4, "w": 6, "x": 18, "y": 94},
                description="",
                targets=[_mimir_target('max(mktxp_system_cpu_temperature{routerboard_name="CRS310"})', "")],
                field_config={
                    "defaults": {
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 70}],
                        },
                        "color": {"mode": "thresholds"},
                        "unit": "celsius",
                    },
                    "overrides": [],
                },
                color_mode="background",
            ),
            _timeseries(
                29,
                "Switch port traffic",
                {"h": 8, "w": 12, "x": 0, "y": 98},
                description="Traffic on each switch port, as the switch counts it. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                targets=[
                    _mimir_target(
                        'max by (name) (rate(mktxp_interface_rx_byte_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) * 8',
                        "{{name}} in (${traffic_scale})",
                    ),
                    _mimir_target(
                        'max by (name) (rate(mktxp_interface_tx_byte_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) * 8',
                        "{{name}} out (${traffic_scale})",
                        "B",
                    ),
                ],
                field_config={
                    "defaults": {
                        "unit": "bps",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "scaleDistribution": {"type": "linear"},
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": "/ [(]Log[)]$/"},
                            "properties": [
                                {
                                    "id": "custom.scaleDistribution",
                                    "value": {"type": "symlog", "log": 10, "linearThreshold": 1},
                                }
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                30,
                "Switch port errors and drops",
                {"h": 8, "w": 6, "x": 12, "y": 98},
                description="Receive and transmit errors plus drops on each switch port, per 5 minutes.",
                targets=[
                    _mimir_target(
                        'max by (name) (increase(mktxp_interface_rx_error_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_rx_drop_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_tx_error_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m])) + max by (name) (increase(mktxp_interface_tx_drop_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[5m]))',
                        "{{name}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                31,
                "Switch port link flaps",
                {"h": 8, "w": 6, "x": 18, "y": 98},
                description="Times each switch port's link went down, per hour.",
                targets=[
                    _mimir_target(
                        'max by (name) (increase(mktxp_link_downs_total{routerboard_name="CRS310",name=~"ether.*|sfp.*"}[1h]))',
                        "{{name}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "description": "Link state of each switch port.",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 106},
                "id": 32,
                "title": "Switch port links",
                "type": "state-timeline",
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": "max by (name) "
                        '(mktxp_interface_running{routerboard_name="CRS310",name=~"ether.*|sfp.*"})',
                        "legendFormat": "{{name}}",
                        "refId": "A",
                        "range": True,
                    }
                ],
                "fieldConfig": {
                    "defaults": {
                        "mappings": [
                            {
                                "type": "value",
                                "options": {
                                    "0": {"text": "Down", "color": "red"},
                                    "1": {"text": "Up", "color": "green"},
                                },
                            }
                        ],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                        },
                        "color": {"mode": "thresholds"},
                        "custom": {"fillOpacity": 80, "lineWidth": 0},
                    },
                    "overrides": [],
                },
                "options": {
                    "showValue": "never",
                    "mergeValues": True,
                    "alignValue": "left",
                    "rowHeight": 0.8,
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                    "tooltip": {"mode": "single", "sort": "none"},
                },
            },
            _timeseries(
                33,
                "Switch temperatures",
                {"h": 8, "w": 12, "x": 12, "y": 106},
                description="Temperatures the switch reports.",
                targets=[
                    _mimir_target('max(mktxp_system_cpu_temperature{routerboard_name="CRS310"})', "CPU"),
                    _mimir_target('max(mktxp_system_board_temperature1{routerboard_name="CRS310"})', "board", "B"),
                    _mimir_target('max(mktxp_system_phy_temperature{routerboard_name="CRS310"})', "PHY", "C"),
                ],
                field_config={
                    "defaults": {
                        "unit": "celsius",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                    },
                    "overrides": [],
                },
            ),
            {
                "datasource": "${DS_LOKI}",
                "description": "The switch's own log, sent by syslog.",
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 114},
                "id": 34,
                "title": "Switch log",
                "type": "logs",
                "targets": [
                    {
                        "datasource": "${DS_LOKI}",
                        "editorMode": "code",
                        "expr": '{job="home-switch", host!="atlas"}',
                        "queryType": "range",
                        "refId": "A",
                    }
                ],
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "sortOrder": "Descending",
                    "enableLogDetails": True,
                    "dedupStrategy": "none",
                    "prettifyLogMessage": False,
                    "showCommonLabels": False,
                    "showLabels": False,
                },
            },
            _row(35, "Home nodes", {"h": 1, "w": 24, "x": 0, "y": 124}),
            _timeseries(
                36,
                "Home node LAN throughput",
                {"h": 8, "w": 12, "x": 0, "y": 125},
                description="Traffic on each home node's physical NIC. Traffic scale selects linear or base-10 symlog; the log view remains linear below 1 bit/s so zero stays visible.",
                targets=[
                    _mimir_target(
                        'rate(node_network_receive_bytes_total{job="node-exporter",device=~"en.*|eth.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} {{device}} in (${traffic_scale})",
                    ),
                    _mimir_target(
                        'rate(node_network_transmit_bytes_total{job="node-exporter",device=~"en.*|eth.*"}[5m]) * 8 * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} {{device}} out (${traffic_scale})",
                        "B",
                    ),
                ],
                field_config={
                    "defaults": {
                        "unit": "bps",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                            "scaleDistribution": {"type": "linear"},
                        },
                        "min": 0,
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byRegexp", "options": "/ [(]Log[)]$/"},
                            "properties": [
                                {
                                    "id": "custom.scaleDistribution",
                                    "value": {"type": "symlog", "log": 10, "linearThreshold": 1},
                                }
                            ],
                        }
                    ],
                },
            ),
            _timeseries(
                37,
                "Home node NIC errors and drops",
                {"h": 8, "w": 6, "x": 12, "y": 125},
                description="Receive and transmit errors plus drops on each home node's physical NIC, per 5 minutes.",
                targets=[
                    _mimir_target(
                        'sum by (instance, device) (increase(node_network_receive_errs_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_transmit_errs_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_receive_drop_total{job="node-exporter",device=~"en.*|eth.*"}[5m])+increase(node_network_transmit_drop_total{job="node-exporter",device=~"en.*|eth.*"}[5m])) * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} {{device}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                38,
                "Home node NIC link flaps",
                {"h": 8, "w": 6, "x": 18, "y": 125},
                description="Carrier changes (cable or switch port going down and up) on each home node's physical NIC, per hour.",
                targets=[
                    _mimir_target(
                        'increase(node_network_carrier_changes_total{job="node-exporter",device=~"en.*|eth.*"}[1h]) * on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{job="node-exporter",nodename=~"wyrm2|optiplex"})',
                        "{{nodename}} {{device}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _row(39, "Outage buffering (home Alloy)", {"h": 1, "w": 24, "x": 0, "y": 133}),
            _timeseries(
                40,
                "Remote-write delay per home node",
                {"h": 8, "w": 8, "x": 0, "y": 134},
                description="How far each home Alloy's delivered data trails what it has collected. It is reported by the Alloy itself, so an outage shows up here only once its backlog is replayed.",
                targets=[
                    _mimir_target(
                        'max by (node) (clamp_min(max by (pod) (prometheus_remote_storage_highest_timestamp_in_seconds{job="alloy-node"}) - max by (pod) (prometheus_remote_storage_queue_highest_sent_timestamp_seconds{job="alloy-node"}), 0) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        "{{node}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "s",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                41,
                "Samples waiting to send",
                {"h": 8, "w": 8, "x": 8, "y": 134},
                description="Samples each home Alloy holds in its send queue.",
                targets=[
                    _mimir_target(
                        'max by (node) (max by (pod) (prometheus_remote_storage_samples_pending{job="alloy-node"}) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        "{{node}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _timeseries(
                42,
                "Series in WAL",
                {"h": 8, "w": 8, "x": 16, "y": 134},
                description="Active series in each home Alloy's write-ahead log.",
                targets=[
                    _mimir_target(
                        'max by (node) (max by (pod) (prometheus_remote_write_wal_storage_active_series{job="alloy-node"}) * on (pod) group_left (node) max by (pod, node) (kube_pod_info{namespace="monitoring",created_by_name="alloy-node",node=~"wyrm2|optiplex"}))',
                        "{{node}}",
                    )
                ],
                field_config={
                    "defaults": {
                        "unit": "short",
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                    },
                    "overrides": [],
                },
            ),
            _row(54, "Outbound application probes", {"h": 1, "w": 24, "x": 0, "y": 142}),
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "type": "timeseries",
                "fieldConfig": {
                    "defaults": {
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                        "unit": "percent",
                        "max": 100,
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"},
                },
                "description": "Failure percentage among probe samples recorded in the preceding 5 minutes, split by "
                "source_node and target. 0% means all recorded probes passed. No data means no probe samples "
                "were available and is not treated as success.",
                "gridPos": {"h": 8, "w": 8, "x": 0, "y": 143},
                "id": 55,
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": '100 * (1 - avg_over_time(probe_success{job="monitoring/home-application-probes"}[5m]))',
                        "legendFormat": "{{source_node}} → {{target}}",
                        "refId": "A",
                        "range": True,
                    }
                ],
                "title": "Observed probe failures",
            },
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "type": "timeseries",
                "fieldConfig": {
                    "defaults": {
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                        "unit": "s",
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"},
                },
                "description": "Blackbox exporter probe duration in seconds for each source_node and target. HTTPS duration "
                "includes DNS, TCP connection, TLS verification, and the HTTP request.",
                "gridPos": {"h": 8, "w": 8, "x": 8, "y": 143},
                "id": 56,
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": 'probe_duration_seconds{job="monitoring/home-application-probes"}',
                        "legendFormat": "{{source_node}} → {{target}}",
                        "refId": "A",
                        "range": True,
                    }
                ],
                "title": "Probe duration",
            },
            {
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "type": "timeseries",
                "fieldConfig": {
                    "defaults": {
                        "custom": {
                            "drawStyle": "line",
                            "lineWidth": 2,
                            "fillOpacity": 10,
                            "spanNulls": False,
                            "showPoints": "never",
                        },
                        "min": 0,
                        "unit": "percent",
                        "max": 100,
                    },
                    "overrides": [],
                },
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"},
                },
                "description": "Observed probe samples as a percentage of the 30 expected samples in each 5-minute window "
                "(10-second scrape interval). Missing series remain No data; reduced values show missing "
                "observations.",
                "gridPos": {"h": 8, "w": 8, "x": 16, "y": 143},
                "id": 57,
                "targets": [
                    {
                        "datasource": {"type": "prometheus", "uid": "mimir"},
                        "editorMode": "code",
                        "expr": '100 * count_over_time(probe_success{job="monitoring/home-application-probes"}[5m]) / 30',
                        "legendFormat": "{{source_node}} → {{target}}",
                        "refId": "A",
                        "range": True,
                    }
                ],
                "title": "Probe sample coverage",
            },
        ],
        "refresh": "1m",
        "schemaVersion": 41,
        "tags": ["home-network"],
        "time": {"from": "now-24h", "to": "now"},
        "timezone": "browser",
        "title": "Home network overview",
        "uid": "home-network-overview",
        "templating": {
            "list": [
                {
                    "name": "traffic_scale",
                    "label": "Traffic scale",
                    "type": "custom",
                    "query": "Linear,Log",
                    "current": {"text": "Linear", "value": "Linear"},
                    "options": [
                        {"text": "Linear", "value": "Linear", "selected": True},
                        {"text": "Log", "value": "Log", "selected": False},
                    ],
                    "multi": False,
                    "includeAll": False,
                    "hide": 0,
                    "skipUrlSync": False,
                }
            ]
        },
    }


def main() -> None:
    print(json.dumps(dashboard(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
