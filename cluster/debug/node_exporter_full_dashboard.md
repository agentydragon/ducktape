# Node Exporter Full: stale default and empty rate panels

Observed 2026-11-16 against the Grafana-managed import of [Node Exporter Full](https://grafana.com/grafana/dashboards/1860-node-exporter-full/), revision 45, at Grafana UID `rYdddlPWk`. The dashboard remains imported by `cluster/cdk8s/monitoring/grafana_instance.py`; this investigation did not change its deployment.

The dashboard opens with `job=buildbuddy-kvm-worker-metrics` and a historical VM instance `10.244.9.103:9100`. The current `node_uname_info` series for `buildbuddy-kvm-worker-0` has instance `10.244.9.41:9100`; the old instance is absent from an instant query. The upstream `job → nodename → node` variables use `label_values` over the selected time range and have no pinned current values, so historical targets can become the first selected value. The default 24-hour range makes this visible when VM addresses change.

Selecting the live `node-exporter` job and OptiPlex instance `10.42.0.18:9100` does not fix the rate panels. In an authenticated browser comparison, disposable one-panel copies of **CPU Busy** used the same live selection:

| Candidate                          | Browser datasource UID | Browser PromQL range       | Grafana result             | Render         |
| ---------------------------------- | ---------------------- | -------------------------- | -------------------------- | -------------- |
| Imported mapping `Mimir`           | `mimir`                | literal `$__rate_interval` | HTTP 200, zero-field frame | N/A            |
| Corrected mapping `mimir`          | `mimir`                | literal `$__rate_interval` | HTTP 200, zero-field frame | N/A            |
| Corrected mapping and fixed window | `mimir`                | `[5m]`                     | HTTP 200, two-field frame  | 28.3% CPU Busy |

All three temporary dashboards were deleted after the comparison. The live datasource UID is `mimir`, while the import's operator input maps to its display name `Mimir`. Grafana's browser resolves that name to the live UID, so changing the input mapping alone does not repair the panels. A direct `/api/ds/query` probe for the same OptiPlex CPU expression likewise returned an empty frame with the literal macro and 61 populated time samples with `[5m]` over one hour. The browser comparison is the decisive evidence that the macro remains literal even after datasource resolution; a manual PromQL substitution would hide this failure.

Revision 45 contains 140 panels, 284 query expressions, and 124 expressions with `$__rate_interval`. The newest upstream revision 46 still has the same variable chain and uses the macro in 137 of 301 expressions across 137 panels. Revisions 40–44 also use the macro in 124 expressions. A revision bump or datasource-only edit would therefore leave the reported failure. An automated local rewrite of the imported JSON would obscure which upstream queries we own and could silently drop its broad hardware and system coverage.

The follow-up is a separately reviewed Foundation SDK replacement under `cluster/cdk8s/monitoring/dashboards/`, with explicit query and panel definitions. Before replacing the import, inventory its useful CPU, memory, filesystem, disk, network, pressure, hardware-monitoring, fan, process, and systemd coverage against existing local dashboards, and record any coverage intentionally omitted. Choose a default from **currently scraped** node-exporter series; keep the BuildBuddy VM selectable only if its distinct job is explicitly supported. Use rate windows that return data at the live scrape interval. Validate every target through Grafana's datasource API, run the packaged candidate verifier, and inspect browser screenshots for both default selection and representative nodes. Replace the imported resource only when the candidate meets those checks.

Separately, Wyrm2's merged host-firewall change had not rolled out at the last live check: local Alloy still reported `up=0` for its node-exporter pod. A dashboard migration must show that absence honestly rather than synthesize a healthy Wyrm2 series.
