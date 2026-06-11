# Atlas Ethernet — Recurring Link Failures

## Summary

Atlas (Proxmox host, ASUS ProArt X870E-CREATOR WIFI) has recurring ethernet link failures. The motherboard has two NICs: Intel I226-V (2.5G, `enp11s0`, igc driver) and Aquantia AQC113CS (10G, `enp12s0`, atlantic driver). The active NIC is the Aquantia, bridged into `vmbr0`. The cable is **self-crimped**.

Two distinct failure modes observed:

1. **Atlantic NIC goes down mid-session** as part of SATA/chipset cascade (see `atlas_sata_2026_03_04.md`)
2. **No link detected at boot** — both NICs show "Link detected: no" and never establish a link

## Hardware

| NIC               | PCI       | Driver   | Interface | Speed | Port                       |
| ----------------- | --------- | -------- | --------- | ----- | -------------------------- |
| Intel I226-V      | `0b:00.0` | igc      | `enp11s0` | 2.5G  | Unused (backup)            |
| Aquantia AQC113CS | `0c:00.0` | atlantic | `enp12s0` | 10G   | Active, bridged to `vmbr0` |

Both NICs are on the AMD Promontory chipset (same PCIe fabric as the SATA controller). The Intel NIC runs at only PCIe x1 (4 Gb/s) due to PCIe lane pressure from 2x RTX 5090 GPUs.

## Interface Naming History

Adding GPUs shifted PCI bus enumeration. Names are stable since then:

| Period                   | Intel I226-V         | Aquantia AQC113CS    | Cause                        |
| ------------------------ | -------------------- | -------------------- | ---------------------------- |
| Before GPUs (pre-Jan 22) | `enp10s0` (bus `0a`) | `enp11s0` (bus `0b`) | Original PCI layout          |
| After GPUs (Jan 22+)     | `enp11s0` (bus `0b`) | `enp12s0` (bus `0c`) | GPUs shifted PCI enumeration |

This was a one-time shift, not random instability. Names derived from PCI slot (`enp<bus>s<slot>`) are deterministic given the same hardware config.

## PCI BAR Address Conflicts

Every boot (working and non-working) shows PCI BAR conflicts:

```
pci 0000:00:02.1: bridge window [mem 0xdc400000-0xdd4fffff]: can't claim; address conflict with AMDIF031:00
pci 0000:0b:00.0: BAR 0 [mem ...]: can't claim; no compatible bridge window  (Intel NIC)
pci 0000:0c:00.0: BAR 0 [mem ...]: can't claim; no compatible bridge window  (Aquantia NIC)
```

These conflicts are present even on boots where networking works fine. The 2x RTX 5090 GPUs consume substantial PCI address space and create resource pressure, but the BAR conflicts alone do not prevent the NICs from functioning. The drivers initialize and operate normally despite unclaimed BARs (likely using fallback MMIO mappings).

## Incident Log

### Jan 10 — No link on either port

**File**: `~/pve-notes` (created 2026-01-10 15:59)

Neither RJ45 port showed link activity. No blinking lights. `vmbr0` bridge had `dhclient` timeout. At this time, config bridged through `enp11s0` (Intel, which was `enp10s0` before GPU addition).

### Jan 22 — Interface name shift after adding GPU

**Files**: `~/networking-logs` (12:35), `~/net-interface-issue` (19:07)

After adding a second GPU, PCI bus numbers shifted. The config still referenced `enp11s0` (Intel) for `vmbr0`, but the Intel NIC was now `enp11s0` (was `enp10s0`) and both NICs showed no link. Multiple reboots (boots -17 through -14 in journal, 12:19–19:09).

Resolution: switched `vmbr0` bridge to `enp12s0` (Aquantia). Network came up on boot -13 (19:10:15) with `link change old 0 new 100` at 19:10:45.

Boot -13 ran until Feb 1 (10 days uptime).

### Feb 27–Mar 5 — Atlantic link drops during SATA cascades

The atlantic NIC link drops are entangled with SATA controller failures (documented in `atlas_sata_2026_03_04.md`):

| Boot | Date      | Network status            | SATA issues            | Atlantic fate                                                                        |
| ---- | --------- | ------------------------- | ---------------------- | ------------------------------------------------------------------------------------ |
| -4   | Feb 27–28 | Worked (link at 19:18:24) | SATA errors from 00:04 | No explicit NIC drop logged before hang                                              |
| -3   | Mar 4–5   | Worked (link at 20:53:09) | SATA errors from 02:38 | `link change old 100 new 0` at 06:47:04, then hang                                   |
| -2   | Mar 5–6   | Worked (link at 13:24:15) | SATA error at 13:34:42 | `link change old 100 new 0` at 00:02:55, then **driver crash** in `aq_hw_read_reg64` |

The atlantic link drops in these incidents appear to be a **consequence** of the chipset-level SATA cascade, not an independent network problem. Both the SATA controller and Aquantia NIC are behind the same Promontory chipset.

### Mar 6 — No link after atlantic driver crash

**File**: `~/2026-03-06-network-problems` (01:13)

Boot -2 ended with the atlantic driver crashing (`RIP: 0010:aq_hw_read_reg64`). On the next boot (-1 at 00:57), network never came up — **before** the case was opened. User then opened the case to reseat SATA cables and rebooted to boot 0 (01:17) — still no link. Unplugging and replugging the cable did not help.

Three consecutive boots without link (boot -1, boot 0, and after driver reload on boot 0). The cable was not touched between boot -2 (working) and boot -1 (not working).

**Attempted remediation on boot 0:**

- `modprobe -r atlantic && modprobe atlantic` + `ifreload -a` — **did not help**, still no link. Driver reloaded cleanly (detected `ATL2FW 1030018`, re-added to bridge, no errors) but no `link change` event appeared. The driver-level reload does not power-cycle the PHY.
- Cold boot (full power off) — **already happened** between boot -2 and boot -1 (54 min gap, machine was hung so recovery required physical power cycle). Still no link on boot -1. This weakens the "stuck NIC firmware" theory — a power cycle already occurred and didn't help.
- Another cold boot pending anyway, but expectations lowered. More likely cable or switch port.
- **Loopback diagnostics** confirm NICs are healthy:
  - Aquantia: PHY internal loopback **PASS** (10G, link detected). PHY external loopback **FAIL** (no link). Problem is at/beyond the RJ45 connector.
  - Intel: register, EEPROM, interrupt, loopback self-tests all **PASS**. Only link test fails (no cable).
  - Both NICs' firmware and hardware are fine. The problem is definitively **cable, connector, or switch port**.

Key difference from SATA-cascade incidents: drivers load cleanly, no errors, no crashes — just no physical link. SATA drives appear healthy on these boots.

The atlantic driver crash on the immediately preceding boot (-2, RIP in `aq_hw_read_reg64` at 00:03) is suspicious but the NIC initializes cleanly on subsequent boots.

### Mar 7 — Replaced cable, link restored (monitoring)

Replaced the self-crimped cable with a factory-made Cat6a cable (Amazon). Link came up immediately. Whether this makes the network stable long-term remains to be seen — the SATA/chipset cascade is a separate potential cause of link drops.

## Current Network Config

```
# /etc/network/interfaces
# vmbr0 bridges through enp12s0 (Aquantia 10G)
# enp11s0 (Intel 2.5G) is unused
auto vmbr0
iface vmbr0 inet dhcp
    bridge-ports enp12s0
    bridge-stp off
    bridge-fd 0
```

## Analysis

Two distinct failure modes:

### Mode 1: Atlantic drops mid-session

Previously attributed to the SATA/chipset cascade (NIC as collateral damage). However, UniFi data shows the link was **already flapping independently** throughout the month — disconnecting every few hours even when SATA was fine. The atlantic link drops during SATA incidents may have been **coincidental cable flaps**, not chipset collateral.

That said, the SATA cascade _can_ kill the NIC (the `aq_hw_read_reg64` crash on boot -2 proves the chipset-level failure reaches the NIC). Both causes may be real — cable flaps are the chronic issue, chipset cascades are the acute one.

### Mode 2: No link at boot (current problem)

Both NICs detect no link. This is a **physical layer** problem — the drivers initialize fine but the PHY sees no partner. Possible causes:

1. **Bad cable (most likely)**: Self-crimped cable. Marginal crimp could fail intermittently. Opening the case and reseating the mobo could have stressed the RJ45 connector. Even though the cable wasn't intentionally unplugged, mobo movement could dislodge a marginal connection.
2. **Switch port failure**: The upstream switch port may have died.
3. **Cable in wrong port**: If the cable is in the Intel port (`enp11s0`) but `vmbr0` bridges through Aquantia (`enp12s0`), DHCP would fail. However, ethtool shows no link on _either_ NIC, so this alone doesn't explain it.
4. **NIC PHY damage**: Unlikely for both NICs simultaneously, unless it's a chipset-level issue. The PCI BAR conflicts are suspicious but present on working boots too.

~~Initially the timeline seemed to weaken the cable theory (cable wasn't touched between working boot -2 and non-working boot -1). However, UniFi controller logs reveal the link was **chronically unstable for a month** — see UniFi data below.~~

**The self-crimped cable is almost certainly the root cause.** UniFi logs show 75 disconnect events in one month with progressively shorter connection durations (from 12+ days to hours), consistent with a degrading cable/crimp. The final complete failure on Mar 6 is just the endpoint of this degradation.

## USB Tethering Fallback

**File**: `~/enable-usb-tether` (2025-07-09)

```bash
ip link set enxe23d47d0e16d up
dhclient -v enxe23d47d0e16d
# NAT for VMs:
echo 1 > /proc/sys/net/ipv4/ip_forward
iptables -t nat -A POSTROUTING -o enxe23d47d0e16d -j MASQUERADE
```

A USB tethering interface (`enxe23d47d0e16d`) is present in the current boot's `/sys/class/net/`.

## UniFi Controller Data (Mar 6)

Atlas (`68:e8` = Aquantia NIC) connects through a dumb (unmanaged) switch to wolf-gateway Port 2. Path: NIC → self-crimped cable → wall socket → in-wall wiring → **dumb switch** (apartment patch panel area) → wolf-gateway Port 2. The gateway sees atlas on Port 2 because that's the dumb switch's uplink.

**75 "Wired Client Disconnected" events** from Feb 9 – Mar 6:

| Period       | Typical connection duration | Disconnects | Pattern                    |
| ------------ | --------------------------- | ----------- | -------------------------- |
| Before Feb 9 | 12+ days                    | Rare        | Stable                     |
| Feb 9–24     | 5–14 hours                  | ~2–3/day    | Degrading                  |
| Feb 24–Mar 5 | 1–18 hours                  | ~2–3/day    | Actively flapping          |
| Mar 6 00:13  | Final disconnect            | —           | **Dead** — no reconnection |

Last disconnect: **Mar 6 00:13:31** ("Time Connected: 1d 3h"). This aligns with the atlantic driver crash at 00:02:55 on boot -2.

**Correlation with boots**: 71 of 75 disconnects are **mid-boot flaps** — the physical link dropped while the system was running, unrelated to reboots. Only 4 coincide with boot boundaries. The flap rate accelerated over time:

| Boot           | Uptime   | Flaps/day |
| -------------- | -------- | --------- |
| -11 (Feb 1–13) | 12 days  | 1.4/day   |
| -9 (Feb 13–24) | 10 days  | 2.9/day   |
| -8 (Feb 24–25) | 44 hours | 6.1/day   |
| -7 (Feb 25–26) | 9 hours  | 13.8/day  |
| Mar 6+         | —        | Dead      |

This is a textbook progressive cable/connector degradation curve.

The progressive shortening of connection durations from days to hours over a month is a classic sign of a **degrading physical connection** (cable crimp, connector, or wall jack).

**Note**: The "dangered Switch" and "dangered Server Switch" (both USW Flex 2.5G 5) are **not** in the atlas path. The intermediate switch is unmanaged (no telemetry).

## Resolution

**Cable replaced** on Mar 7 with factory-made Cat6a — link came up immediately. Monitoring whether this resolves the chronic flapping. The SATA/chipset cascade remains a separate potential cause of link drops.

## Remaining Next Steps

1. **Monitor for link flaps** — `journalctl -f -k | grep atlantic` to verify the new cable stays stable. If flaps recur, the SATA/chipset cascade is a separate problem to address.

2. **Pin interface names by MAC** — add udev rules to assign stable names regardless of PCI enumeration:

   ```
   # /etc/udev/rules.d/70-persistent-net.rules
   SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="bc:fc:e7:e9:68:e7", NAME="eth-intel"
   SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="bc:fc:e7:e9:68:e8", NAME="eth-aquantia"
   ```

   Update `/etc/network/interfaces` to match. This prevents future confusion if PCI layout changes again.

3. **Address PCI BAR conflicts** — try BIOS option "Above 4G Decoding" (should already be on for GPU passthrough) and "Resize BAR" settings. The `AMDIF031:00` conflict with the bridge window is the root cause of the cascading `can't claim` errors.

## Source Files

All from `~` on atlas:

| File                          | Created          | Content                         |
| ----------------------------- | ---------------- | ------------------------------- |
| `pve-notes`                   | 2026-01-10       | Early network troubleshooting   |
| `networking-logs`             | 2026-01-22 12:35 | Interface naming after GPU add  |
| `net-interface-issue`         | 2026-01-22 19:07 | Detailed Jan 22 troubleshooting |
| `enable-usb-tether`           | 2025-07-09       | USB tethering fallback          |
| `2026-03-06-network-problems` | 2026-03-06 01:13 | Current incident notes          |
| `2025-01-09-21-10-dmesg.txt`  | 2026-01-09       | Full dmesg capture              |
