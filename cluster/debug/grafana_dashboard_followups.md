# Grafana dashboard audit follow-ups

A browser audit on 2026-11-16 opened all 26 dashboards returned by Grafana
search with live data, using default selections and ranges except for the
user-linked views. Collapsed rows were opened as well. The following issues
still need investigation after the associated collection changes roll out;
this is not an exhaustive test of every variable combination or time range.

## CloudNativePG dashboard selectors

The imported CloudNativePG dashboard's operator-namespace variable queries
`controller_runtime_active_workers` without restricting it to the CNPG
operator, so an unrelated controller namespace was selected by default. The
operator is in `cnpg-system`; a PodMonitor has since been added and its target
was observed up with controller metrics in Mimir. Verify the imported panels
with `cnpg-system` selected, then make the default and choices specific to the
CNPG operator. Its instance dropdown also included deleted `attic-db-3` over a
seven-day range, even though only `attic-db-1` and `attic-db-2` were current.
Choose current instances without losing useful historical queries.

## Interface flap dashboard

During the audit the default Wyrm2 instance variable resolved to an empty regex
because its local node-exporter scrape was down, and the carrier timeline
reported that its data lacked a time field. Several Loki log queries timed out
over 24 hours. The Wyrm2 firewall fix is merged but the host had not applied
it at the last check; after rollout, verify the scrape and timeline, then
reproduce the Loki timeouts independently and choose a practical default range
or query. Do not present missing carrier data as an inactive interface.

The separate active investigations cover [Node Exporter Full](https://github.com/agentydragon/ducktape/pull/17014),
[Authentik worker metrics](authentik_worker_telemetry.md), and
[CI workflow PR associations](https://github.com/agentydragon/ducktape/pull/17013).
Remove this note when these remaining selector and interface issues are fixed.
