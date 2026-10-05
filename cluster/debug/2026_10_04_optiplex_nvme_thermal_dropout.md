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
- **Pod history** (`kube_pod_created`/`kube_pod_info`, which keep deleted pods): tofu-controller
  starts a new runner pod, named `<module>-tf-runner`, for every reconcile of each of the 19
  `Terraform` objects (15 min interval; `infra-drift` 6 h), so about 76 launches an hour is the
  designed rate. 160 pod incarnations were created between 22:00 and 00:10, which is that rate.
  They arrive synchronised: 12 pods in the same minute at 22:11, 22:41 and 23:02. At 23:11–23:15
  there were 9, 11, 8, 4 and 6 creations in consecutive minutes (38 in five minutes, against 12
  per 15 minutes normally). The window also holds 6 `Failed` runner pods, 5 containers terminated
  with `Error` and 1 `Evicted`, so the excess is plausibly retries of failures, but the failure
  reasons were not read. Nothing constrains where they run: up to 10 were on `optiplex` at once,
  up to 7 on `wyrm2` and 2 on OVH nodes.
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

## Chronic write volume

The burst sits on top of a steady load that is heavy for a 256 GB drive. `node_exporter`
`nvme0n1` bytes written, per UTC day: typically 160–350 GB (median about 230 GB; 382 GB on
09-23, the day of the first dropout), 111 GB in the 24 h before this failure. The drive's rated
endurance and SMART wear counters have not been read.

Long-lived pods account for under half of that: in the same 24 h the six `haku-ci` runner pods
wrote about 36 GB (about 6 GB each), the CPAP gateway VM 7.3 GB and `alloy` 1.75 GB, leaving
about 65 GB unattributed. The Tofu runners cannot be counted this way, since a pod's cAdvisor
series disappear with the pod; the unattributed part is where they, image unpacking and kubelet
writes would sit.

## Across three weeks

`node_exporter` and cAdvisor series for 2026-09-14 to 10-04, with the incident window at 15 s
resolution and the rest in 5-minute bins. Rates use a 2 minute window because the node's scrape
interval is 60 s.

**Temperature follows disk load closely, within minutes.** In the incident window (22:20 to
23:16:30), the correlation (Pearson r, same instant) of the NVMe's hottest sensor with each
metric:

| Metric                                       | r    |
| -------------------------------------------- | ---- |
| CPU busy                                     | 0.94 |
| device busy                                  | 0.93 |
| I/O pressure (all tasks stalled), CPU iowait | 0.87 |
| network receive                              | 0.86 |
| load average                                 | 0.85 |
| write latency                                | 0.83 |
| disk writes                                  | 0.80 |
| CPU package temperature                      | 0.55 |

The correlation is gone once the metric leads the temperature by 5 minutes, so heating and
cooling both take minutes. The CPU package stayed at or below 72 °C while the drive reached 98 °C.
Many of these metrics share one cause (the same pods), so they do not separate writes from
everything else the pods do.

**Dose and response over the three weeks** (5-minute mean write rate against the drive's hottest
sensor; r = 0.66 overall):

| Write rate (MB/s) | 5-minute bins | Mean temperature | Bins at or above 80 °C |
| ----------------- | ------------- | ---------------- | ---------------------- |
| under 1           | 3085          | 51 °C            | 0.1 %                  |
| 1 to 5            | 1168          | 53 °C            | 0 %                    |
| 5 to 10           | 409           | 55 °C            | 1.0 %                  |
| 10 to 20          | 336           | 60 °C            | 3.6 %                  |
| 20 to 40          | 54            | 75 °C            | 35 %                   |

There were 53 bursts of 20 MB/s or more; 15 of them reached 80 °C. Bursts of five minutes mostly
stayed below 80 °C; those that held 10 to 15 minutes mostly reached 93 °C or more.

**Who wrote, for each episode at or above 85 °C.** Device bytes written in the 15 minutes to two
minutes after the peak, and the largest container writers in that window (cAdvisor
`container_fs_writes_bytes_total`; counted from each pod's first sample, so short-lived pods are
under-counted, and nested `dind` and cached writes can make a pod exceed the device total, so
read it as a ranking):

| Peak (UTC, 2026) | °C  | Device GB | Largest writers (GB)                                                     |
| ---------------- | --- | --------- | ------------------------------------------------------------------------ |
| 09-16 07:35      | 93  | 0.1       | none                                                                     |
| 09-23 03:00      | 86  | 7.1       | `source-controller` 4.0                                                  |
| **09-23 23:10**  | 100 | 18.7      | `haku-ci` runner 23.3, `source-controller` 4.8, `dns-records` runner 2.5 |
| 09-25 06:45      | 96  | 15.6      | `haku-ci` runner 4.3                                                     |
| 09-25 18:05      | 93  | 15.2      | `haku-ci` runners 24.9                                                   |
| 09-25 18:40      | 93  | 12.2      | `haku-ci` runner 20.1                                                    |
| 09-26 03:55      | 93  | 14.6      | `haku-ci` runners 24.8                                                   |
| 09-26 08:30      | 86  | 10.6      | none attributed                                                          |
| 09-28 06:15      | 87  | 15.2      | `haku-ci` runner 6.4, `dns-records` runner 3.0                           |
| 09-29 00:55      | 96  | 13.1      | `haku-ci` runner 17.6                                                    |
| 10-01 02:20      | 87  | 7.9       | `haku-ci` runner 6.5                                                     |
| 10-01 16:45      | 96  | 11.2      | `haku-ci` runner 17.5                                                    |
| **10-04 23:15**  | 97  | 9.8       | Tofu runners 3.6 (`dns-records` 2.9, `litellm-keys` 0.6)                 |

Bold rows are the dropouts. `haku-ci` runner pods lead nine of the 13 episodes, Tofu runner pods
three (09-23 23:10, 09-28 06:15, 10-04 23:15), `source-controller` two, and two have no write
load to blame. The two dropouts are the two highest readings of the three weeks; three other
episodes reached 96 °C without one. So any sustained write burst of roughly 10 GB per quarter
hour heats this drive, whichever workload makes it, and the Tofu runners were the trigger on 10-04
but not the recurring one.

Queries: `node_hwmon_temp_celsius{chip=~".*nvme.*"}`, `node_disk_written_bytes_total`,
`node_disk_write_time_seconds_total` over `node_disk_writes_completed_total` (device
`nvme0n1`), and `container_fs_writes_bytes_total{node="optiplex"}`.

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

- Why runner creations at 23:11–23:15 ran at about three times the designed rate (the failed pods'
  reasons were not read), and what inside the runners wrote gigabytes. Repeated provider
  downloads into the pod's scratch volume fit the numbers but were not observed.
- Why 30 MB/s was enough to reach 97 °C, and why write latency reached seconds. Both fit a
  drive that was thermally throttling and then stalling, but that is not shown. A 256 GB OEM
  drive with no heatsink in an enclosed micro chassis is a candidate; its SMART data (media
  errors, thermal-throttle counters) has not been read.
- Whether a hottest-sensor reading near 100 °C is the dropout condition. The two dropouts are the
  highest readings in three weeks, and three other episodes reached 96 °C without one; these are
  5-minute samples, which can miss a higher peak.
- Two episodes with no write load to explain them: 09-16 07:35 (93 °C with 0.1 GB written) and
  09-26 08:30 (86 °C, 10.6 GB at the device, no pod attributed).
- Whether a soft `talosctl reboot` brings the NVMe back, or only a power cycle does.
- Whether the Home Assistant state is safely backed up. Its volume (`local-path-home-ssd`)
  lives on this drive and is copied by VolSync to SeaweedFS. The `volsync-src-home-assistant-config-restic`
  job started at 00:17 on 10-05 and is still `Pending` (nowhere to run); the last successful
  snapshot was not checked, because the `haku` identity cannot read `ReplicationSource`.
  Check it before reformatting or replacing the drive.

## Options

In order of how much of the cause each removes. The runner pod template of the
tofu-controller `Terraform` CRD accepts `resources`, `nodeSelector`, `affinity` and `tolerations`.

1. **Place Tofu runner pods off the home nodes.** Set a `nodeSelector`
   (`topology.kubernetes.io/region: hil`) on the runner pod template in `tofu_state_terraform`
   (`cluster/cdk8s/terraform.py` sets none). One change, covers every module, and removes the
   10-04 trigger. It does not touch the recurring one: see 2.
2. **Limit `haku-ci`'s writes on this node.** Its runner pods lead nine of the 13 hot episodes,
   including the 09-23 dropout, at 17 to 25 GB per quarter hour, and wrote about 36 GB in the
   24 h before the failure. Choices: keep CI off this node (operator notes from 2026-09-10
   record identical jobs at 1.5 to 4 minutes here against 11 to 21 on an OVH worker, so this
   costs CI speed; not re-measured), cap concurrent CI jobs here, or put the `dind` layer
   storage in memory-backed scratch (a job writes about 6 GB, so this needs the RAM headroom
   checked).
3. **Give runner pods resource requests, and later an ephemeral-storage limit.** They ran
   best-effort. CPU and memory requests (250m, 512Mi, sized from three days of observed usage)
   are in `cluster/cdk8s/terraform.py`. An ephemeral-storage limit would let the kubelet evict a
   runaway pod instead of letting it keep writing; scratch-space use was not measured, so none is
   set.
4. **Reconcile less often and not all at once.** All 19 objects poll every 15 minutes in
   synchronised waves, and nearly all report `TerraformPlannedNoChanges`. Stagger the intervals
   or lengthen them (a spec change already reconciles at once, per the comment in
   `cluster/cdk8s/dns_automation.py`; whether a new Git revision does too should be checked).
5. **Move the DNS records to external-dns** (below). Removes the heaviest Tofu runner, but only that one.
6. **Cool the drive.** Fit a low-profile M.2 heatsink or thermal pad and leave the shelf open;
   moving it about 30 cm from the CRS310 is cheap and harmless but, per the temperature
   history above, unlikely to be enough alone.
7. **Alert on the leading indicators.** Sustained device write latency or I/O pressure on
   `optiplex` (both were far out of range by 23:13, three minutes before the drive disappeared),
   and NVMe temperature above about 75 °C, ahead of the 83.85 °C warning threshold.
8. **Recovery watchdog.** A standard hardware or `softdog` watchdog does not help: Talos'
   `machined` keeps running and keeps feeding it while the disk is gone. What detects this
   failure is an external check ("`NotReady` for N minutes while the host still answers ping")
   followed by a power cycle through something that does not depend on this node: a smart plug,
   or Intel AMT if this unit's vPro is enabled (unchecked). Home Assistant runs on this node,
   so it cannot be the actuator.
9. **Cap the drive's power state** (kernel parameter or `nvme set-feature`) to lower peak heat
   at the cost of write speed. Unverified on this drive.
10. **Reduce the node's role.** It is the only home worker that can host the USB WiFi stick
    (CPAP gateway) and the pinned Home Assistant volume.

### external-dns for the Route 53 records

`tf/gitops/dns-records` is the heaviest runner here: it uses the AWS provider, and
`dns-records-tf-runner` wrote at least 3.1 GB in the ten minutes to 23:17:30.

- **What it manages:** apex and wildcard `A` records, an `mx` host `A` record, the apex `MX`, SPF
  and DMARC `TXT` records (all from the public-node roster in `nebula-mesh.json`), the `api` `A`
  record (control-plane roster), and the domain registration's name-server delegation.
- **Fit:** external-dns's CRD source (`DNSEndpoint`) can express `A`, `MX` and `TXT`, so the
  record set could be generated by cdk8s from the same roster. The registration delegation is
  not a DNS record and would stay in a module that rarely runs (it already ignores almost every
  field).
- **Why it helps:** a long-lived Deployment calling the Route 53 API directly downloads no
  provider per reconcile.
- **Constraints:** the OVH-only resilience invariant (`cluster/docs/decisions.md`) asks for no
  Proxmox-pinned storage or workloads and that it can schedule on OVH nodes; external-dns is
  stateless, so it meets that without any placement rule. The wildcard is the front door to every
  hostname, so a bad delete takes ingress DNS down (TTL 300 s).
- **Adoption risk (from memory of external-dns behaviour, not checked against its docs):** it
  tracks ownership with `TXT` registry records and skips records it does not own, so the existing
  hand-imported records need adopting, or a policy that allows it, before cutover. Rehearse on a
  test zone first. An archived note on an earlier DNS-operator failure exists in
  `cluster/archive/`; it was not read here.
- **Limit:** this removes one of 19 modules. The other 18 would still reinstall their providers
  on every reconcile, so options 1 to 4 remain the actual fix.

## Open infrastructure

- **Out-of-band telemetry.** Today reading this node's logs or the CRS310 needs an approved SSH
  session to `wyrm2`. The Talos API is reachable over Nebula (`10.42.0.18:50000`), so a
  read-only in-cluster path (Talos `os:reader`) is possible. The CRS310 has no telemetry path
  into Prometheus yet.
