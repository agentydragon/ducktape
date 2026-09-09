# Rugged power telemetry

## Goal

Make a battery-life regression explainable after the fact without making the
measurement itself a meaningful battery or storage consumer. The result is a
Rugged battery dashboard plus a reusable host-consumer surface, both backed by
low-cardinality, one-minute Mimir metrics. When a laptop is genuinely
discharging, it also preserves concise ranked process snapshots in Loki for
forensic use.

This is observability, not a policy change. In particular, do not enable TLP
or auto-cpufreq as part of this work: the existing
`power-profiles-daemon`/GNOME policy remains the single owner of CPU power
policy until the evidence points to a specific corrective change.

## What exists now

The cluster-wide `prometheus-node-exporter` DaemonSet is already scraped by
Alloy every 60 seconds and remote-written to Mimir, whose block retention is
365 days. On Rugged it exposes:

- two batteries' charge, full and design charge, voltage, current,
  temperature, cycle count, health, capacity, and charging state;
- AC and USB-C/PD online, voltage, current, and status;
- CPU governor, current/min/max frequency, CPU time, pressure, throttling,
  thermal zones, hwmon sensors, cooling-device state, disk I/O, and network
  counters; and
- Kubernetes/cAdvisor workload resource metrics.

The useful facts from the first live check are that both
`powersupplyclass` and thermal/cpufreq collectors work, while the enabled
`rapl` collector returns `node_scrape_collector_success{collector="rapl"} 0`.
The exporter is mounted on the real host `/sys`, so this must be treated as a
host kernel/firmware/powercap availability problem, not fixed by adding a
second exporter container. It currently provides no package-energy series.

`powertop` is installed on Rugged and remains valuable for interactive,
time-bounded wakeup diagnosis. It is not the continuous collector: a PowerTOP
run takes its own measurement interval, can be comparatively intrusive, and
its tabular process/device output is not a stable Prometheus schema.

The host also runs the local Arc and NPU inference facilities, and intentionally
pins the Foxconn WWAN PCI device's runtime PM to `on` to avoid a firmware wedge.
Those are especially important dashboard dimensions: the latter is a deliberate
battery trade-off, not a metric or configuration bug.

## Design

### 1. Establish hardware truth and repair RAPL only when possible

Before adding metrics, use the approved read-only Rugged execution path to
capture the following in a short evidence note under `debug/rugged/`:

1. `/sys/class/powercap`, `/sys/devices/virtual/powercap`, loaded
   `intel_rapl*` modules, and kernel messages referring to RAPL/powercap.
2. `turbostat --debug` (or the least invasive read-only mode supplied by the
   pinned Nix package) while on AC and while discharging, recording whether
   package watts, energy counters, and C-state residency are actually exposed.
3. Intel Lunar Lake kernel/firmware support status for this exact host. Do not
   infer support from another Intel generation.

Decision:

- If the kernel exposes powercap energy counters, repair the smallest missing
  prerequisite (normally a module/kernel configuration) and prove it by
  `node_rapl_*` samples in Mimir. Keep the existing node-exporter collector;
  it is the correct reusable path.
- If powercap is absent or the platform does not expose RAPL, keep the exporter
  health metric as an explicit known-unavailable signal. Do not synthesize
  watts from battery percentage, and do not retain a failing collector merely
  as though it were power data. Use battery-energy slope as the system-level
  discharge measure instead; document its limitations while charging and near
  charge thresholds.

This investigation must also establish which of `energy_now`/`power_now`
versus charge/current attributes the Dell battery driver exposes. Prefer the
native energy/power fields if available; otherwise calculate neither watts nor
remaining energy in the exporter.

### 2. Reuse node-exporter for static host state

Extend the existing HelmRelease only as needed to mount a _read-only host_
textfile directory and set
`--collector.textfile.directory=/host/var/lib/rugged-power-exporter` (the
current textfile collector has no host directory configured). Do not run a
second node-exporter.

Add a Rugged-only Nix module, for example
`nix/nixos/hosts/rugged/power-telemetry.nix`, which owns:

- the atomic `.prom` writer directory and its permissions;
- a `rugged-power-telemetry.service` plus one-minute timer, aligned with the
  Alloy scrape cadence but randomized slightly to avoid scrape/write races;
- a strict, bounded parser of sysfs and stable command output; and
- an explicit feature flag in the Rugged host configuration, so it is not
  silently installed on every NixOS workstation.

Write a temporary file then rename it to `rugged_power.prom`; never let the
node-exporter scrape a partial file. On collection failure, emit a dedicated
success/timestamp metric and preserve the last good file rather than emitting
fictional zeroes. Unit hardening should use a read-only root filesystem and
the minimal `ReadOnlyPaths` needed for `/sys`, `/proc`, and the output
directory; it must not gain network access.

Export only fixed-label, low-cardinality state:

| Area             | Metrics / source                                                                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Battery          | current native battery/charger state plus an explicit aggregate available-energy metric only if the kernel exposes energy units safely                       |
| Policy           | PPD active profile, CPU EPP, Intel pstate min/max performance, turbo state, and active CPU governor                                                          |
| Display          | backlight actual/max brightness; do not attempt per-app display attribution                                                                                  |
| Radios           | Wi-Fi/WWAN/Bluetooth rfkill and link/connection state; WWAN PCI runtime-PM control and runtime status                                                        |
| PCI / runtime PM | a small allowlist: GPU, Wi-Fi, WWAN, NVMe, and sensor hub `power/control`, runtime status, and active/suspended-time counters                                |
| Accelerators     | DRM GPU runtime-PM state and GPU frequency if the `xe` sysfs ABI exposes it; NPU device-present/runtime state; the two local-Ollama service/container states |
| Thermal          | reference existing node-exporter temperature/fan/cooling metrics rather than duplicate them                                                                  |

Metric labels must be stable device classes or PCI BDFs from a checked-in
allowlist. Never place a full sysfs path, serial number, SSID, command line,
or PID into Mimir labels.

### 3. Attribute software consumers without unbounded cardinality

Create a shared Nix module for the packaged `process-exporter`, enabled first
on both Rugged and Wyrm2, rather than implementing `/proc` parsing for stable
workloads. Have it run as root only because I/O accounting under
`/proc/<pid>/io` needs it; bind it to loopback, and add a narrow
ServiceMonitor/host scrape path through the existing Alloy discovery setup.

Its configuration should contain an intentionally short, reviewed group list:

- GNOME shell and desktop/user-session infrastructure;
- browser families, terminal/editor/agent families, and the local inference
  processes/services;
- Podman/containerd/Kubernetes processes; and
- the WWAN/NetworkManager stack.

Disable per-thread metrics, avoid PID/start-time/cmdline-derived group names,
and use `remove-empty-groups`. Store CPU time, RSS/PSS where available, block
read/write counters, faults, context switches, and process count. Grafana can
then rank `rate(...)` and current memory over this bounded group set for any
historical window.

Do **not** configure process-exporter to emit every executable or each PID:
that makes a 365-day Mimir series set both noisy and unbounded, and command
lines may leak sensitive workspace or document paths.

For the user's requested "what was top right then?" answer, add a separate
forensic path. Enable the drain gate only on Rugged; make the bounded
process-snapshot mechanism reusable on Wyrm2 for explicit operator-triggered
captures or a later host-appropriate trigger:

1. When on battery and the smoothed discharge rate crosses a documented
   threshold for at least five minutes, start a bounded capture for a maximum
   of 30 minutes. Hysteresis and a cooldown prevent flapping.
2. Every five minutes during that capture, collect top ten by interval CPU,
   RSS, read/write I/O, and wakeup/context-switch proxy. Use process basename,
   UID class, PID, and values in a structured journal message; omit argv and
   environment. PID is acceptable in a Loki log record, not a metric label.
3. The existing NixOS-node journal promtail DaemonSet forwards that journal to
   Loki. Verify the exact unit is included, then use Loki's 90-day retention
   as the forensic window. It complements—not replaces—the year of bounded
   metrics.

The first version may use `pidstat` for interval CPU/I/O and `ps` for RSS if
the pinned packages make their output stable. If their runtime overhead is
measurable, replace only this transient sampler with a small `/proc` reader;
do not replace process-exporter.

### 4. PowerTOP as a controlled capture, not telemetry

Add an approved manual command/service that writes a timestamped PowerTOP CSV
or text report to a protected local diagnostic directory, runs for a bounded
20–60 seconds, and records its metadata in journald. It must be explicitly
operator-triggered through the existing inspection/host-exec policy—not a
background timer. Test the exact `powertop` flags and privilege requirements
on Rugged before granting them in `nix/lib/inspection-commands.nix`.

This report is the escalation tool for high wakeups, device power-management
advice, and C-state residency. It is not uploaded wholesale automatically;
the operator chooses whether a particular capture should be retained or
attached to an investigation.

### 5. Dashboard, recording rules, and alerts

Create `rugged-power.yaml` alongside the existing Grafana dashboards and list
it in that directory's `kustomization.yaml`. Reuse the Mimir datasource and
the node-exporter dashboard variables; scope the battery/power panels to
Rugged's current node-exporter instance rather than creating a second dashboard
framework. Make the stable process-group panels host-selectable so Wyrm2 uses
the same surface.

Panels, in order:

1. Battery energy/charge, state, AC/USB-C power, and a clearly labelled
   discharge-rate estimate that is blank while charging or state is unknown.
2. CPU package watts/energy and C-state residency only if RAPL/turbostat
   validation made them real; otherwise show "unavailable on this hardware".
3. Policy and knobs: PPD, EPP, pstate/turbo, brightness, radio state, and
   runtime-PM state—including the intentionally pinned WWAN state.
4. Thermal, fan/cooling, CPU frequency, throttling, and pressure.
5. GPU/NPU/local-inference lifecycle and their runtime-PM state.
6. Stable process-group CPU/RSS/I/O leaderboards, plus a link to filtered Loki
   top-process snapshots for drain episodes.

Add only two alerts after a one-week baseline:

- telemetry stale while the node is Ready; and
- sustained discharge materially worse than its observed idle baseline while
  unplugged, with the alert carrying the current policy/radio context.

No battery-health or drain threshold should be invented before observing a
normal battery-only baseline across several charge cycles.

## Delivery slices

1. **Evidence and RAPL decision PR:** capture the sysfs/turbostat evidence;
   fix node-exporter RAPL only if a real host prerequisite exists. Include an
   explicit unavailable outcome if not.
2. **Static power-state PR:** textfile mount plus the Rugged-only hardened Nix
   sampler, unit tests for parser/output, and Mimir proof of the new series.
3. **Software attribution PR:** shared bounded process-exporter deployment enabled
   on Rugged and Wyrm2 and its scrape wiring; separately add the Rugged
   drain-gated Loki snapshot timer after its retention and output volume are
   tested.
4. **Dashboard/rules PR:** Grafana dashboard, initially informational
   recording rules, and finally baseline-derived alerts.

Each slice is independently reviewable and deployable. Do not hold static
hardware-state telemetry behind the optional RAPL, process, or dashboard work.

## Acceptance evidence

- Nix evaluation proves the battery/power sampler is enabled only for Rugged
  while the shared process exporter is enabled on Rugged and Wyrm2; unit/timer
  hardening and node-exporter mount/flag are rendered as intended.
- The live exporter exposes a complete, atomically written textfile surface;
  Alloy has an `up` target and Mimir returns new series after a full scrape.
- A controlled AC-to-battery interval demonstrates the battery state and
  discharge calculation change without emitting false values while charging.
- A known CPU, memory, and I/O workload appears in its expected
  process-exporter group and Grafana's leaderboards.
- A forced, safe drain-capture test produces one redacted structured Loki
  record, with no command lines or secrets, and stops at its time bound.
- RAPL is either demonstrated through Mimir energy-counter deltas or shown as
  unavailable with the exact platform evidence. "Collector enabled" is not
  acceptance.
