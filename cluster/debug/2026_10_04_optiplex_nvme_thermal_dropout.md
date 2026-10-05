# `optiplex` NVMe drops off the bus after a thermal spike — 2026-10-04

**Status:** open. The node is `NotReady` and needs a power cycle. The failure signature
(NVMe temperature spike, then the drive disappears while the rest of the host keeps
running) matches 2026-09-23. Why the drive drops is not established: the kernel message
from the moment of failure was not retrieved.

## Summary

The home worker `optiplex` (Dell OptiPlex 7060 Micro, SK hynix BC511 256 GB NVMe, Talos)
did not hang and did not lose its network. Its NVMe overheated during a burst of load and
stopped answering. Talos runs from RAM, so the host, `node_exporter` and the Talos API kept
answering while every write to disk failed. The kubelet then starved and Kubernetes marked the
node `NotReady`.

## Observations

Times are UTC on 2026-10-04 unless noted. Metrics are from Mimir (`node_exporter` on
`10.42.0.18:9100`); logs from Loki (`node="optiplex"`) and `talosctl dmesg`.

- **22:54–23:16:30:** NVMe temperature rose 55 → 65 → 76 (23:12) → 86 °C on the composite
  sensors and 97.85 °C on the hottest one (23:15–23:16). The drive reports a warning threshold
  of 83.85 °C and a critical threshold of 84.85 °C. CPU package temperature stayed at or below
  73 °C throughout.
- **Pod history on the node** (`kube_pod_info`, which keeps deleted pods): Tofu runner pods
  (`flux-system/*-tf-runner`, created per reconcile by tofu-controller) arrived in waves:
  22:42 (10 pods; the 22:43 52 MB/s write burst), 22:53 (`haku-state-tf-runner`; the 22:54
  20 MB/s burst and 65 °C bump), 23:07 (4 pods), then about nine new pods in each of the minutes
  23:12, 23:13 and 23:14 and again at 23:16. The same runners came back repeatedly:
  `dns-records-tf-runner` appears in five incarnations between 22:42 and 23:16. Up to nine pods
  sat in `ContainerCreating` at once. The runner pods carry no node selector, so they can land
  here.
- **23:11–23:16 disk I/O on `nvme0n1`** (`node_exporter`, against a baseline of about
  0.14 MB/s, under 2 % utilisation and 2 ms per write): writes rose to 26–35 MB/s and 290–460
  write IOPS, device utilisation 63 % → 92 %, average write latency 0.1 s (23:11) → 3 s (23:13)
  → 8.65 s (23:15), CPU `iowait` 22 → 42 %, I/O pressure `some` 0.41–0.69 and `full` 0.28–0.49.
  Reads stayed under 20 kB/s. Load average rose from about 2 to 28 (peak 45 at 23:14), CPU to
  82 % busy, receive traffic to about 8 MB/s.
- **Earlier bursts the same evening:** at 22:43 writes peaked at 52 MB/s (temperature barely
  moved, 58 °C), and at 22:54 at 20 MB/s with 0.58 s write latency and a temperature bump to
  65 °C that settled back to 58 °C within three minutes.
- **Who wrote (cAdvisor `container_fs_writes_bytes_total`):** the `/kubepods/besteffort` cgroup
  wrote 35–38 MB/s at 23:11:30–23:13:30, which accounts for the burst. Per pod, increase over the
  10 minutes to 23:17:30, counting only from each pod's first sample (so a lower bound):
  `dns-records-tf-runner` 3.1 GB, `litellm-keys-tf-runner` 0.68 GB,
  `github-secrets-sync-tf-runner` 44 MB, `flux-webhook-token-tf-runner` 26 MB,
  `forgejo-images-tf-runner` 22 MB; everything else on the node, including Home Assistant, the
  CPAP gateway VM (about 0.1 MB/s steady) and `alloy` (0.15 MB/s), was below 0.2 MB/s. CPU in the
  same window was spread across `alloy`, `github-secrets-sync-tf-runner`, Cilium and the CPAP VM;
  no single workload was CPU-bound.
- **23:16:30:** the NVMe `hwmon` series stops and never returns. The CPU series continues.
- **23:17:28 onward:** kubelet events `MountVolume.SetUp failed … input/output error`.
- **23:46:30:** node marked `Unknown`/`NotReady`.
- **05:53–06:13 (next day):** `nvme nvme0: Identify namespace failed (-5)` once a minute;
  Talos `LogPersistenceController` and `siderolink.ManagerController` fail with
  `input/output error`. The host answers ping on the LAN and over Nebula and keeps the Talos API
  port (50000) open, with about 51 h of continuous uptime.

Not retrieved: the kernel's own NVMe message at the time of failure (controller timeout,
reset, removal). The Talos ring buffer starts at 05:53 the next day; Loki should hold it, but
the search used (`controller` in the pattern) was swamped by Talos controller-runtime lines.

## Earlier occurrences

Boot times from `node_boot_time_seconds`: 07-18, 09-08, 09-18 21:56, 09-20 04:37,
09-20 19:33, 09-25 06:29, 10-03 03:13.

- **2026-09-23:** the NVMe read 87–88 °C at 22:40–23:10, then its sensor vanished. The CPU
  series continued until 09-24 07:40, then everything went silent until the 09-25 06:29
  reboot. Same signature as today, while the machine was in the wiring closet.
- **2026-09-05 and 2026-09-16/18:** the NVMe was about 53 °C before the gaps, and both have a
  network explanation on file (see _Earlier notes_). These are not this failure.
- **NVMe peaks of 80 °C or more that did not kill it:** 09-05, 09-11 (86), 09-25, 09-26,
  09-29, 10-01, all in the closet. Daily mean temperature is 50–55 °C both in the closet and
  since the move under the TV on 10-02, so ambient has not shown up as the main driver; the
  spikes are load-driven. The cabinet may still reduce the margin; that is untested.

## Earlier notes

Every earlier note on this machine's outages covers a network cause. None covers an NVMe
dropout, and the 2026-09-23 event is not recorded anywhere.

- [`debug/home_network_connectivity/`](../../debug/home_network_connectivity/README.md):
  2026-09-17 outage, gateway fiber bend-radius hypothesis; the optical-telemetry follow-ups are
  still open there.
- 2026-09-05: a failed managed switch took down the home LAN, diagnosed from link-light and
  cabling evidence; it has since been replaced by the MikroTik CRS310, whose setup (static
  management address, SNMP into Prometheus) is unfinished.
- [`2026_09_19_optiplex_talos_upgrade_config_drift.md`](2026_09_19_optiplex_talos_upgrade_config_drift.md):
  Talos upgrade plan drift, not an outage.

Some operator notes describe `optiplex` as intentionally offline from 2026-09-18 until it moved
under the TV on 2026-10-02. Mimir shows it booted 09-18 21:56 and reporting continuously from
then, except 09-24 07:40 to 09-25 06:40 and the two reboots on 09-20. The two accounts are not
reconciled here.

## What is not known

- Why the runner pods were recreated repeatedly (five incarnations of `dns-records-tf-runner`
  in 35 minutes), and what inside them wrote gigabytes. Repeated `tofu init` provider downloads
  into the pod's scratch volume fit the numbers but were not observed.
- Why 30 MB/s was enough to reach 97 °C, and why write latency reached seconds. Both fit a
  drive that was thermally throttling and then stalling, but that is not shown. A 256 GB OEM
  drive with no heatsink in an enclosed micro chassis is a candidate; its SMART data (media
  errors, thermal-throttle counters) has not been read.
- Whether a soft `talosctl reboot` brings the NVMe back, or only a power cycle does.
- Whether the Home Assistant state is safely backed up. Its volume (`local-path-home-ssd`)
  lives on this drive and is copied by VolSync to SeaweedFS. The `volsync-src-home-assistant-config-restic`
  job started at 00:17 on 10-05 and is still `Pending` (nowhere to run); the last successful
  snapshot was not checked, because the `haku` identity cannot read `ReplicationSource`.
  Check it before reformatting or replacing the drive.

## Options

In order of how cheaply they remove the trigger or the damage:

1. **Keep the burst off this node.** Give the Tofu runner pod template a node selector away
   from the `home` region (`cluster/cdk8s/terraform.py` sets none), or use memory-backed
   `emptyDir` for runner scratch. `haku-ci`'s rootless dind is the other heavy writer approved
   to run here. Also give runner pods resource requests: the burst ran in the best-effort
   cgroup, which has no I/O or memory protection against the pods that matter.
2. **Cool the drive.** Fit a low-profile M.2 heatsink or thermal pad and leave the shelf open;
   moving it about 30 cm from the CRS310 is cheap and harmless but, per the temperature
   history above, unlikely to be enough alone.
3. **Early warning.** A Mimir alert on `node_hwmon_temp_celsius{chip=~".*nvme.*"}` above about
   75 °C, ahead of the 83.85 °C warning threshold, and optionally cordoning the node.
4. **Recovery watchdog.** A standard hardware or `softdog` watchdog does not help: Talos'
   `machined` keeps running and keeps feeding it while the disk is gone. What detects this
   failure is an external check ("`NotReady` for N minutes while the host still answers ping")
   followed by a power cycle through something that does not depend on this node: a smart plug,
   or Intel AMT if this unit's vPro is enabled (unchecked). Home Assistant runs on this node,
   so it cannot be the actuator.
5. **Throttle the drive.** Capping the NVMe's maximum power state (kernel parameter or
   `nvme set-feature`) should lower peak heat at the cost of write speed. Unverified on this
   drive; try it only if 1–3 are not enough.
6. **Reduce the node's role.** It is the only home worker that can host the USB WiFi stick
   (CPAP gateway) and the pinned Home Assistant volume; everything else running here is
   scheduling that could go elsewhere.

## Open infrastructure

- **Out-of-band telemetry.** Today reading this node's logs or the CRS310 needs an approved SSH
  session to `wyrm2`. The Talos API is reachable over Nebula (`10.42.0.18:50000`), so a
  read-only in-cluster path (Talos `os:reader`) is possible. The CRS310 has no telemetry path
  into Prometheus yet.
- **Per-pod I/O alerting.** The data to find this already exists: node-level disk and pressure
  series from `node_exporter` and per-container writes from cAdvisor. What is missing is an alert
  on sustained device write latency or I/O pressure on `optiplex`, which would have fired at 23:13,
  three minutes before the drive disappeared.
