# AT&T gateway exporter

Prometheus exporter for the AT&T BGW320 fiber gateway, which has no SNMP or API. It
scrapes the status pages that need no login (`sysinfo`, `broadbandstatistics`,
`fiberstat`, `lanstatistics`): uptime, WAN and PON state, WAN counters, optical
levels with the gateway's own thresholds and flags, LAN port link and errors. With
`ATT_GATEWAY_ACCESS_CODE` (the device access code on the gateway's label) it also logs in
for `nattable` (session table use, IPv4 sessions per source) and `speed` (the gateway's
latest speed test to AT&T per direction).

The gateway keeps no link event history: `events.ha` only toggles redirect notifications,
and `logs.ha` is a firewall drop log covering the last few minutes.

Pages are fetched one at a time with `ATT_GATEWAY_PAGE_GAP_SECONDS` between them, because
the gateway's web server stalls under a burst. `/metrics` serves the last parsed values,
so scrapes never reach the gateway. It has to run on the gateway's LAN.

```bash
ATT_GATEWAY_URL=http://192.168.1.254 bazel run //cluster/exporters/att_gateway:main_image_bin
curl -s localhost:9173/metrics | grep '^att_gateway_'
```

Settings: <settings.py>.
