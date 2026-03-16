# Atlas Black Screen Lockup — Recurring Chipset Failure

## Summary

Atlas (Proxmox host, ASUS ProArt X870E-CREATOR WIFI) has recurring system hangs (black screen, requires power-off) caused by the AMD 800 Series chipset PCIe fabric failing. The failure manifests as SATA link errors on the second AHCI controller (`ata7`–`ata10`), then escalates to the entire chipset subtree going offline — SATA, USB (xHCI), and Atlantic NIC all become inaccessible (`0xFFFFFFFF` reads), causing soft lockups and a hard hang.

**GPUs**: 2x NVIDIA GeForce RTX 5090 (GB202, Blackwell, PCI ID `10de:2b85`):

- Slot `01:00.0` — Zotac (subsystem `19da:1761`, IOMMU group 14)
- Slot `03:00.0` — Gigabyte (subsystem `1458:416f`, IOMMU group 16)

Six chipset-level incidents documented (incidents 1–6), plus a new pattern of rapid VFIO-triggered crashes (incidents 7–10, 12) discovered on Mar 10–11. SATA cable reseat on Mar 6 did **not** help — incident 3 occurred afterward with identical pattern plus USB controller death, confirming this is **not** a cable issue. Incidents 4–6 continue the pattern with increasing frequency (two hangs on Mar 10 alone).

**Mar 10 breakthrough**: After the ASPM/runtime PM intervention, atlas was rebooted multiple times. Four consecutive boots (incidents 7–10) froze within 30–60 seconds — every time immediately after VMs auto-started and VFIO reset the 2x RTX 5090 GPUs for wyrm2 (VM 110). Disabling VM autostart and stopping all VMs resulted in atlas remaining stable. This identifies **VFIO GPU passthrough as the immediate trigger** for the rapid crashes, and likely a contributing factor to the earlier slow-onset chipset failures.

**Mar 11 update**: Incident 11 confirms the **two failure modes are independent**: ata7 SATA errors appeared at 23:48 even with GPU passthrough removed and VMs running without GPUs. The slow-onset chipset failure pattern persists regardless of VFIO. Incident 12 confirmed VFIO-triggered crashes are still reproducible — a 2-minute crash after GPUs were re-added to the VM config, **even with `disable_idle_d3=1` active**. `ahci.mobile_lpm_policy=1` (max_performance) added to kernel cmdline to disable SATA DIPM. Notable observation: kernel 6.17 logs explicit VFIO FLR resets (`vfio-pci ... resetting` / `reset done`) on every VM start — kernel 6.8 did not log these (unclear whether the reset was actually skipped or just not logged).

**Mar 11 afternoon**: Incident 13 (kernel 6.8): wyrm2 with GPU passthrough ran stable for ~17 minutes, then crashed ~22 seconds after Talos CP VM (10000, no GPU) was manually started. Wyrm2 started at 15:24:58, Talos CP started at 15:40:18, last journal entry at 15:40:46 — hard power-off followed. Incident 14 (kernel 6.17): crashed within ~1 minute with wyrm2+GPUs autostarting. Both boots ended with abrupt journal silence (no shutdown signals), consistent with hard lockup + power button. `ahci.mobile_lpm_policy=1` confirmed active on current boot (6.17).

**Incident 15** (kernel 6.17, `nvidia-drm.modeset=0` applied, `mobile_lpm_policy=1` active): wyrm2 with single GPU (Zotac 01:00.0) ran stable for ~33 minutes alone. After Talos CP VM (10000, no GPU) was started, the system experienced two major stalls but **recovered from both**: (1) ~16:54 (~1h after boot) — ZFS zvol write threads deadlocked 122s, pcieport `18:00.0` failed D3cold→D0, snapd watchdog timed out; (2) ~18:12 (~2h22m after boot) — additional pcieport `18:00.0` D3cold failures at kernel timestamps 8446s and 9532s, screen froze, load average hit 101, SSH unreachable for ~16 minutes. System recovered at ~18:28. PCIe bridge D3cold and runtime PM were disabled live on all bridges (class `0x0604*`) during the recovery window. **Historical scan**: pcieport `18:00.0` was present on all prior boots with D3cold capability (`PME# supported from D3cold`) but no D3cold failure errors were logged in any historical boot — the failures in boot `5ef3cedb` are the first recorded instances.

## Current Status

**Mar 12 ~04:40 CET** (boot `978abe76`, kernel 6.17, `pcie_aspm=off`, `ahci.mobile_lpm_policy=1`): Fresh boot of atlas with wyrm2 (96 GiB, 2 GPUs: Zotac 01:00.0 + Gigabyte 03:00.0) + Talos CP VM (10000, 8 GiB). **7 hours uptime, zero errors** — no SATA errors, no PCIe failures, no soft lockups. VFIO resets completed cleanly. ZFS ARC healthy (`memory_available_bytes` = 799 MB, `size` = 9.3 GiB, above `c_min` = 3.9 GiB). This is full production config (both GPUs + Talos CP) running stable — the longest 2-GPU VFIO survival by a wide margin (incidents 7–10, 12, 14 all died within 0–2 min).

## Recurrence Log

Boot IDs are the first 8 hex chars of the systemd journal boot UUID (`journalctl --list-boots`).

| #   | Boot ID    | Boot time    | Onset         | Hang          | Recovery                     | Uptime before failure  | Devices affected                                                                                                                                                                                                           |
| --- | ---------- | ------------ | ------------- | ------------- | ---------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `ba60fe65` | Feb 27 19:17 | Feb 28 00:04  | Feb 28 ~18:05 | Mar 4 20:52 (powercycle)     | ~5h after boot         | SATA (`ata7`, `ata8`, `ata10`)                                                                                                                                                                                             |
| 2   | `b825ed78` | Mar 4 20:52  | Mar 5 02:38   | Mar 5 ~06:47  | Mar 6 01:17 (powercycle)     | ~6h after boot         | SATA (`ata7`, `ata8`), Atlantic NIC, PCIe `08:08.0`                                                                                                                                                                        |
| 3   | `6522a81a` | Mar 6 01:17  | Mar 7 00:01   | Mar 7 ~06:42  | Mar 7 06:54 (powercycle)     | ~23h after boot        | SATA (all 4), xHCI USB, Atlantic NIC                                                                                                                                                                                       |
| 4   | `df5a691c` | Mar 7 13:00  | Mar 7 18:29   | **survived**  | Mar 9 22:36 (clean shutdown) | ~5h onset, 2.5d uptime | SATA (`ata7` only) — errors but no cascade                                                                                                                                                                                 |
| 5   | `4089259e` | Mar 9 22:37  | Mar 10 00:01  | Mar 10 ~01:04 | Mar 10 01:06 (powercycle)    | ~1.5h after boot       | SATA (`ata7`), Atlantic NIC, soft lockup                                                                                                                                                                                   |
| 6   | `c2a8b888` | Mar 10 01:06 | —             | Mar 10 ~06:31 | Mar 10 13:21 (powercycle)    | ~5.5h after boot       | Atlantic NIC, soft lockup (pci_mmcfg_read + aq_hw_read_reg)                                                                                                                                                                |
| 7   | `f96c21cc` | Mar 10 19:18 | —             | Mar 10 ~19:19 | Mar 10 19:24 (powercycle)    | ~1 min                 | VFIO GPU reset → immediate freeze                                                                                                                                                                                          |
| 8   | `b0e04dde` | Mar 10 19:24 | —             | Mar 10 ~19:24 | Mar 10 21:07 (powercycle)    | ~30 sec                | VFIO GPU reset → immediate freeze                                                                                                                                                                                          |
| 9   | `4e2daf12` | Mar 10 21:07 | —             | Mar 10 ~21:08 | Mar 10 21:10 (powercycle)    | ~45 sec                | VFIO GPU reset → immediate freeze                                                                                                                                                                                          |
| 10  | `104bc613` | Mar 10 21:10 | —             | Mar 10 ~21:10 | Mar 10 21:17 (powercycle)    | ~40 sec                | VFIO GPU reset → immediate freeze                                                                                                                                                                                          |
| 11  | `01bca0b8` | Mar 10 21:17 | Mar 10 23:48  | **survived**  | Mar 11 02:51 (clean reboot)  | ~2.5h onset, 5.5h up   | SATA (`ata7` only) — no GPUs, errors but no cascade                                                                                                                                                                        |
| 12  | `dcad0e96` | Mar 11 03:49 | —             | Mar 11 ~03:51 | Mar 11 03:54 (powercycle)    | ~2 min                 | VFIO GPU reset → immediate freeze (D3cold workaround active)                                                                                                                                                               |
| 13  | `e6081d6d` | Mar 11 15:23 | —             | Mar 11 ~15:41 | Mar 11 15:48 (powercycle)    | ~17 min                | Kernel 6.8: wyrm2+GPUs ran 17min, froze ~22s after Talos CP VM (10000, no GPU) started                                                                                                                                     |
| 14  | `c740ecfd` | Mar 11 15:48 | —             | Mar 11 ~15:49 | Mar 11 15:50 (powercycle)    | ~1 min                 | Kernel 6.17: VFIO GPU reset → immediate freeze                                                                                                                                                                             |
| 15  | `5ef3cedb` | Mar 11 15:50 | Mar 11 ~16:54 | **survived**  | Mar 11 19:27 (clean reboot)  | ~1h onset, 3.6h up     | Kernel 6.17, modeset=0: 1 GPU (Zotac) + Talos CP → ZFS hung tasks (122s), pcieport 18:00.0 D3cold fail. Two stalls (~16:54, ~18:12), both recovered. Load avg hit 101. PCIe bridge D3cold/PM disabled live during recovery |

## Incident 1 — Feb 28

### Timeline

| Time         | Event                                                                                           |
| ------------ | ----------------------------------------------------------------------------------------------- |
| Feb 27 19:17 | Boot started                                                                                    |
| Feb 28 00:04 | `ata7` starts throwing `READ FPDMA QUEUED` timeouts, `SError: PHYRdyChg CommWake 10B8B LinkSeq` |
| Feb 28 06:36 | `ata8` joins with ATA bus errors (`PHYRdyChg CommWake DevExch`)                                 |
| Feb 28 06:37 | `ata7` continues with bus errors                                                                |
| Feb 28 06:40 | `ata10` starts timing out too — 3 of 4 SATA drives now failing                                  |
| Feb 28 06:45 | Kernel downgrades `ata7` link from 6.0 Gbps to 3.0 Gbps. `BadCRC`, `Handshk`, `TrStaTrns` flags |
| Feb 28 06:48 | Last SATA errors in journal. System limps along (cron, smartd, tailscaled still running)        |
| Feb 28 18:05 | Journal abruptly stops. Machine hung                                                            |
| Mar 4 20:52  | Powercycle. All drives come back clean at 6.0 Gbps, ZFS pools ONLINE, zero errors               |

## Incident 2 — Mar 5

### Timeline

| Time        | Event                                                                                                                                                                    |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Mar 4 20:52 | Boot after powercycle. All drives up at 6.0 Gbps                                                                                                                         |
| Mar 5 02:38 | `ata7` starts with same pattern: `READ/WRITE FPDMA QUEUED` timeouts, `SError: PHYRdyChg CommWake 10B8B LinkSeq`. Hard reset recovers link at 6.0 Gbps                    |
| Mar 5 02:42 | `ata8` joins: `SError: PHYRdyChg CommWake DevExch`. Hard reset recovers                                                                                                  |
| Mar 5 06:38 | `ata7` again: same errors plus `TrStaTrns`. ZFS `delay` events on `ata-ST16000NT001-3MC101_ZYD502GM-part1` (delays 31–34s)                                               |
| Mar 5 06:43 | `ata7` escalates: **`irq_stat 0x48000008, interface fatal error`**, `SError: UnrecovData CommWake 10B8B BadCRC Handshk LinkSeq`, `error: ICRC ABRT`. Hard reset recovers |
| Mar 5 06:45 | `ata8` hard reset and re-link at 6.0 Gbps                                                                                                                                |
| Mar 5 06:47 | Network link drops (`atlantic enp12s0: link change old 100 new 0`). PCIe port `0000:08:08.0` unable to transition from D3hot to D0 — **device inaccessible**             |
| Mar 5 06:47 | **Soft lockup**: `CPU#12 stuck for 22s` in `kworker` doing `pci_mmcfg_read` → `pci_restore_ltr_state` → `pci_pm_runtime_resume`. PCI config read returns `0xFFFFFFFF`    |
| Mar 5 06:47 | Journal ends. Machine hung                                                                                                                                               |

### New observations in incident 2

- **`ata10` did not fail** this time (only `ata7` and `ata8`), but system still hung
- **PCI bus-level failure**: the hang was preceded by a PCIe device (`0000:08:08.0`) becoming inaccessible during runtime PM resume — `pci_mmcfg_read` returned all-ones (classic sign of a device/link that has dropped off the bus)
- **Network card (Atlantic/Aquantia) lost link** at the same time — this NIC is on the same PCIe root complex, suggesting the problem may extend beyond SATA to the chipset/PCIe fabric
- **`interface fatal error`** flag appeared (not seen in incident 1), indicating the AHCI controller itself flagged an unrecoverable condition
- **ZFS delay events** explicitly logged: 31–34 second I/O delays on `ata7`'s drive before the hang
- **Onset timing**: ~6h after boot (incident 1 was ~5h). Both overnight — possibly thermal buildup or a periodic background task (ZFS scrub from incident 1 may still have been running)

## Incident 3 — Mar 7

This incident occurred **after** the partial SATA cable reseat on Mar 6.

### Timeline

| Time        | Event                                                                                                                                                             |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mar 6 01:17 | Boot after powercycle. All drives up at 6.0 Gbps. Atlantic NIC up                                                                                                 |
| Mar 6 01:52 | Atlantic NIC link flap (10G → 0 → recovers) — independent NIC flakiness, not part of chipset failure                                                              |
| Mar 7 00:01 | `ata7` starts: `SError: PHYRdyChg CommWake DevExch`, `READ FPDMA QUEUED` failed. Hard reset recovers                                                              |
| Mar 7 00:05 | `ata7` again: `SError: PHYRdyChg CommWake 10B8B DevExch`, hard reset recovers                                                                                     |
| Mar 7 06:30 | `ata7` resumes: `SError: PHYRdyChg CommWake DevExch`, hard reset                                                                                                  |
| Mar 7 06:35 | `ata7` escalates: multiple READ/WRITE FPDMA failures, `SError: PHYRdyChg CommWake 10B8B LinkSeq`                                                                  |
| Mar 7 06:36 | `ata7` again: hard reset                                                                                                                                          |
| Mar 7 06:41 | **Soft lockup**: CPU#25 stuck for 26s in `pci_mmcfg_read` → `pci_restore_ltr_state` → `pci_pm_runtime_resume` (workqueue: `pm pm_runtime_work`). RAX=`0xFFFFFFFF` |
| Mar 7 06:41 | **xHCI USB controller `0000:0e:00.0` dies**: "xHCI host controller not responding, assume dead". USB devices disconnect                                           |
| Mar 7 06:41 | `ata10` joins: `SError` with **all flags set** (`0xFFFFFFFF`), WRITE FPDMA failures                                                                               |
| Mar 7 06:41 | AHCI controller `0000:11:00.0`: "AHCI controller unavailable!" (repeated)                                                                                         |
| Mar 7 06:41 | `ata9` joins: READ FPDMA failures, hard reset                                                                                                                     |
| Mar 7 06:41 | All 4 SATA ports: `failed to resume link (SControl FFFFFFFF)`, `SATA link down (SStatus FFFFFFFF SControl FFFFFFFF)` — controller completely gone                 |
| Mar 7 06:41 | **Atlantic NIC stuck**: CPU#6 soft lockup in `aq_hw_read_reg` → `aq_nic_service_task` — NIC MMIO reads hanging                                                    |
| Mar 7 06:42 | Soft lockup escalates: CPU#25 stuck 105s. More USB disconnects. Network unreachable. Journal ends                                                                 |

### New observations in incident 3

- **xHCI USB controller died** (`0000:0e:00.0`) — first time USB is affected. "not responding, assume dead"
- **All 4 SATA drives** failed (not just ata7/ata8). All returned `SStatus/SControl FFFFFFFF`
- **Atlantic NIC stuck** in MMIO register read (`aq_hw_read_reg`), causing its own soft lockup on a separate CPU
- **Partial cable reseat did not help** — incident occurred ~29h after the Mar 6 intervention. Note: only right-side mobo connectors and disk-side connectors were reseated; the 2 bottom mobo-side connectors were not touched (blocked by GPUs)
- **Onset ~23h after boot** (longer than incidents 1-2's ~5-6h), but the initial `ata7` errors at 00:01 match the previous pattern of overnight onset
- **Escalation was faster**: from first `ata7` errors to complete chipset death in ~6.5h (similar to incidents 1-2)
- **Multiple CPUs locked**: CPU#25 (`pm_runtime_work`), CPU#6 (`atlantic`), CPU#1 (`atlantic` again), CPU#25 repeated at 78s, 105s

## Incident 4 — Mar 7–9 (survived)

Boot `df5a691c` (Mar 7 13:00 → Mar 9 22:36, clean shutdown). ~2.5 days uptime — longest so far.

### Timeline

| Time        | Event                                                                                                                      |
| ----------- | -------------------------------------------------------------------------------------------------------------------------- |
| Mar 7 13:00 | Boot after powercycle                                                                                                      |
| Mar 7 18:29 | `ata7` starts: `SError: RecovData UnrecovData Proto CommWake 10B8B BadCRC Handshk LinkSeq TrStaTrns`, 4x READ FPDMA failed |
| Mar 7 18:41 | `ata7` again: `SError: CommWake 10B8B LinkSeq`                                                                             |
| Mar 7 23:00 | `ata7` again: `SError: RecovData UnrecovData CommWake 10B8B BadCRC Handshk LinkSeq`                                        |
| Mar 8 03:17 | `ata7` again: `SError: PHYRdyChg CommWake 10B8B Handshk LinkSeq`                                                           |
| Mar 8 06:33 | `ata7` again: `SError: PHYRdyChg CommWake DevExch`                                                                         |
| Mar 8 06:36 | `ata7` again: `SError: UnrecovData CommWake 10B8B BadCRC Handshk LinkSeq TrStaTrns`                                        |
| Mar 8 06:38 | `ata7` again: `SError: PHYRdyChg CommWake DevExch`                                                                         |
| Mar 8 06:39 | `ata7` last errors: `SError: CommWake 10B8B Handshk LinkSeq`                                                               |
| Mar 9 22:36 | Clean shutdown (SIGTERM, journal stopped normally)                                                                         |

### Notable

- **Did NOT escalate** to full chipset death. `ata7` had repeated errors with hard resets but the chipset PCIe fabric held.
- Only `ata7` was affected — no `ata8`/`ata9`/`ata10`, no USB, no NIC.
- All errors recovered via hard reset. System remained functional for 2.5 days.

## Incident 5 — Mar 10

Boot `4089259e` (Mar 9 22:37 → Mar 10 ~01:04, hung). Only ~2.5h uptime.

### Timeline

| Time         | Event                                                                                                                            |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| Mar 9 22:37  | Boot after clean shutdown                                                                                                        |
| Mar 10 00:01 | `ata7` starts: `interface fatal error`, `SError: UnrecovData CommWake 10B8B BadCRC Handshk LinkSeq TrStaTrns`, READ FPDMA failed |
| Mar 10 01:03 | Atlantic NIC link drop (`link change old 2500 new 0`)                                                                            |
| Mar 10 01:03 | **Soft lockup**: CPU#8 stuck 22s in `pci_mmcfg_read` (pm_runtime_work). CPU#6 stuck 22s                                          |
| Mar 10 01:04 | CPU#8 stuck 48s. Journal ends. Machine hung                                                                                      |

### Notable

- `interface fatal error` appeared immediately on first `ata7` error (not after escalation)
- Very fast progression: ata7 errors at 00:01, hang at ~01:04 — only 1h between first error and death
- Shortest time-to-death yet

## Incident 6 — Mar 10

Boot `c2a8b888` (Mar 10 01:06 → Mar 10 ~06:31, hung). ~5.5h uptime.

### Timeline

| Time         | Event                                                                                                                                          |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Mar 10 01:06 | Boot after powercycle                                                                                                                          |
| Mar 10 01:06 | Atlantic NIC link up (2500)                                                                                                                    |
| Mar 10 06:31 | Atlantic NIC link drop (`link change old 2500 new 0`)                                                                                          |
| Mar 10 06:31 | **Soft lockup**: CPU#8 stuck 22s in `pci_mmcfg_read` → `pci_restore_ltr_state` → `pci_pm_runtime_resume` (pm_runtime_work). RAX=`0xFFFFFFFF`   |
| Mar 10 06:31 | CPU#0 stuck 22s in `aq_hw_read_reg` → `hw_atl2_shared_buffer_read_block` → `aq_nic_service_task` (atlantic workqueue) — NIC MMIO reads hanging |
| Mar 10 06:31 | Journal ends. Machine hung                                                                                                                     |

### Notable

- **No `ata7` SATA errors logged before the hang** — the first visible symptom was the Atlantic NIC losing link, immediately followed by soft lockups
- Two separate CPUs locked: CPU#8 on PCI config space read (pm_runtime_work), CPU#0 on Atlantic NIC MMIO read
- The hang occurred at the same time of day (~06:30) as incidents 2, 3, and 5's ata7 escalation
- Possible that ata7 errors occurred but weren't logged before the chipset fabric dropped entirely

## Incidents 7–10 — Mar 10 (VFIO-triggered rapid crashes)

After the ASPM/runtime PM intervention (incident 6), atlas was rebooted several times. VM autostart was still enabled, causing wyrm2 (VM 110, 2x RTX 5090 VFIO passthrough, 32 cores, 114GB RAM, ~25 SCSI disks) to start on every boot.

### Timeline

| Incident | Boot ID    | Started | Last log | Uptime  | Last logged activity                      |
| -------- | ---------- | ------- | -------- | ------- | ----------------------------------------- |
| 7        | `f96c21cc` | 19:18   | 19:19    | ~1 min  | VFIO GPU resets, VM tap interfaces up     |
| 8        | `b0e04dde` | 19:24   | 19:24    | ~30 sec | VFIO GPU resets, VM tap interfaces up     |
| 9        | `4e2daf12` | 21:07   | 21:08    | ~45 sec | VFIO GPU resets, journal corruption noted |
| 10       | `104bc613` | 21:10   | 21:10    | ~40 sec | VFIO GPU resets, journal corruption noted |

### Key observations

- **No SATA errors in any of these boots** — the system froze without the ata7 canary pattern seen in incidents 1–6
- **Every crash occurred immediately after VFIO GPU reset**: last logged lines are `vfio-pci 0000:0[13]:00.0: reset done` followed by VM tap interface setup, then journal stops
- **VMs involved**: wyrm (VM 100), wyrm2 (VM 110, GPU passthrough), talos (VM 10000)
- **Journal corruption messages** (`user-1000.journal corrupted or uncleanly shut down`) in boots `4e2daf12` and `104bc613` confirm hard crashes
- `snd_hda_intel ... GPU sound probed, but not operational: please add a quirk to driver_denylist` appeared in boots `b0e04dde` and `f96c21cc` — GPU audio function conflicting with host HDA driver during VFIO handoff
- **Boot `ac3243b8`** (the ASPM-fixed boot from 13:21) survived for ~6h, had the classic ata7 error at 17:41, then wyrm2 was restarted around 19:15 and it froze shortly after — matching the VFIO trigger pattern

### Intervention — Mar 10 21:19: Disable VM autostart

- Stopped all VMs: wyrm (100), wyrm2 (110), talos (10000), and others
- Disabled `onboot` for all VMs
- **Atlas remained stable** with no VMs running — confirming VFIO GPU passthrough as the trigger

## Incident 11 — Mar 10–11 (survived, no GPUs)

Boot `01bca0b8` (Mar 10 21:17 → Mar 11 02:51, clean reboot). ~5.5h uptime.

### Timeline

| Time         | Event                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------ |
| Mar 10 21:17 | Boot. VMs 100, 110, 10000 auto-start at 21:18 (with GPU passthrough still in VM 110 config)            |
| Mar 10 21:18 | VFIO GPU resets for both RTX 5090s complete successfully — system **survives** (unlike incidents 7–10) |
| Mar 10 21:19 | Intervention: all VMs stopped, autostart disabled for all VMs                                          |
| Mar 10 22:33 | GPU passthrough removed from VM 110 config. Autostart re-enabled for VMs 110 and 10000                 |
| Mar 10 23:48 | `ata7` errors: `SError: { PHYRdyChg CommWake DevExch }`, 2x WRITE FPDMA QUEUED failures. Hard reset    |
| Mar 11 02:51 | Clean reboot (systemd-reboot)                                                                          |

### Notable

- **VFIO resets succeeded without crashing** — first time since incident 7. The difference from incidents 7–10 is unclear (identical config). VFIO crashes are not 100% deterministic.
- **ata7 SATA errors appeared despite no GPU passthrough being active** — GPUs were removed from the VM config hours earlier, VMs were running without them. This confirms the **slow-onset ata7 pattern is independent of VFIO**.
- Only `ata7` affected, no cascade. System survived (same as incident 4).

## Boot between incidents 11 and 12 — Mar 11 (clean, no GPUs)

Boot `ad2bbb56` (Mar 11 02:52 → Mar 11 03:48, clean reboot). ~57 min uptime.

### Timeline

| Time         | Event                                                                                                      |
| ------------ | ---------------------------------------------------------------------------------------------------------- |
| Mar 11 02:52 | Boot. VM 110 auto-starts **without GPU passthrough** (no VFIO resets)                                      |
| Mar 11 02:54 | `kubernetes-csi` begins migrating PVC SCSI disks from VM 110 to VM 10000 (deleting scsi slots from VM 110) |
| Mar 11 03:26 | Ansible playbook runs (re-applies VFIO module config, `vfio_virqfd` "Failed to find module")               |
| Mar 11 03:40 | Ansible playbook runs again (second pass)                                                                  |
| Mar 11 03:48 | Clean reboot (likely ansible-triggered for kernel cmdline changes)                                         |

### Notable

- **Completely clean boot** — no SATA errors, no lockups, no PCIe issues.
- Ansible re-applied configuration, added `ahci.mobile_lpm_policy=0` to kernel cmdline.
- GPU passthrough was re-added to VM 110 config (manually, between ansible runs — preparing to test).

## Incident 12 — Mar 11 (VFIO-triggered crash)

Boot `dcad0e96` (Mar 11 03:49 → Mar 11 ~03:51, crash). ~2 min uptime.

### Timeline

| Time         | Event                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------ |
| Mar 11 03:49 | Boot. Atlantic NIC link up at 03:49. VMs 110 and 10000 auto-start                                      |
| Mar 11 03:49 | VFIO GPU resets: `vfio-pci 0000:01:00.0: reset done`, `vfio-pci 0000:03:00.0: reset done` (first pair) |
| Mar 11 03:50 | Second round of VFIO GPU resets (both GPUs, multiple resets). VM 110 starts with PID 3714              |
| Mar 11 03:51 | Journal ends abruptly (postfix/PackageKit activity, no shutdown messages). Hard crash                  |

### Notable

- **Another VFIO-triggered crash**, consistent with incidents 7–10 pattern.
- GPU passthrough was re-added to VM 110 config after the clean boot `ad2bbb56` (testing if VFIO works after `ahci.mobile_lpm_policy=0` was added to cmdline — it doesn't).
- **D3cold workaround was already deployed** when this crash happened: ansible wrote `/etc/modprobe.d/vfio-pci.conf` (`disable_idle_d3=1`) and `/etc/udev/rules.d/99-vfio-gpu-no-d3cold.rules` at 03:40 during boot `ad2bbb56`, which rebooted cleanly at 03:48. Boot `dcad0e96` started at 03:49 with both files on disk before `vfio_pci` module loaded. **`disable_idle_d3=1` did not prevent the VFIO crash.**
- No SATA errors or soft lockups logged before the freeze.

### Intervention — Mar 11 03:57: Remove GPU passthrough again

- Boot `784e8c60` started at 03:54. Waited for atlas to come up via SSH.
- Removed `hostpci0`/`hostpci1` from **all** VMs (not just VM 110).
- Disabled `onboot` for all VMs. Stopped all running VMs.
- Atlas stable.

### What changed: wyrm2 VM reshuffle

The rapid freezing correlates with the wyrm2 VM being massively upsized (commit `f2e7606a7`, "refactor(infra): reshuffle VMs — delete GPU worker, upsize wyrm2, downsize PVE CP"):

| Property | Old wyrm2       | New wyrm2                    | Old GPU worker (deleted) |
| -------- | --------------- | ---------------------------- | ------------------------ |
| OS       | NixOS           | NixOS                        | Talos (minimal)          |
| vCPUs    | 8               | 32                           | 16                       |
| RAM      | 16 GB           | 114 GB (balloon=0)           | —                        |
| GPUs     | None            | 2x RTX 5090 (VFIO)           | 2x RTX 5090 (VFIO)       |
| Disks    | 1               | ~25 SCSI + virtio + virtiofs | few                      |
| Display  | —               | QXL + SPICE                  | none                     |
| Role     | Dev workstation | Dev workstation + K8s worker | K8s GPU worker           |

The old Talos GPU worker VM had the same GPUs passed through and worked fine. The new wyrm2 simultaneously demands VFIO GPU reset, 114GB RAM allocation (balloon=0), ~25 disk attachments, virtiofs shares, and QXL/SPICE initialization — a massive burst of PCIe and memory operations that appears to destabilize the system.

## PCIe Topology of Affected Devices

All affected devices share the same root port `0000:02.1` through the AMD 800 Series chipset PCIe switch:

```
0000:02.1 (CPU PCIe root port)
  └─ 04:00.0 (PCIe switch upstream)
      └─ 05:xx (switch downstream ports)
          ├─ 08:xx (inner PCIe switch)
          │   ├─ 0a: MediaTek WiFi
          │   ├─ 0b: Intel I226-V (igc) — not affected (yet)
          │   ├─ 0c: Aquantia Atlantic NIC — STUCK in incidents 2, 3, 5, 6
          │   ├─ 0d: (empty downstream port) — FFFFFFFF in incident 2
          │   ├─ 0e: AMD xHCI USB — DEAD in incident 3
          │   └─ 0f: AMD SATA Controller #1
          ├─ 10: AMD xHCI USB #2
          └─ 11: AMD SATA Controller #2 — ata7-ata10, UNAVAILABLE in all incidents
```

## Analysis

### Leading Hypothesis: Kernel 6.8 → 6.17 Upgrade Introduced DIPM

**Full journal analysis (24 boots, Jan 28 – Mar 11) shows the slow-onset crashes correlate with the kernel upgrade:**

| Kernel           | Boots                                    | Long uptimes                   | SATA/chipset crashes | DIPM reported |
| ---------------- | ---------------------------------------- | ------------------------------ | -------------------- | ------------- |
| 6.8.12-17/18-pve | `b4f351e2` through `a1f4e657` (6 boots)  | 3d20h, 12d5h, 10d6h, 1d20h     | **Zero**             | No            |
| 6.17.9-1-pve     | `b8fcfd57` through `b825ed78` (3 boots)  | 8h42m (crash), 1d5h (degraded) | **Every long boot**  | Yes           |
| 6.17.13-1-pve    | `a0211f18` through `784e8c60` (13 boots) | 2d10h best (no GPUs)           | **Every long boot**  | Yes           |

- **Kernel 6.8** ran with the same GPUs (2x RTX 5090) via VFIO passthrough for 12+ days without a single SATA/chipset crash
- **Kernel 6.17** had its first SATA/chipset crash on its very first boot (`b8fcfd57`, Feb 25, 8h 42m), with the same GPU worker VM (10100)
- The kernel upgrade happened on Feb 25 (boot `b8fcfd57`). The first crash was that same boot

Kernel 6.17 reports `DIPM NCQ-sndrcv CDL` in drive features at every boot. Kernel 6.8 did not. **DIPM (Device Initiated Power Management)** lets SATA drives autonomously request PHY link power state transitions (partial/slumber). On the AMD 800 Series chipset, these transitions may destabilize the PCIe fabric that the AHCI controller sits behind. `ahci.mobile_lpm_policy=1` (max_performance) has been deployed to disable DIPM — **not yet tested long enough to confirm this fixes the slow-onset crashes**.

`pcie_aspm=off` was present on all boots but **does not disable SATA DIPM** — ASPM is PCIe-level, DIPM is SATA-level. They are independent power management mechanisms.

### VFIO Reset Log Messages: Logging Change, Not Behavioral Change

Kernel 6.8 (boots `7249faba` through `a1f4e657`): zero `vfio-pci ... resetting` / `reset done` messages across all 6 boots, despite running the same GPU worker VM (10100) with 2x RTX 5090 via VFIO for 12+ days.

Kernel 6.17 (boots `b8fcfd57` through `784e8c60`): explicit `resetting` / `reset done` messages on every boot that starts a GPU VM (6 resets per typical boot, 116 on boot `df5a691c` which had multiple VM restarts).

**Resolved:** This is a **logging-only change**. Kernel commit [`a3151e6daaec`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=a3151e6daaec171b7d46ac79170ec420ad874cae) ("PCI: Warn if a running device is unaware of reset", v6.13, author Keith Busch) added `pci_warn()` messages when a PCI device is reset while its driver (e.g., `vfio-pci`) hasn't registered `reset_prepare`/`reset_done` notification callbacks. The FLR reset itself was already happening on kernel 6.8 — it just wasn't logged.

### Known Blackwell FLR Bug

The RTX 5090 (Blackwell, GB202) has a **known, widely-reported VFIO FLR bug**. When the host issues a Function Level Reset (standard VFIO cleanup), Blackwell GPUs fail to respond — PCI config space reads as all `0xFF`, PCI header type reads as `0x7F` (invalid), followed by CPU soft lockups. Older NVIDIA architectures (Ada Lovelace/RTX 4090, Hopper/H100, Ampere) are not affected.

**Community reports:**

- [Proxmox forum: RTX 6000/5090 D3cold lockup](https://forum.proxmox.com/threads/passthrough-rtx-6000-5090-cpu-soft-bug-lockup-d3cold-to-d0-after-guest-shutdown.168424/)
- [Level1Techs: RTX 5090 reset bug](https://forum.level1techs.com/t/do-your-rtx-5090-or-general-rtx-50-series-has-reset-bug-in-vm-passthrough/228549)
- [NVIDIA Developer Forums: GPU passthrough crashes](https://forums.developer.nvidia.com/t/proxmox-gpu-passthrough-crashes-host-rtx-pro-6000-and-rtx-5090/339038)
- [CloudRift blog: $1000 bounty (paid out)](https://www.cloudrift.ai/blog/bug-bounty-nvidia-reset-bug)

**Community-reported workarounds:**

- **GPU UEFI firmware update** using NVIDIA's GPU UEFI Firmware Update Tool
- **NVIDIA driver 580+** reportedly contains a fix
- `nvidia-drm modeset=0` in the guest VM
- Kernel 6.14.8-2-bpo12-pve (NVIDIA's suggested temporary workaround)
- `disable_idle_d3=1` (mixed results — did not help in our incident 12)

**Kernel code changes between 6.8 and 6.17 that may interact with this bug:**

- Upstream bridge locking for `pci_reset_function()` added in v6.10 ([`7e89efc6e9e4`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=7e89efc6e9e4))
- CRS (Configuration Request Retry Status) readiness detection changed in v6.12 ([`d591f6804e7e`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=d591f6804e7e1310881c9224d72247a2b65039af)) — alters post-reset timing/timeout behavior
- `vfio_pci_core_disable()` bridge lock fix ([`962ae6892d8b`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=962ae6892d8bd208b2d1e2b358f07551ddc8d32f)) — if bridge lock can't be acquired, **reset is skipped entirely**
- Proxmox 6.17 kernel enables `CONFIG_VFIO_DEVICE_CDEV=y` (iommufd backend instead of legacy vfio_iommu_type1)

These changes alter the reset code path timing and locking, which may explain why kernel 6.8 survived the Blackwell FLR bug while 6.17 crashes. The bug is in the GPU firmware/hardware; the kernel changes just change how the host interacts with the broken reset.

### Other Findings

- **Confirmed chipset-level failure**: Three different device types (SATA, USB, NIC) behind the same AMD 800 Series chipset PCIe hierarchy all fail simultaneously. This rules out SATA cables as the root cause.
- **Same escalation pattern**: `ata7` link errors → hard resets → AHCI controller unavailable → PCIe fabric returns `0xFFFFFFFF` → soft lockups → system hang
- **`ata7` is usually the canary**: first to show errors in incidents 1–5, hours before the cascade. But incident 6 had no logged SATA errors — the chipset PCIe fabric may have dropped without SATA being the first visible symptom.
- **PCI config space `0xFFFFFFFF`** across multiple devices confirms the chipset's PCIe switch or its upstream link is dropping
- **Runtime PM is the trigger for the lockup**: the soft lockup always occurs in `pci_pm_runtime_resume` → `pci_mmcfg_read`, trying to restore a device that has already fallen off the bus. The kernel spins forever waiting for a response that will never come
- **ASPM L1 is enabled** on the chipset PCIe switch (`LnkCtl: ASPM L1 Enabled`) — L1 power state transitions are a known source of PCIe link instability, especially with AMD 800 Series chipsets
- **Atlantic NIC has independent flakiness** (link flap at Mar 6 01:52 with no other symptoms), but its MMIO hangs during the lockup events are caused by the shared chipset failure, not the NIC itself
- **Frequency is increasing**: incidents 1–3 were every 1–2 days. Incidents 5–6 were back-to-back on Mar 10 (2.5h and 5.5h uptime respectively)
- **~06:30 is the witching hour**: incidents 2, 3, and 6 all had their fatal lockup around 06:30. Investigated: `apt-daily-upgrade.timer` fires at 06:00+random(60m), but the last run (06:19:18 on boot `c2a8b888`) completed instantly with nothing to upgrade. `locate.service` fires at 00:00 and does a full filesystem scan (coincides with midnight-onset incidents 3, 5), but doesn't explain 06:30 hangs. `cups.service` retry-loops every ~90s around 06:30 in all incidents but is benign. **No specific I/O-heavy job triggers the 06:30 crashes** — the timing correlation is likely coincidental, reflecting that most boots happen overnight and the chipset fails after ~5-6h of uptime
- SMART health: all 4 drives PASSED, zero reallocated/pending/uncorrectable sectors, temps 31–40°C
- ZFS pools: ONLINE with zero errors after every powercycle

### Uptime Comparison by Configuration

| Configuration                         | Avg uptime                           | Notes                            |
| ------------------------------------- | ------------------------------------ | -------------------------------- |
| Kernel 6.8 + GPUs                     | **~7d** (3d20h, 12d5h, 10d6h, 1d20h) | Rock solid                       |
| Kernel 6.17 + GPUs on old VM (10100)  | **~14h**                             | Crashes with hours of lead time  |
| Kernel 6.17 + GPUs on new wyrm2 (110) | **~7h**                              | Worse                            |
| Kernel 6.17 + no GPU passthrough      | **~2d** (2d10h best)                 | Better but still has ata7 errors |
| Kernel 6.17 + wyrm2 autostart + GPUs  | **~1m**                              | Instant VFIO death               |

**Working model**: Three distinct failure modes:

1. **Slow-onset chipset failures (incidents 1–6, 11)**: ata7 SATA errors appear hours after boot, escalate over 1–6h to full chipset PCIe fabric dropout. The AMD 800 Series chipset's internal PCIe fabric drops out, taking all downstream devices offline. **Incident 11 confirmed this pattern persists even with GPU passthrough completely removed.** DIPM (enabled by kernel 6.17's default `min_power` LPM policy) is the likely cause — `ahci.mobile_lpm_policy=1` deployed. No SATA errors have been observed since LPM was set to `max_performance`.
2. **VFIO-triggered instant crashes (incidents 7–10, 12, 14)**: System freezes within 30–120 seconds of boot, immediately after VFIO resets the 2x RTX 5090 GPUs for wyrm2. No SATA errors logged. Disabling VMs makes atlas stable. These crashes match the pattern of the **known Blackwell FLR bug** (see "Known Blackwell FLR Bug" section above). **`disable_idle_d3=1` did not prevent incident 12.** `nvidia-drm.modeset=0` may help — incident 15 (single GPU, modeset=0) survived where incidents 7–10 and 14 (2 GPUs, no modeset=0) crashed instantly.
3. **Memory pressure / ZFS ARC starvation (incident 15)**: wyrm2's memory was bumped to 112 GiB (balloon=0) ~1-2 days before this boot. With 128 GiB total, wyrm2 + Talos CP (8 GiB) = 120 GiB, leaving only ~8 GiB for the host, ZFS ARC, and kernel. ZFS ARC reports `memory_available_bytes = -1.9 GiB` (negative!), `arc_no_grow = 1`, and ARC size (3.6 GiB) is below `c_min` (3.9 GiB). ZFS zvol write threads blocked 122+ seconds on `dbuf_read → cv_wait` (waiting for ARC-evicted buffers to be re-read from disk). The `txg_quiesce` thread also blocked, preventing transaction group commits. `journald` reported "Under memory pressure, flushing caches" at 3836s and 3862s, immediately before the ZFS stall at ~3934s. Load average hit 101 as processes piled up waiting for ZFS I/O. **This failure mode is new with the recent memory increase** — earlier incidents (7-14) had different wyrm2 memory configs.

   **Thunderbolt bridge D3cold is a red herring.** The Thunderbolt 4 bridge (`pcieport 18:00.0`) D3cold failures happen on **every boot** (including stable 2.5-day boot `df5a691c`) and are a pre-existing condition caused by the Thunderbolt subsystem's runtime PM (`auto` control on TB devices `0-0`, `0-1`). Config space reads `0xFF` (device fully offline, `Unknown header type 7f`). The D3cold failures are unrelated to the stalls — they're the kernel periodically trying (and failing) to wake the sleeping Thunderbolt bridge. PCI-level `d3cold_allowed=0` and `power/control=on` have **no effect** because the Thunderbolt/USB4 subsystem controls the power state independently.

The slow-onset failures are **not** caused by VFIO — they occur independently (incident 11). The incident 15 stalls are caused by **memory overcommit**, not PCIe bridge issues.

## Available Sensors

Installed `lm-sensors` (2026-03-10). Available readings via `asusec` ISA adapter and other hwmon drivers:

| Sensor                 | Source  | Typical value | Notes                                            |
| ---------------------- | ------- | ------------- | ------------------------------------------------ |
| CPU (Tctl)             | k10temp | ~63°C         |                                                  |
| CPU CCD1/CCD2          | k10temp | ~52–54°C      |                                                  |
| CPU (EC reading)       | asusec  | ~49°C         |                                                  |
| CPU Package            | asusec  | ~60°C         |                                                  |
| Motherboard            | asusec  | ~36°C         | Closest proxy for chipset temp (not chipset die) |
| VRM                    | asusec  | ~57°C         |                                                  |
| T_Sensor               | asusec  | -62°C         | Disconnected (no external probe)                 |
| DDR5 DIMMs ×4          | spd5118 | 45–48°C       |                                                  |
| Atlantic NIC (PHY/MAC) | enp12s0 | ~55°C         |                                                  |
| NVMe                   | nvme    | ~56°C         |                                                  |
| GPU (edge)             | amdgpu  | ~49°C         |                                                  |
| GPU vddgfx             | amdgpu  | ~1.32V        | Only voltage rails available                     |
| GPU vddnb              | amdgpu  | ~0.91V        |                                                  |
| CPU_Opt fan            | asusec  | ~720–860 RPM  | Only fan reading                                 |

**Not available:** PSU voltage rails (3.3V, 5V, 12V), chipset die temperature, chassis fans. The `asusec` driver exposes no `in*_input` channels — requires multimeter for PSU voltages.

## Scheduled Jobs Inventory

Checked as potential triggers for the recurring ~06:30 and ~00:00 hangs.

| Job                       | Schedule                  | I/O impact                   | Correlation                                                                                                  |
| ------------------------- | ------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `apt-daily-upgrade.timer` | 06:00 + random(60m)       | Heavy (apt upgrade)          | Fires in 06:00–07:00 window but last run completed instantly (nothing to upgrade). Not the trigger           |
| `apt-daily.timer`         | 06:00,18:00 + random(12h) | Moderate (apt update)        | Wide random window, not consistently at 06:30                                                                |
| `locate.service`          | 00:00 daily               | Heavy (full filesystem scan) | Coincides with midnight-onset incidents (3, 5). Was running when ata7 first errored in incident 5 (00:01:47) |
| `pve-daily-update.timer`  | 01:00 + random(5h)        | Moderate                     | Wide random window                                                                                           |
| `logrotate.timer`         | ~00:00 + random           | Light                        | Not significant                                                                                              |
| `fstrim.timer`            | Weekly (Mon)              | Moderate (SSD trim)          | Not relevant — SATA drives are HDDs                                                                          |
| ZFS scrub cron            | 2nd Sunday 00:24          | Heavy                        | Monthly only, not correlated with incident frequency                                                         |
| ZFS trim cron             | 1st Sunday 00:24          | Moderate                     | Monthly only                                                                                                 |
| `cups.service`            | Retry-loops every ~90s    | Negligible                   | Present in all 06:30 incidents but benign — just socket timeout                                              |

**Conclusion**: `locate.service` may stress the chipset at midnight but doesn't explain 06:30 hangs. The ~06:30 timing reflects typical uptime-to-failure (~5-6h) from overnight boots, not a scheduled trigger.

## Interventions Log

### Mar 6 ~17:35 — Partial cable reseat and reroute

The motherboard has 4 SATA connectors: 2 on the right side, 2 on the bottom.

**Done:**

- Reseated both SATA cables on the **right side** of the motherboard
- Reseated SATA cables on the **disk side** of all drives
- Rerouted the 2 right-side cables — they had been at a questionable 90-degree twist, now rerouted to follow a less strained path

**Not done:**

- The 2 **bottom** SATA connectors were not reseated — accessing them requires removing probably both GPUs, too inconvenient for now

**Result:** Did not help. Incident 3 occurred ~29h later with identical pattern plus USB controller death.

### Mar 10 ~13:35 — Force-disable ASPM and runtime PM on chipset devices

**Discovery:** `pcie_aspm=off` was already in `/etc/kernel/cmdline` but **had no effect** — ASPM L1 remained enabled on chipset devices `04:00.0`, `0e:00.0`, `10:00.0`, `11:00.0`. The kernel ASPM policy showed `[default]`, meaning the parameter only prevented the kernel from _adding_ ASPM, but didn't clear ASPM already enabled by BIOS/firmware.

**Done:**

- Force-cleared ASPM L1 on all chipset devices via `setpci -s $dev CAP_EXP+10.w=0000:0003` (clears bits 0:1 of Link Control register)
- Disabled PCIe runtime PM on chipset devices (`echo on > /sys/bus/pci/devices/0000:$dev/power/control`)
- Created persistent udev rule `/etc/udev/rules.d/99-disable-chipset-aspm.rules` to apply both on every boot
- Affected devices: `04:00.0` (chipset PCIe switch), `0e:00.0` (xHCI USB), `10:00.0` (xHCI USB #2), `11:00.0` (AHCI SATA), `0c:00.0` (Atlantic NIC)

**Verified:** All chipset devices now show `LnkCtl: ASPM Disabled` and `power/runtime_status: active`.

**Rationale:** Every fatal lockup hits `pci_pm_runtime_resume` → `pci_mmcfg_read` spinning on a device in L1 that never wakes. Keeping links always active and devices never suspended removes both the L1 transition that may destabilize the fabric and the resume code path that causes the infinite spin.

**Status:** Incident 11 showed ata7 errors still occur with ASPM disabled and runtime PM off. PCIe ASPM and SATA DIPM are independent mechanisms — disabling ASPM doesn't affect DIPM (see "Root Cause" section). The ASPM fix may still help prevent the lockup cascade (by avoiding the `pci_pm_runtime_resume` spin when the chipset fabric drops).

### Mar 11 ~03:26 — Ansible adds `ahci.mobile_lpm_policy=0`

Ansible playbook ran during clean boot `ad2bbb56`, adding `ahci.mobile_lpm_policy=0` to `/etc/kernel/cmdline`:

```
root=ZFS=rpool/ROOT/pve-1 boot=zfs amd_iommu=on iommu=pt pcie_aspm=off ahci.mobile_lpm_policy=0
```

**Rationale:** Disables AHCI Aggressive Link Power Management for all ports, preventing the SATA PHY from entering low-power states (`Partial`/`Slumber`) that may not recover cleanly on the chipset's AHCI controller.

**Status:** ata7 errors still appeared in incident 11 (before this parameter was applied). Not yet tested long enough post-application. Incident 12 crashed from VFIO, not SATA.

### Mar 11 ~03:26/03:40 — Ansible deployed with VFIO D3cold workaround

Ansible playbook ran twice during boot `ad2bbb56` (02:52–03:48):

- First run at 03:26: added `ahci.mobile_lpm_policy=0` to kernel cmdline, VFIO modules to `/etc/modules` (including stale `vfio_virqfd`)
- Second run at 03:40: deployed `disable_idle_d3=1` in `/etc/modprobe.d/vfio-pci.conf` and udev rule `/etc/udev/rules.d/99-vfio-gpu-no-d3cold.rules`

Boot `ad2bbb56` rebooted cleanly at 03:48. **Boot `dcad0e96` (incident 12) started at 03:49 with both D3cold mitigations on disk** — yet crashed within 2 minutes of VFIO GPU resets. `disable_idle_d3=1` alone does not prevent the VFIO instant-crash failure mode.

### Mar 11 ~03:57 — Remove GPU passthrough from all VMs (again) + further ansible

After incident 12, removed `hostpci0`/`hostpci1` from all VMs, disabled autostart, stopped all running VMs. Further ansible deploy on boot `784e8c60` added `vfio_virqfd` removal.

**Boot `784e8c60` status** (03:54 → ongoing):

- VFIO GPU resets at 03:55 (both GPUs) — VMs auto-started before our intervention with GPUs still in config. System **survived** the resets (non-deterministic — incident 11 also survived).
- After intervention: GPUs removed, VMs restarted without GPUs (wyrm2 + talos running).
- No SATA errors after ~25 min uptime. Monitoring.

### Mar 11 ~18:28 — Live PCIe bridge D3cold/runtime PM disable

After incident 15's second stall recovery, disabled D3cold and runtime PM on all PCIe bridges live:

```
for dev in /sys/bus/pci/devices/*/; do
  class=$(cat "$dev/class" 2>/dev/null)
  if [[ "$class" == 0x0604* ]]; then
    echo on > "$dev/power/control" 2>/dev/null
    echo 0 > "$dev/d3cold_allowed" 2>/dev/null
  fi
done
```

Also added persistent udev rule to ansible (`99-pcie-bridge-no-d3cold.rules`).

**Historical scan**: `pcieport 18:00.0` (Intel Thunderbolt 4 bridge, JHL8540) was present on every historical boot with `PME# supported from D0 D1 D2 D3hot D3cold`, but **no D3cold failure errors were logged in any prior boot**. The failures in boot `5ef3cedb` are the first recorded instances. This suggests the D3cold failures are triggered by specific workload conditions (GPU passthrough + second VM starting), not a persistent hardware defect.

### Mar 11 ~04:16 — LPM policy investigation — confirmed root cause

**Observation:** Boot `01bca0b8` (without `ahci.mobile_lpm_policy`) booted with `lpm-pol 3` (`min_power`) on all SATA ports — this enables DIPM. Boot `784e8c60` (with `=0`) shows `lpm-pol 0` (`keep_firmware_settings`), and `hdparm -I` confirms DIPM is not active.

This proves the kernel 6.17 AHCI driver defaults to `min_power` for this AMD 600 Series chipset SATA controller (`1022:43f6`). Kernel 6.8 did not. The `min_power` policy enables DIPM, which lets drives autonomously trigger SATA PHY link power state transitions that destabilize the chipset's PCIe fabric.

| Policy value | Name                   | `lpm-pol` | DIPM   | `01bca0b8` (no param) | `784e8c60` (=0) |
| ------------ | ---------------------- | --------- | ------ | --------------------- | --------------- |
| 0            | keep_firmware_settings | 0         | Off\*  | —                     | Yes             |
| 1            | max_performance        | 1         | Off    | —                     | —               |
| 3            | min_power              | 3         | **On** | **Yes (default!)**    | —               |

\* DIPM off because this board's firmware doesn't enable it.

**Action:** Switched ansible to `ahci.mobile_lpm_policy=1` (`max_performance`) — explicitly disables all SATA LPM including DIPM, regardless of firmware defaults. Needs reboot to take effect. All ports also manually set to `max_performance` via sysfs on current boot.

**D3cold workaround verified**: both RTX 5090s show `d3cold_allowed: 0`.

## EFI Variables

Readable via `/sys/firmware/efi/efivars/` after enabling "Publish HII Resources" in BIOS 2102.

**How to read:** `xxd /sys/firmware/efi/efivars/<name>-<guid>`. First 4 bytes are EFI variable attributes (typically `0x07` = BS+RT+NV), remaining bytes are the payload.

### Key variables (boot `978abe76`, BIOS 2102)

| Variable            | GUID           | Payload                   | Interpretation                                                                |
| ------------------- | -------------- | ------------------------- | ----------------------------------------------------------------------------- |
| `EnableDIPM`        | `a44da20b-...` | `00 00 00 00`             | SATA DIPM disabled in firmware (4 bytes, one per AHCI controller port group?) |
| `EnableHIPM`        | `a44da20b-...` | `00 00 00 00`             | SATA HIPM disabled in firmware                                                |
| `PcieExpressNative` | `ec87d643-...` | `01`                      | PCIe Express Native control enabled (OS manages ASPM/AER)                     |
| `PcieSataModVar`    | `5e9a565f-...` | `02 02 02`                | SATA mode per controller (0x02 = AHCI for all 3)                              |
| `CpuTempMaxLimit`   | `4034591c-...` | `55` (85 decimal)         | CPU thermal throttle limit: 85°C                                              |
| `AntiSurgeStatus`   | `4034591c-...` | 16 bytes                  | ASUS Anti-Surge (over-voltage protection) status — no trips recorded          |
| `Usb4FwVersion`     | `4034591c-...` | `24 10 22 20 00 11`       | Thunderbolt 4 firmware version                                                |
| `AMD_RAID`          | `fe26a894-...` | `00 00 00 00 00 00 00 00` | AMD RAID disabled (all zeros)                                                 |

### Notable observations

- **`EnableDIPM` = 0 and `EnableHIPM` = 0** confirm the firmware does not enable SATA power management. The DIPM issue was purely kernel 6.17's `min_power` default LPM policy overriding the firmware setting. `ahci.mobile_lpm_policy=1` in kernel cmdline prevents this.
- **`PcieExpressNative` = 1** means the OS (not firmware) controls PCIe native features including ASPM. This is why `pcie_aspm=off` in the kernel cmdline is effective — the firmware delegates ASPM control to Linux.
- The `Setup` var (365 bytes) and `AMD_PBS_SETUP` (256 bytes) contain the full BIOS configuration but require IFR extraction from the BIOS ROM to decode field offsets. No IFR extraction tools are installed on atlas.
- `BiosSettingMappingTableV2` (4596 bytes) is the ASUS mapping table linking string IDs to Setup var offsets — binary format, would need the HII string database to decode.

## Recommended Next Steps

### Next session checklist

1. ~~**Apply wyrm2 NixOS config with `nvidia-drm.modeset=0`**~~ **Done** (2026-03-11).
   - `cd ~/code/ducktape && git pull` on wyrm2 (as agentydragon)
   - `sudo nixos-rebuild switch --flake ~/code/ducktape#wyrm2`

2. ~~**Reduce wyrm2 memory allocation**~~ **Done** (2026-03-11). Reduced from 112 GiB to 96 GiB (`memory: 98304`) in both Proxmox (`qm set`) and Terraform. Ballooning is incompatible with VFIO (passthrough requires pinned memory). Takes effect on next wyrm2 restart. Leaves 24 GiB for host+ARC instead of 8 GiB.

3. ~~**Deploy PCIe bridge D3cold/runtime PM disable**~~ **Not needed** (2026-03-11). Investigation showed `18:00.0` D3cold failures occur on every boot (including stable 2.5-day boots) and are benign — the Thunderbolt subsystem controls power independently of PCI sysfs settings. The udev rule in ansible (`99-pcie-bridge-no-d3cold.rules`) is harmless but ineffective for the Thunderbolt bridge.

4. ~~**Post-BIOS-update verification**~~ **Done** (2026-03-12, boot `978abe76`):
   - [x] Kernel cmdline: `ahci.mobile_lpm_policy=1`, `pcie_aspm=off` confirmed present
   - [x] ASPM disabled on all chipset devices: `04:00.0`, `11:00.0`, `0e:00.0`, `0c:00.0` all show `LnkCtl: ASPM Disabled`
   - [x] wyrm2 running with 96 GiB (`memory: 98304`)
   - [x] ZFS ARC healthy: `memory_available_bytes` = 799 MB (positive), `size` = 9.3 GiB > `c_min` = 3.9 GiB
   - [x] EFI vars readable (see "EFI Variables" section)

5. **Continue incremental GPU passthrough testing** (kernel 6.17, `nvidia-drm.modeset=0` active, `mobile_lpm_policy=1` active):
   - [x] **Step A**: One GPU only (`hostpci0` 01:00.0, Zotac) + wyrm2 alone — **stable ~33 min**. VFIO resets succeeded.
   - [x] **Step B**: Start Talos CP VM (10000) alongside wyrm2+1 GPU — **stalled but recovered twice** (incident 15). Root cause: **memory overcommit**. wyrm2 (112 GiB, balloon=0) + Talos CP (8 GiB) = 120 GiB out of 128 GiB total, leaving ZFS ARC starved (`memory_available_bytes = -1.9 GiB`). Thunderbolt bridge D3cold failures are a red herring (happen on all boots including stable ones).
   - [x] **Step B (retry with reduced memory)**: wyrm2 reduced to 96 GiB. Running with Talos CP — **stable 7h+** (boot `978abe76`).
   - [x] **Step C**: Both GPUs (Zotac 01:00.0 + Gigabyte 03:00.0) passed through to wyrm2 — **stable 7h+**.
   - [x] **Step D**: Both GPUs + Talos CP VM — **full production config stable 7h+**. Zero errors.

6. **If still crashing**: investigate **GPU UEFI firmware update** (NVIDIA GPU UEFI Firmware Update Tool) — community reports this fixes the Blackwell FLR bug at the source.

Note: `ahci.mobile_lpm_policy=1` is active on current boot. Kernel 6.8 is **not** a reliable fix — incident 13 crashed on 6.8 too (though it survived longer than 6.17 boots). Some apparent "hard lockups" with short uptimes may have been recoverable stalls killed prematurely by holding the power button.

### Completed mitigations

1. ~~**Disable SATA link power management**~~ **Done** (2026-03-11). Ansible sets `ahci.mobile_lpm_policy=1` (`max_performance`). No SATA errors observed since deployment.

2. **Blackwell FLR bug workarounds** — the VFIO instant crashes match the widely-reported RTX 5090/Blackwell FLR bug (see "Known Blackwell FLR Bug" section). `disable_idle_d3=1` did not prevent incident 12. Status of workarounds:

   a. ~~**NVIDIA driver 580+**~~ **Already present** — wyrm2 runs NVIDIA 580.119.02 (`nvidia-x11-580.119.02-6.12.74`). Did not prevent incident 12.

   b. **`nvidia-drm modeset=0`** — **Deployed and applied** (2026-03-11) via `boot.kernelParams` in `nix/nixos/hosts/wyrm2/default.nix`. `nixos-rebuild switch` completed. Testing in progress — single GPU (Zotac, 01:00.0) stable so far on kernel 6.17.

   c. **Boot into kernel 6.8** — kernel 6.8.12-18-pve is still installed on atlas. Kernel 6.8 ran VFIO passthrough of the same GPUs (via VM 10100) for 12+ days without crashes. If GPUs work on 6.8 but not 6.17, confirms kernel reset code path changes interact badly with the Blackwell FLR bug.

   d. **Update GPU UEFI firmware** — NVIDIA provides a GPU UEFI Firmware Update Tool. Community reports this fixes the FLR bug for some users.

   e. **Slim down wyrm2 / try lightweight VM** — the old Talos GPU worker (16 cores, small RAM, few disks) worked on kernel 6.8. If 6.8 also crashes with the heavy wyrm2 config, the VM weight is a contributing factor independent of the kernel.

   f. **Blacklist `snd_hda_intel` for GPU audio** — `snd_hda_intel ... GPU sound probed, but not operational: please add a quirk to driver_denylist` appeared in crash logs. The host HDA driver probing GPU audio during VFIO handoff may interfere. Add to `/etc/modprobe.d/`: `options snd_hda_intel enable=0,0` or bind GPU audio to `vfio-pci` explicitly.

### Previous mitigations (done)

4. ~~**Disable PCIe runtime PM for chipset devices**~~ **Done** (2026-03-10). Applied live and persisted via udev rule. See intervention log above.

5. ~~**Disable ASPM on chipset devices**~~ **Done** (2026-03-10, reinforced 2026-03-11). `pcie_aspm=off` in cmdline + `setpci` udev rule + BIOS Native ASPM set to "Disabled" (BIOS-controlled) during 2102 update. Triple-layered: firmware won't enable ASPM, kernel won't add it, udev clears any remnants.

6. ~~**Disable VM autostart**~~ **Done** (2026-03-10). All VMs set to `onboot: 0`. Atlas stable without VMs.

7. ~~**Remove GPU passthrough from wyrm2, re-enable VMs**~~ **Done** (2026-03-10, redone 2026-03-11). Removed `hostpci0`/`hostpci1` from all VMs. Re-tested with GPUs in incident 12 — still crashes. Slow-onset ata7 errors confirmed independent of GPU passthrough (incident 11).

### If GPU passthrough turns out to be incompatible

The 2x RTX 5090 GPUs are needed for Ollama in the k8s cluster. If VFIO passthrough to a VM can't be stabilized, options:

1. **Run NVIDIA + kubelet directly on Proxmox host** — skip the VM layer entirely. Install NVIDIA drivers and a kubespand/k8s-worker setup on the Proxmox Debian host, joining atlas itself as a k8s worker node (similar to how rugged joins via `k8s-worker.nix`). Avoids VFIO entirely. Downside: mixing k8s worker duties with the hypervisor host.

2. **Slim down wyrm2 to match the old working GPU worker** — the old Talos GPU worker that worked fine was minimal (16 cores, no SPICE, few disks, purpose-built). Try a stripped-down VM with just the GPUs, minimal RAM, and no SPICE/virtiofs/25 PVC disks. The old config worked; the new one is much heavier.

3. **Pass through one GPU instead of two** — halves the VFIO reset pressure. Test if single-GPU passthrough is stable.

4. **BIOS update** — AGESA updates (3 versions behind) may fix GPU VFIO reset handling. Worth trying before giving up on passthrough.

5. **LXC/container passthrough instead of VFIO** — Proxmox supports passing GPU devices to LXC containers without VFIO. Less isolation but avoids the VFIO reset path entirely. Requires NVIDIA drivers on the host.

### High priority — chipset-level investigation

3. **Check chipset heatsink and airflow** — the AMD 800 Series chipset handles SATA + USB + NIC + PCIe switching. If its heatsink has poor contact or no airflow, thermal runaway could cause the PCIe fabric to drop. Clean dust, verify heatsink is seated, consider adding a fan.

4. ~~**Update BIOS**~~ **Done** (2026-03-11). Flashed to BIOS 2102 (AGESA 1.3.0.0a) via EZ Flash. BIOS settings applied: Native ASPM set to "Disabled" (BIOS-controlled), AC power loss set to "Last State", RTC wake enabled (every 2h as safety net), HII resources published, SR-IOV left disabled. PSU voltages confirmed healthy in BIOS: 12V=11.808V, 5V=5.040V, 3.3V=3.328V, CPU core=1.279V.

5. **Consider an HBA card** — a dedicated LSI/Broadcom HBA (e.g., 9300-8i in IT mode) would move SATA off the failing chipset entirely. Given the chipset-level PCIe failures, this may be the most practical workaround regardless of root cause.

6. **File an ASUS support ticket** — the pattern (SATA + USB + NIC all dying simultaneously behind the chipset PCIe switch) is distinctive. May be a known X870E issue or warrant an RMA.

### Lower priority

7. **Reseat remaining 2 bottom SATA cables** — unlikely to help given USB and NIC also fail, but eliminates the last cable variable.

8. **Check PSU voltages** — marginal 3.3V/5V could starve the chipset. Requires a multimeter on a SATA or Molex power connector — software cannot read PSU rails on this board. `lm-sensors` installed (2026-03-10); `asusec` ISA adapter exposes only temps (CPU, CPU Package, Motherboard, VRM, T_Sensor) and one fan (CPU_Opt). No `in*_input` voltage channels. Also check the 24-pin ATX connector for loose/corroded pins.

9. ~~**Run a ZFS scrub**~~ **Done** (2026-03-04). Completed with 0 errors. ZFS pools healthy after all powercycles.

### Monitoring

10. **Add chipset error monitoring** — a cron/systemd timer that watches `journalctl -k` for `ata.*SError|AHCI.*unavailable|soft lockup|xHCI.*not responding` and alerts (e.g., Healthchecks.io or webhook). Early detection won't prevent the hang but could enable a clean shutdown before cascade.

11. ~~**Set up smartd email alerts**~~ Partially done (2026-03-04) — `postfix`/`smartd` configured but **alerts are not reaching actual mailbox**. Needs mail delivery verification.
