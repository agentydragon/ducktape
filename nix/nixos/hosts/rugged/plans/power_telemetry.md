# Rugged power telemetry

## Goal

Make a battery-life regression explainable after the fact without making the
measurement itself a meaningful battery or storage consumer. Continuous metrics
must stay low-cardinality; PowerTOP and per-process captures remain deliberate,
bounded diagnostic actions rather than background telemetry.

This is observability, not a power-policy change. `power-profiles-daemon` and
GNOME remain the sole owners of CPU policy; do not add TLP or auto-cpufreq
without evidence for a specific corrective change.

## Landed

- **RAPL access:** Rugged's non-root node-exporter can read the Intel RAPL
  energy counters through a dedicated Nix-managed reader group. It retains its
  non-root UID and no added capabilities. Live Mimir evidence showed
  `node_scrape_collector_success{collector="rapl"} == 1` and increasing package,
  DRAM, core, and platform joule counters.
- **Battery and RAPL dashboard:** `Rugged Power` is GitOps-merged. It adapts
  Grafana dashboard 12542 to the actual battery, AC, and USB-PD metrics from
  Rugged and derives RAPL watts from counter rates. Package and DRAM queries
  select canonical `intel-rapl` paths, excluding identical `intel-rapl-mmio`
  series. The dashboard is awaiting Flux reconciliation; do not call it
  deployed until its `GrafanaDashboard` resource is Ready.

The existing node-exporter scrape already supplies battery capacity, current,
voltage, temperature, cycle count, health, charge state, AC/USB-PD state, CPU
frequency, thermal/hwmon/cooling state, disk I/O, network counters, and
Kubernetes/cAdvisor resource metrics at one-minute cadence through Alloy to
Mimir. PowerTOP is installed for interactive, bounded wakeup diagnosis but is
not a continuous collector.

## Remaining work

### 1. Static host power-state telemetry

Add a Rugged-only Nix module and an atomic textfile collector only for state
that node-exporter does not already expose. Extend the existing node-exporter
HelmRelease with a read-only host textfile directory; do not run a second
node-exporter.

The one-minute, slightly jittered service writes a temporary `.prom` file and
renames it atomically. On failure it emits a success/timestamp metric and
retains the last good values. It has no network access and only the minimum
read-only `/sys` and `/proc` paths plus its output directory.

Keep labels to a checked-in device-class/PCI-BDF allowlist; never emit a full
sysfs path, serial, SSID, command line, or PID as a Mimir label. Candidate
state is PPD/EPP/pstate/turbo, brightness, rfkill and link state, selected PCI
runtime-PM state (GPU, Wi-Fi, WWAN, NVMe, sensor hub), GPU/NPU service state,
and the intentionally pinned WWAN runtime-PM state. Reuse node-exporter for
thermal and cooling metrics.

Do not manufacture a battery discharge rate. First establish whether the Dell
driver exposes trustworthy native energy/power values; battery-current sign is
kernel-defined and is only a labelled electrical-power observation today.

### 2. Bounded software attribution on Rugged and Wyrm2

Deploy the packaged `process-exporter` through a shared Nix module, initially
on Rugged and Wyrm2. Keep its group list short and reviewed: desktop/session,
browser, terminal/editor/agent, local inference, container runtime/Kubernetes,
and WWAN/NetworkManager families. Disable per-thread metrics, remove empty
groups, and never create groups from PID, start time, or command line.

Expose CPU time, RSS/PSS where available, block I/O, faults, context switches,
and process count through a narrow loopback scrape path. This supports
historical Grafana leaderboards without unbounded cardinality.

For "what was top right then?", add a separate forensic capture. On Rugged,
gate a 30-minute maximum capture behind a documented sustained on-battery drain
threshold, hysteresis, and cooldown. Every five minutes, record the top ten by
interval CPU, RSS, read/write I/O, and wakeup/context-switch proxy as a
structured journal record. Omit argv and environment; a PID may appear in Loki
logs but never in metrics. Make Wyrm2 operator-triggerable, not drain-gated,
until its policy is known.

### 3. Controlled PowerTOP capture

Add an explicit inspection/host-exec command that runs PowerTOP for 20–60
seconds, writes a timestamped CSV or text report in a protected diagnostic
directory, and records metadata in journald. Test its flags and privileges on
Rugged before adding an allowlist. It is an escalation tool for wakeups,
device-PM advice, and C-state residency, not an automatic timer or an upload.

### 4. Extend the dashboard and add evidence-based alerts

After the collectors above are real, add the corresponding policy, runtime-PM,
thermal, accelerator, and process panels to `Rugged Power`; keep the
process-group surface host-selectable for Wyrm2. Only after a week of normal
battery-only baseline may we add alerts for stale telemetry and materially
worse sustained discharge. Include policy/radio context in an alert and do not
invent battery-health or drain thresholds first.

## Delivery order and acceptance

1. **Static state PR:** textfile mount, hardened Rugged module, parser/output
   tests, and Mimir proof of fresh series.
2. **Software attribution PR:** shared Rugged/Wyrm2 process-exporter and scrape
   wiring; separately add the Rugged drain-gated Loki capture after validating
   retention and output volume.
3. **PowerTOP PR:** explicit bounded command and privilege proof.
4. **Dashboard/alerts PR:** extend panels from the landed collectors, then add
   only baseline-derived rules.

For each slice, prove the full path: rendered Nix/Helm configuration, exporter
or journal output, Alloy/Mimir or Loki ingestion, and the Grafana/alert query.
Do not hold one independent slice behind another.
