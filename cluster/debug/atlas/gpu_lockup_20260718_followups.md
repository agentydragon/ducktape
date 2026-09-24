# RTX 5090 fall-off-the-bus on wyrm2 — going-forward plan (2026-07-18)

Context: recurring **Xid 79 "GPU has fallen off the bus"** on the passed-through RTX 5090s
(see <atlas/gpu_lockup_20260718/README.md>, <atlas/gpu_lockup_20260417/README.md>,
<atlas/wyrm_gpu_lockup.md>). Each incident ends the same way: a GPU stops answering PCIe
(host config space → all `0xff`), the guest wedges, and only a host reboot (Xid 154 = Node
Reboot Required) recovers it. Two workstreams: **detect + auto-collect diagnostics** (so we
get data and an alert instead of discovering a silently-wedged VM hours later), and
**reduce how often it happens**.

## A. Detection + automatic diagnostic capture ("collect diag")

The hard constraint: after Xid 79, `nvidia-smi` and `nvidia-bug-report.sh` **hang** on the
dead GPU. Any capture must use `nvidia-bug-report.sh --safe-mode` and wrap every GPU query
in `timeout`. Capture the moment the Xid fires — waiting until the node wedges loses the
window.

1. **Guest-side Xid watchdog on wyrm2** (systemd, journald-triggered). Match kernel log for
   `Xid .* 79`, `Xid .* 154`, or `fallen off the bus`; on match immediately run a bounded
   capture (`nvidia-bug-report.sh --safe-mode`, `timeout 5 nvidia-smi -q`, `dmesg -T` tail,
   `/sys/.../current_link_speed`, PCI config hexdump) into the virtiofs `code`/`tank` share
   so it survives the guest dying, and fire an alert (telegram/ntfy). Reuse
   <atlas/wyrm2-diagnose.sh> as the capture body; make it `--safe-mode` aware.
2. **Host-side wedge detector on atlas** (systemd timer, ~1–2 min). For each passed-through
   GPU, read `/sys/bus/pci/devices/<addr>/config` (all `0xff` ⇒ off the bus) and probe VM
   110 liveness (`qm guest cmd 110 ping` + `screendump` surface with timeouts). On a
   confirmed wedge: snapshot host state (this is the data the guest can't report because
   it's already dead) and alert. This is the layer that would have paged today instead of
   the VM sitting wedged for ~21h.
3. **Alerting via existing stack.** wyrm2 goes k8s `NotReady` on these events — add an
   Alertmanager rule so it surfaces through the normal channel.

## B. Prevention (reduce Xid 79 frequency)

Xid 79 is a hardware event (power delivery, thermal, PCIe signal integrity), not a driver
bug. Two commonly-cited _software_ levers are **already ruled out here** (see below). The
power budget makes power a credible suspect with a cheap, non-invasive test
(power-limiting) — but it is not confirmed, so run section A (detection + fast recovery) in
parallel regardless: the fall-off may persist.

**Already done / not a fault (do not re-propose):**

- **ASPM is already disabled.** `pcie_aspm=off` is on the atlas kernel cmdline and every
  link reports `LnkCtl: ASPM Disabled` (both GPUs + both root ports). The sysfs policy
  reads `[default]` because the cmdline flag force-locks it. Not a remaining lever.
- **The x8 link width is architectural, not degraded.** atlas is a Ryzen 9 9950X3D (AM5);
  the CPU x16 PEG bifurcates **x8/x8** across the two GPU root ports (`00:01.1`, `00:01.3`,
  each `LnkCap: Width x8`). x8 per GPU is the hardware ceiling — there is no x16 to
  recover. The transient `2.5 GT/s` seen while wedged was idle link-speed scaling; released
  and idle the display GPU reads `Speed 32GT/s` (Gen5). No link fault to chase.

### What the telemetry shows about _this_ event

The `gpu-monitor` poller (30 s CSV, `/var/log/gpu-monitor/`, enabled on wyrm2) captured the
run-up. The solid, config-independent fact:

- **It fell off at idle.** The compute GPU (index 1) sat at **P8, 11–13 W, 38 °C, PCIe gen 1
  for the entire 90 min before 02:09:53** — then dropped off the bus. A 12 W idle card is
  not spiking the PSU, so **this event was not a power/transient excursion**, and power-
  limiting would not have prevented it.

That points away from power draw and toward a **PCIe-link / idle power-state / marginal-
electrical** cause on `03:00`.

**Framing (operator's read, 2026-07-18):** these 5090s have been **chronically wonky all
along** — long before the move (Jan–Apr manual notes, the pre-move 05-10 poller event). What
changes over time is _how_ the fault surfaces as drivers/configs/instrumentation change, not
a steadily-worsening new fault. So do **not** over-read the recent cluster of poller events
as "degradation" or blame the relocation; treat it as one long-standing flaky-GPU problem
observed through improving instrumentation.

### Power budget — assessed, now demoted for this event

Kept for reference; the idle-failure above makes it an unlikely cause here.

- **Board:** ASUS ProArt X870E-Creator WiFi (AM5). **CPU:** Ryzen 9 9950X3D.
- **PSU:** single **1600 W ATX 3.0** unit (Amazon ASIN `B0C571LRNB`), each card on an
  **independent 12V-2x6 cable to a separate PSU port** (confirmed — no daisy-chain).
- **GPUs:** 2× RTX 5090, 575 W TDP each = **1150 W**; full system sustained ≈ **~1500 W ≈
  94 % of 1600 W** — a tight _sustained_ margin. Transients are real (TechPowerUp measured
  **901 W sub-1 ms** per card [TPU]) but ATX 3.0/3.1 PSUs are spec'd to absorb 200 % rated
  for ~100 µs [ATX3.1]. Plausible under heavy dual-GPU load — but not for a 12 W idle
  fall-off.

Sources: [TPU] <https://www.techpowerup.com/331542/geforce-rtx-5090-power-excursions-tested-can-spike-to-901w-under-1ms>;
[ATX3.1] Intel ATX 3.1 / PCIe 5.1 power-excursion spec (200 % rated for 100 µs).

**Levers, reranked:**

1. **Reseat the compute GPU (`03:00`) + re-latch its 12V-2x6 connector** next time the case
   is open — cheap, and the RTX 5090 connector is seating-sensitive. Framed as ruling out a
   marginal contact, _not_ as a move-caused regression. If it recurs after a clean reseat,
   suspect the card itself (RMA) or the slot.
2. **Power-limit the 5090s** — `nvidia-smi -pl`, persistent. Demoted to a harmless secondary
   given the idle failure; keep it only if heavy-load events also show up in the metrics.
3. **Fix the run-harness busy-spin bug** (E9 `llama-cli` EOF read-loop, ~1h48m at 103 % CPU;
   <atlas/gpu_lockup_20260718/README.md>) — hygiene, unrelated to the idle fall-off.

## C. Quantifying frequency + telemetry (data sources)

Goal: characterize these events over time (power/temp/PCIe leading in, event frequency). The
system journals are too shallow for this, **but the `gpu-monitor` CSV is a real ~3-month
archive** — so a partial "before" _does_ exist; it's just local-only and needs moving into
Mimir to be useful long-term.

**Best existing source — the `gpu-monitor` CSV (this is the real "before").** The poller
(`/var/log/gpu-monitor/telemetry-*.csv`, 30 s, enabled) has **~3 months of GPU telemetry
(2026-04-17 → 07-18, survived the mothball)** _and_ writes a `NVIDIA-SMI FAILED` row on each
lockup — a timestamped, GPU-specific event log. It is local-only and a **lower bound** (hard
wedges may die before writing a marker), but it's far better than the journals. Poller-witnessed
lockup episodes:

| Date/time              | Episode                        |
| ---------------------- | ------------------------------ |
| 2026-05-10 02:26       | 1 marker (isolated)            |
| 2026-07-06 17:54       | 1 marker                       |
| 2026-07-17 02:42–02:48 | ~6 min sustained, recovered    |
| 2026-07-17 22:27–22:36 | intermittent, recovered        |
| 2026-07-18 02:09       | terminal wedge (this incident) |

Read offline with `mount -o ro,norecovery /dev/zd320p2 /mnt` (scsi0 `vm-110-disk-1` p2 = ext4
`nixos` root); grep the CSVs for `FAILED`, or filter by `gpu_index`/timestamp for power/temp.

**Do not over-read the recent cluster as a trend.** Per operator: the GPUs have been wonky
all along (pre-move Jan–Apr notes + the 05-10 poller event); the mothball/move is not clearly
causal, and denser recent events partly reflect better instrumentation, not a worsening fault.
So there is no clean rate to compute — treat it as one long-standing flaky-GPU problem.

**Other logs (measured 2026-07-18), all too shallow for GPU history:**

- **wyrm2 guest journal** (authoritative Xid source): only **~4 days** (07-14 → 07-18),
  `SystemMaxUse`-capped; one Xid 79 in window (`2026-07-18T02:09:53`). `journalctl -D
<mnt>/var/log/journal` from the same offline mount.
- **atlas host journal:** ~5 months of _span_ but GPU content is sparse (Xid is guest-side;
  host has only proxies — vfio reset failures, `pcieport … retraining failed`).
- **PVE task logs:** ~2 weeks, low volume.
- **Loki (cluster):** ships **pod logs only — systemd journal not scraped**, so no Xid there.

**Instrument forward (the durable fix):**

1. **[DONE — formal metrics in Mimir] Replaced the local CSV poller with a Prometheus path.**
   **DCGM exporter** landed in `cluster/k8s/dcgm-exporter/` — an nvidia-runtime DaemonSet on
   wyrm2 → Alloy PodMonitor auto-discovery → **Mimir (365 d)**, with the gnetId 12239 Grafana
   dashboard. DCGM gives power/temp/clocks, **PCIe replay counters**, and **XID-as-a-metric**
   (`DCGM_FI_DEV_XID_ERRORS`) — graphable and correlatable, unlike the local CSV.
   **[TODO]** retire `nix/nixos/modules/gpu-monitor.nix` + `ducktape.gpuMonitor.enable` on
   wyrm2 once Mimir coverage is confirmed (don't lose the local CSV before then).
2. **[DONE for k8s nodes] Ship the node journals/logs to Loki** (see `cluster/k8s/TODO.md`):
   NixOS nodes via the `promtail-journal` HelmRelease (journal scrape, `node-vendor=nixos`);
   Talos nodes via `machine.logging` → the `vector-talos-logs` DaemonSet. So `Xid`/`fallen off
the bus` on wyrm2 now lands in Loki with cluster retention. **[TODO]** atlas host journal
   (off-cluster PVE host — host-level shipper over Nebula; see `cluster/k8s/TODO.md`).
3. Xid watchdog (section A) also appends every Xid 79/154 to an **append-only log** in the
   `code` virtiofs share — survives rotation _and_ the guest dying.
4. **[TODO] Scrape per-GPU AER correctable-error counts.** AER is _already active on the GPU
   endpoints_ — `/sys/bus/pci/devices/0000:0X:00.0/aer_dev_correctable` populates on a live
   card (verified clean on `01:00.0`, 2026-07-18). Correctable-error accumulation there is the
   marginal-link canary for the PCIe/electrical hypothesis, so scrape it into Mimir (DCGM's
   PCIe metrics, or a textfile exporter reading `aer_dev_*`). Note the **AMD Zen root ports
   (`00:01.x`) do not implement AER** — no per-port counts, only the `DevSta: CorrErr+`
   summary bit. `pcie_ports=native` on the atlas cmdline is _optional_ — it would improve AER
   event logging to dmesg (→ Loki, TODO 2) but won't add root-port counters and may be noisy
   on this consumer board; try only if endpoint counts prove insufficient.
5. Any A/B on power-limiting is inherently slow at this event rate — a quiet spell is not
   evidence; lean on the DCGM metrics + AER counts + event log to characterize each event.

## Recovery runbook (until prevention lands)

1. `qm stop 110 --skiplock` (guest is unresponsive; graceful shutdown won't work).
2. Confirm the fault is host-side: `hexdump -n4 -C /sys/bus/pci/devices/0000:03:00.0/config`
   — all `0xff` ⇒ off the bus ⇒ reboot required.
3. Reboot atlas. After boot, re-check the config read is non-`ff`. **If still `ff`, do a
   full cold power cycle** — a warm reboot doesn't always re-power the card.
4. wyrm2 restarts via `onboot: 1`.
