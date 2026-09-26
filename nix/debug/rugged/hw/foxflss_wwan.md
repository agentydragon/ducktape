# Foxconn DW5934e WWAN Investigation

Live investigation log for the Foxconn DW5934e WWAN modem setup on the Dell Rugged 12 running NixOS.

## Hardware

- Foxconn DW5934e (SDX72) — PCI `105b:e11d`
- Firmware: `FDE2.F0.0.0.1.2.TO.003.062`, carrier config: `T-mobile` rev `0A010503`
- Communicates via MBIM through `mbim-proxy` (abstract socket `@mbim-proxy`)
- Platform ID `0D67` (Dell Rugged), detected via `dmidecode`
- Device nodes: `/dev/wwan0mbim0` (MBIM), `/dev/wwan0at0` (AT, unresponsive),
  `/dev/wwan0qcdm0` (QCDM/diagnostic, not useful for eSIM)
- **SIM slots**: slot 1 = physical micro-SIM (inside battery compartment), slot 2 = eSIM (eUICC)
- **Physical SIM slot location**: remove battery, pull SIM slot cover outward. Gold contacts
  face up, notched corner aligned. Accepts micro-SIM (nano-SIM with adapter works).
  See: Dell KB article 000214805
- Modem IMEI: `356398950074094`

## What Works (as of 2026-04-30)

- **FCC unlock** via `fcc-unlock.d/105b:e11d`: MM invokes the script
  reactively when it sees `Cannot power-up: sotware radio switch is OFF`
  during enable. The script runs `FoxFlss` (FCC unlock) +
  `FoxFlss -f Check_RF_SSKU` (RF cal). Confirmed firing successfully
  starting 2026-04-30 once `dmidecode` was added to the foxflss wrapper
  PATH (see "Watchdog + dmidecode fix" section). Without dmidecode the
  binary exits 1 with `Current platform: do not support FccLock!` and
  MM gives up — the silent failure that masked the unlock path for 5+
  days.
- **FCC-unlock watchdog** (`foxflss-watchdog.service`, defined in
  `nix/nixos/hosts/rugged/foxconn-wwan.nix`): listens to MM state via
  `mmcli -m any -w` and, if the modem is stuck in
  `{enabling,disabled,failed}` + `power-state=low` for ≥ 12 s, runs
  FoxFlss + restarts MM (cooldown 120 s). Belt-and-suspenders backstop
  for the case where MM gives up after a script failure. Marked
  `CLEANUP(added 2026-04-30)` — try retiring after a month of zero fires.
- **RF calibration** (`FoxFlss -f Check_RF_SSKU`): PASSES when modem is
  in clean state. Calibration data persists in modem NVRAM — does not
  need to be re-run unless firmware is wiped. RF cal does NOT
  meaningfully improve throughput (the cap is carrier-side QoS, not
  RF-link, see "Physical SIM throughput" below).
- **MTU fix**: `ipv6.method=disabled` in NM profile drops IPv6 minimum
  MTU floor (RFC 2460 requires 1280 for IPv6), allowing `gsm.mtu=1200`
  to take effect. Path MTU ceiling on Google Fi is ~1256 B. With MTU
  1200, TLS appconnect went from 3.7 s → 0.14 s on the (rare)
  un-throttled connections; while throttled, HTTPS still fails because
  the TLS ClientHello + cert chain doesn't fit a single 1200 B segment
  and PMTU-D is suppressed by the carrier.
- **NM dispatcher script** on `wwan0 up`: wired up to run
  `FoxFlss -f Check_RF_SSKU` via `systemd-run` (transient unit
  `foxflss-rf-cal.service`). Fires after bearer is fully established.

## Modem Reset Methods (what works, what doesn't)

The modem is a separate computer (Qualcomm SDX72 SoC) with its own firmware.
The Linux kernel communicates with it over PCIe/MHI/MBIM but cannot directly
control the modem's internal state. Different reset methods affect different
layers:

### MHI driver unbind/rebind — clears MBIM UICC state after profile ops

```bash
systemctl stop ModemManager
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/unbind
sleep 3
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/bind
sleep 8
# lpac can now access the eUICC — MM must stay stopped
```

Resets the MHI channels and MBIM UICC state, clearing the "SelectFailed"
error that occurs after profile switch operations (disable/enable/delete of
enabled profile all trigger SIM reset → stale MBIM channel).

**Works after**: profile disable, profile delete, profile enable.
**Does NOT work after**: profile download — download leaves the ISD-R
(eSIM management applet) in a deeper wedged state that persists across
MHI rebinds. See "Post-download wedge" below.

### Bringing modem operational (no reboot)

After MHI rebind, modem is in `power state: low` (FCC locked). MM reports
`esim-without-profiles` / `failed`. To bring it up:

```bash
# MM must run (for mbim-proxy), even if modem is in failed state
systemctl start ModemManager && sleep 10
# FCC unlock (needs mbim-proxy)
FoxFlss && sleep 5
# Restart MM — now sees the profile, completes init
systemctl restart ModemManager
```

### Post-download wedge (CRITICAL)

After `lpac profile download` completes, the modem's MBIM stack becomes
**completely unresponsive**. ALL MBIM operations time out — not just ISD-R
(SelectFailed), but even basic `--query-device-caps`. The modem's firmware
is alive (dmesg shows MHI power-on after rebind) but its MBIM service
processor is wedged.

**Nothing clears this except a full system reboot:**

| Method                        | Resets PCIe? | Resets MHI? | Clears post-download wedge?   |
| ----------------------------- | ------------ | ----------- | ----------------------------- |
| `systemctl restart MM`        | no           | no          | no                            |
| MHI driver unbind/rebind      | no           | yes         | **no**                        |
| PCIe device remove/rescan     | yes          | yes         | **no**                        |
| PCIe Function Level Reset     | partial      | yes         | **no**                        |
| `soc_reset` sysfs trigger     | yes          | yes         | **no**                        |
| `mmcli --reset`               | no           | no          | **no**                        |
| `mbimcli --ms-set-uicc-reset` | n/a          | n/a         | parser broken in libmbim 1.32 |
| **System reboot**             | **yes**      | **yes**     | **yes**                       |

The `soc_reset` sysfs at `/sys/devices/.../mhi0/soc_reset` is supposed to
reset the Qualcomm SoC, and dmesg confirms it re-enumerates the MHI device
(`Power on setup success`, ports re-attach). But the modem firmware's MBIM
service does not recover — opens still time out. This suggests the modem's
MBIM processor is in a state that survives even SoC-level reset, and only
a full power cycle (PCIe slot power off during system shutdown) clears it.

### SBL-stage firmware wedge (CRITICAL — categorically different from post-download wedge)

A second class of unrecoverable-without-reboot wedge, observed
2026-04-30. Symptom in dmesg:

```
mhi mhi0: Resuming from non M3 state (SYS ERROR)
mhi-pci-generic 0000:71:00.0: failed to resume device: -22
mhi-pci-generic 0000:71:00.0: device recovery started
mhi-pci-generic 0000:71:00.0: reset failed
mhi-pci-generic 0000:71:00.0: Recovery failed: -25
… and on every subsequent rebind / rescan attempt:
mhi mhi0: Power on setup success
mhi mhi0: No firmware image defined or !sbl_size || !seg_len
mhi-pci-generic 0000:71:00.0: failed to power up MHI controller
```

The modem chip is alive at the PCIe link layer (re-enumerates fine,
PCIe `Power on setup success`), but its internal CPU is hung at the
SBL (Secondary Boot Loader) stage — when the MHI host driver reads
the firmware metadata region to start boot, the modem returns
zeros / invalid lengths and SBL never starts. `lspci` reports the
modem as `Unassigned class [ff00]` (rather than the healthy
`Wireless controller [0d40]`). This differs from the post-download
wedge: there the firmware was running and only ISD-R/MBIM was wedged;
here the firmware itself is dead.

Triggers observed:

1. **2026-04-30**: rapid MM-stop/start cycling (buggy old foxflss
   wrapper failing FCC unlock → watchdog kept restarting MM → modem
   suspended/resumed too many times → SYS ERROR). Dmesg precursor:
   `Resuming from non M3 state (SYS ERROR)` + `failed to resume
device: -22` + `Recovery failed: -25`.

2. **2026-05-14**: plain suspend/resume during normal use, no MM
   cycling, no FCC failures. No SYS ERROR precursor at all — last
   resume marker was clean (`PM: suspend exit`) and MM logged only
   `system is resuming` with no follow-up MHI events. Modem entered
   SBL wait-for-firmware state silently; only the probe attempt
   triggered by `modem.sh recover` step 1 (MHI rebind) surfaced the
   actual error in dmesg. Diagnostic signature (visible in
   `modem.sh status` after the 2026-05-14 extension): MHI section
   shows driver bound + runtime active + `mhi0 channels: NONE`,
   `/dev/wwan0*` empty, `foxflss-watchdog` hot-spinning. So the
   "rapid cycling" framing in the original write-up undersold the
   risk surface — ordinary suspend/resume is enough to reach this
   wedge.

**Deep mechanism trace + fix plan**: see <modem_suspend_research.md>.
Key code citations there pin the wedge to the
`mhi_pci_recovery_work` → `pci_try_reset_function` (FLR) → modem-in-PBL
chain, plus a DSDT-side discovery that the platform DOES have a real
WWAN slot power-cycle (`FHRF`/`SHRF`/`_RST`) but it's gated by a BIOS
variable (`WWEN`) — flipping the right Dell BIOS attribute may unlock
the kernel's existing reset-method machinery and turn this from
"reboot only" into "kernel reset works." Validation pending.

**Nothing the kernel can do clears this. Confirmed exhaustively
2026-05-01:**

| Method                                                      | Effect on SBL wedge                                                                                                                                                                                                      |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `systemctl restart MM`                                      | no                                                                                                                                                                                                                       |
| MHI driver unbind/rebind                                    | no                                                                                                                                                                                                                       |
| PCIe device remove + bus rescan                             | re-enumerates, firmware dead                                                                                                                                                                                             |
| PCIe Function Level Reset                                   | no                                                                                                                                                                                                                       |
| Parent bridge `reset_subordinate` (bus reset)               | no                                                                                                                                                                                                                       |
| Full kernel module reload (mhi\*, mhi-pci-generic)          | no                                                                                                                                                                                                                       |
| `setpci` Link Disable on parent bridge (CAP_EXP+10.W bit 4) | link goes down + back up; chip stays powered; firmware still dead                                                                                                                                                        |
| `systemctl suspend` (s2idle)                                | **no** — `cat /sys/power/mem_sleep` shows `[s2idle]` only; Lunar Lake doesn't expose S3, so suspend never powers down the PCIe slot. Verified 2026-05-01 (suspend ran cleanly for 12 s, modem stayed wedged after wake). |
| `systemctl hibernate` (S4)                                  | untested; would power-cycle the slot (full poweroff to RAM-on-disk) but requires swap configured + slower than reboot                                                                                                    |
| **`systemctl reboot`**                                      | **yes**                                                                                                                                                                                                                  |

This slot lacks all the platform hooks that would normally let a
running kernel power-cycle a PCIe device:

- Not in `/sys/bus/pci/slots/` (no hot-plug controller).
- Parent bridge (`00:1c.0`) only supports `pm` reset; no
  ASPM/power-resource handle for the slot.
- ACPI exposes `\_SB_.PC00.RP02.PXSX` for the modem but no
  `_PR0`/`_PR3` that the kernel's pcieport driver picks up for
  this address.
- `runtime_status: unsupported` for the device — kernel runtime PM
  is disabled.

Implication: this wedge is reboot-only on this hardware (or possibly
S3, with caveats). `modem.sh recover` runs the kernel-level escalation
ladder anyway, then prints suspend/reboot guidance.

### Implication for eSIM provisioning

The post-download wedge means eSIM provisioning requires one reboot:

1. **Before reboot**: wipe old profiles, download new profile (lpac works
   until the download command completes, then MBIM wedges)
2. **Reboot**: clears the wedge, modem reads the new profile
3. **After reboot**: send notification (`lpac notification process`),
   bring modem online (FoxFlss + MM restart)

There is no way to avoid this reboot with the current DW5934e firmware.

### Recommendation: use physical SIM (caveat 2026-04-30)

The eSIM provisioning path on this modem is unreliable (firmware wedges,
requires reboots, Google Fi backend doesn't receive installation
notification automatically). Use a physical SIM in slot 1 (micro-SIM,
battery compartment) for production use; eSIM is experimental only.

**Caveat**: switching SIM type does NOT solve the Google Fi QoS throttle
on this device. Both eSIM (2026-04-20) and physical SIM (2026-04-30)
hit the same ~7.5 KB/s TCP throughput cap with ICMP unaffected. The
throttle is gated by IMEI registration on the Fi account, not SIM type.
See "Physical SIM throughput" section below for the comparative numbers
and the activation step needed at `fi.google.com`.

### What does NOT reset the modem's UICC/SIM subsystem

All of these were tried and failed to clear the post-download wedge:

- **`systemctl restart ModemManager`** — re-probes but modem firmware state persists
- **PCIe device remove/rescan** (`echo 1 > .../remove && echo 1 > /sys/bus/pci/rescan`)
  — re-enumerates PCI device but doesn't reset modem firmware
- **PCIe Function Level Reset** (`echo 1 > /sys/bus/pci/devices/0000:71:00.0/reset`)
  — resets PCIe link but modem firmware persists state
- **`mbimcli --ms-set-uicc-reset`** — broken parameter parser in libmbim 1.32.0,
  rejects all values (`disabled`, `enabled`, `0`)
- **Double MHI rebind** — no better than single
- **Longer sleep times** (up to 30s between steps) — no effect

### Full modem restart without reboot — MHI rebind + FoxFlss + MM restart

After enabling a new eSIM profile via lpac, the modem is in `failed` /
`power state: low` (FCC locked, MM sees `esim-without-profiles`). To bring
it fully operational **without a system reboot**:

```bash
# After lpac enable completes:

# 1. MHI rebind to reset MBIM state
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/unbind
sleep 3
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/bind
sleep 5

# 2. Start MM — modem will be in failed/low state, but mbim-proxy runs
systemctl start ModemManager
sleep 10

# 3. FCC unlock via FoxFlss (needs mbim-proxy from step 2)
export PATH="/nix/store/wg8dv8x56avxinns71n4mqqnj90jybbg-foxflss-1.0.15/bin:$PATH"
# (or use the foxflss package path from nixos config)
FoxFlss

# 4. Restart MM — now sees the enabled profile, completes full init
sleep 5
systemctl restart ModemManager
# Modem should now be in connected/registered state
```

The key insight: MM must be running (for mbim-proxy) before FoxFlss can
unlock the modem. But MM's first probe fails with `esim-without-profiles`
because the modem hasn't read the eUICC yet. After FoxFlss powers up the
modem, restarting MM triggers a fresh probe that now sees the profile.

### Recommended workflow for eSIM profile changes

```bash
# 1. Stop MM
systemctl stop ModemManager

# 2. MHI rebind (clears MBIM state for lpac)
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/unbind
sleep 3
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/bind
sleep 8

# 3. lpac operations (can chain multiple without rebinding)
LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 \
  lpac profile list
LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 \
  lpac profile download -s <smdp> -m <matching-id>
LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 \
  lpac profile enable <iccid>

# 4. If enable triggers SelectFailed on subsequent commands, MHI rebind again

# 5. Bring modem operational (no reboot needed!)
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/unbind
sleep 3
echo 0000:71:00.0 > /sys/bus/pci/drivers/mhi-pci-generic/bind
sleep 5
systemctl start ModemManager && sleep 10
FoxFlss && sleep 5
systemctl restart ModemManager
```

Note: `lpac profile download` may time out during `es10b_load_bound_profile_package`
("Transaction timed out") but still partially install the profile. If the next
download attempt fails with `install_failed_due_to_iccid_already_exists_on_euicc`,
the profile IS installed — just enable it.

### What does NOT bring the modem out of failed/low state

- **MHI rebind alone** — resets MBIM UICC channels (lpac works) but modem
  stays in `power state: low` with FCC lock
- **`mmcli -m 0 --reset`** — accepted by MM ("successfully reseted") but
  MM re-probes and hits `esim-without-profiles` again
- **PCIe FLR** — no effect on modem power state
- **Running FoxFlss without MM** — fails with `Check mbim-proxy failed`
  (FoxFlss needs mbim-proxy which only runs when MM is active)

## eSIM ISD-R wedge after download (DW5934e firmware limitation)

`lpac profile download` wedges the modem's ISD-R (eSIM management)
MBIM UICC channel. After the download completes, ALL tools that need
the ISD-R fail with `SelectFailed` (`lpac` any command;
`mbimcli --ms-set-uicc-open-channel` to the ISD-R AID). Non-ISD-R MBIM
UICC queries still work (e.g. `mbimcli --ms-query-uicc-application-list`
shows USIM/ISIM).

Nothing the kernel can do clears this — MHI rebind, PCIe FLR, PCIe
remove/rescan, MM restart, `mmcli --reset` all confirmed ineffective.
Only a full reboot (or S3 suspend/resume) clears it. This is a firmware
bug in the DW5934e (SDX72).

Implication: provisioning a new eSIM profile is a two-phase op with a
reboot in between (download → reboot → enable + bring online + send GSMA
installation notifications). `modem.sh esim activate` handles the
non-reboot half; the reboot must be manual.

(MHI rebind DOES clear `SelectFailed` after profile disable/delete —
this specific wedge is download-only.)

## Physical SIM (2026-04-30) — current configuration

Google Fi data-only physical micro-SIM in slot 1, active. eUICC (slot 2)
empty. Switch is `mbimcli --ms-set-device-slot-mappings=0` (requires MM
stop + restart; safe with WiFi up). NM "Google Fi" profile is unbound
(`gsm.sim-id` empty), so it binds to whatever SIM is active.

### Watchdog + dmidecode fix (2026-04-30, evening)

Two related fixes shipped:

1. **`nix/packages/foxflss.nix`** — added `dmidecode` (and `gnused`,
   `gawk`, `gzip`, `coreutils` for completeness) to the `runtimePATH`
   wrapper. Without `dmidecode`, FoxFlss fails the platform check with
   `Current platform: do not support FccLock!` and exits 1. The script
   "worked" up to now only because we always invoked it from the user's
   shell which had dmidecode on PATH; the systemd-managed paths
   (fcc-unlock.d, the new watchdog) had a clean PATH and silently failed.
   This is why MM's `fcc-unlock.d/105b:e11d` had never been observed
   firing successfully in 5 days of journal — MM was invoking it; it was
   just exiting 1.

2. **`nix/nixos/hosts/rugged/foxconn-wwan.nix` + `foxflss_watchdog.py`** —
   a small Python systemd watchdog (`foxflss-watchdog.service`) listens
   to MM state changes via `mmcli -m any -w` and re-checks on a 5 s tick
   backstop. When the modem sits in `{enabling,disabled,failed}` with
   `power-state=low` for ≥ 12 s, it runs `FoxFlss` and restarts MM.
   Cooldown 120 s.

After the dmidecode fix landed, MM's `fcc-unlock.d` started firing
successfully on its own — observed at 22:57:40-22:57:45 PDT:

```
... state changed (disabled -> enabling)
... Cannot power-up: sotware radio switch is OFF      ← FCC lock detected
... power state updated: on                            ← unlocked, 5s later
... state changed (enabling -> enabled)
... 3GPP registration state changed (registering -> home)
... state changed (enabled -> registered)
```

So the watchdog's primary value going forward isn't to BE the unlock
path — it's to kick MM with `systemctl restart` if the modem ever wedges
again (e.g. if MM's "don't retry the unlock script after first failure"
contract bites us). MM's own reactive unlock now does the heavy lifting.

### SIM identity confirmed (2026-04-30)

Read from MBIM after slot switch + FCC unlock:

| Field              | Value                                                          |
| ------------------ | -------------------------------------------------------------- |
| ICCID              | `8901240270139815559` (also `8901240270139815559F` from MBIM)  |
| IMSI               | `310240273981555` (MCC/MNC 310/240 = T-Mobile direct)          |
| GID1               | `4276` (Google Fi marker)                                      |
| Operator           | T-Mobile (`310240`)                                            |
| Lock state         | `sim-pin2` enabled, `fixed-dialing` lock — does not block data |
| Initial bearer APN | `fast.t-mobile.com` (from carrier config)                      |

### Physical SIM throughput — same throttle as eSIM (2026-04-30, 23:12 PDT)

First end-to-end run with the physical SIM in slot 1, WiFi up, cellular
tests bound to wwan0. Run output:
`debug/rugged-mobile-net-diag/20260430-231220/`.

| Test                 | Result                                                            |
| -------------------- | ----------------------------------------------------------------- |
| Modem state          | `registered`, `home`, `attached`                                  |
| Carrier              | Google Fi (`310260`), LTE                                         |
| wwan0 source IP      | `100.81.36.193` (CGNAT, 100.64/10)                                |
| ICMP ping 8.8.8.8    | avg **68 ms** (healthy)                                           |
| HTTP 1MB throughput  | **7.3 KB/s** (`http: 200`)                                        |
| HTTP 10MB throughput | **7.4 KB/s** sustained                                            |
| HTTPS throughput     | **0 B/s, `http: 000`** — fails fast (~0.2-0.3s, no TLS handshake) |
| MTU enforced         | 1200 (DF-ping 1172 OK, 1272+ → "Message too long")                |

**Conclusion: the SIM swap did NOT defeat the QoS throttle.** Throughput
profile is identical to the prior eSIM measurement (7.5 KB/s on
2026-04-20), with the same characteristic ICMP-fast / TCP-throttled
asymmetry that signals carrier-side shaping rather than RF or
host-side problems.

This matches the prior root-cause analysis in this file: Google Fi
shapes devices not registered on the account by IMEI. Our modem
IMEI `356398950074094` was never on the Fi account (only the Pixel 6
appears in the app), and inserting a physical SIM doesn't auto-bind
the IMEI to the account. **Next step is to complete activation at
`fi.google.com`** (the data-only physical SIM kit ships requiring
explicit activation), which should link ICCID
`8901240270139815559` to the account and bind the modem's IMEI as
an authorized device.

The HTTPS-fails-fast pattern is a likely _separate_ secondary issue:
TLS ClientHello + cert chain typically needs >1200 B in the first
flight; with cellular MTU pinned at 1200 and ICMP "fragmentation
needed" suppressed by Google Fi's network (per prior probing),
PMTU discovery never fires and the TLS handshake silently fails.
Won't matter once throttle is gone (TCP MSS clamping handles
post-handshake bulk traffic), but worth verifying after activation.

## Modem behavior notes (reference)

- **RSSI-only signal reporting**: `mmcli --signal-get` returns only RSSI, no
  RSRP/RSRQ/SINR. `mbimcli --query-signal-state` confirms `RSRP/SNR info: 'n/a'`.
  SDX72 firmware limitation.
- **RF calibration resets mode lock**: `FoxFlss -f Check_RF_SSKU` resets allowed modes
  to `3g, 4g, 5g; preferred: 5g`. Must re-lock with
  `mmcli -m 0 --set-allowed-modes=4g` after RF cal.
- **5G NR unusable at primary location**: 0% signal quality, 93-1374ms latency spikes.
  LTE-only mode required. Mode lock survives reboots.
- **IPv6-only bearer rejection**: Cell sometimes rejects IPv4-only bearers with
  `Ipv6OnlyAllowed`. NM retries and gets IPv4 on next attempt.
- **AT port unresponsive**: `/dev/wwan0at0` does not respond to AT commands while MM
  is running. AT passthrough via `mmcli --command` requires `--debug` mode on MM
  startup. `mbimcli` via nix-shell works for MBIM queries.

## eSIM Provisioning from Linux

### Tools

- **`lpac`** (v2.3.0+): Open-source eUICC/LPA tool. Has native MBIM backend (added
  v2.2.0, Jan 2025). Foxconn T99W175 (same family as DW5934e) documented as
  MBIM backend = SUCCESS in lpac compatibility table.
- **`mbimcli`** (libmbim 1.32.0+): Has UICC Low Level Access commands:
  `--ms-set-uicc-open-channel`, `--ms-set-uicc-close-channel`,
  `--ms-set-uicc-apdu`, `--ms-query-uicc-atr`, `--ms-query-uicc-application-list`
- **`qmicli`**: Can tunnel QMI over MBIM (`--device-open-mbim`), has `--uim-get-card-status`
  but no high-level eSIM download commands.

### lpac Environment Variables

```bash
LPAC_APDU=mbim
LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0
LPAC_APDU_MBIM_UIM_SLOT=2          # 1-based; slot 2 = eSIM
LPAC_APDU_MBIM_USE_PROXY=1         # needed when ModemManager is running
```

### lpac Commands

```bash
# List profiles (MM stopped, no proxy needed):
sudo systemctl stop ModemManager
sudo LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 \
  lpac profile list

# Download new profile:
sudo LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 \
  lpac profile download \
  -s sm-v4-007-a-gtm.pr.go-esim.com \
  -m TY93BCW699ZG4WBL3Z8YDWAF05GDUO4D

sudo systemctl start ModemManager
```

### What Failed (2026-04-20)

- `lpac` with `LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0` — "no channel response
  received: Failure". Tried with MM stopped, with/without proxy. **Did NOT set
  `LPAC_APDU_MBIM_UIM_SLOT=2`** — this may have been the issue (eSIM is slot 2,
  default is slot 1 which is empty).
- `mbimcli --ms-open-channel=1` — wrong flag name. Correct flag is
  `--ms-set-uicc-open-channel` with parameters
  `application-id=A0000005591010FFFFFFFF8900000100,selectp2arg=4,channel-group=1`
  (AID is ISD-R applet for eSIM management).
- AT commands via `/dev/wwan0at0` — port unresponsive. Foxconn modems on WWAN
  subsystem do not expose traditional AT serial. AT backend documented as FAIL
  for T99W175 in lpac compatibility table.

### Fallback: Physical SIM

If eSIM provisioning fails from Linux: order free Google Fi "5G Data Only SIM Kit"
($0.00), insert micro-SIM in battery compartment slot (slot 1). No eSIM provisioning
needed.

## Previous Issue: 5G NR Unusable (2026-04-19)

Modem defaulted to 5G NR NSA with 0% signal quality and latency spikes up to 1374ms.
Manual `mmcli -m 0 --set-allowed-modes=4g` fixed latency to 47-67ms but throughput
remained poor (~6-7.5 KB/s). The mode lock survives reboots.

## Previous Issue: MBIM Session Exhaustion (2026-04-19)

Setting `autoconnect-retries = 0` (unlimited) in the NM profile caused NM to hammer
the modem with connection attempts on profile change. Each failed attempt leaked an
MBIM CID session. After ~272 attempts, all bearers failed with "Unknown error". Only
a full power cycle (reboot) clears the modem firmware's MBIM session table. PCIe
remove/rescan is insufficient.

## FoxFlss Tool Dependencies

FoxFlss shells out to several tools that must be on `PATH`. The systemd transient unit
has a minimal PATH, so all must be supplied explicitly via `lib.makeBinPath`:

- **`dmidecode`**: reads system SKU for platform detection (`0D67` = Dell Rugged).
- **`pgrep`** (procps): checks if `mbim-proxy` process is running. Without it, FoxFlss
  always reports `Check mbim-proxy failed` regardless of proxy state.
- **`tar`** (gnutar): extracts RF calibration data from
  `/opt/foxconn/data/DW5934e_RF.dat` (a `.tar.gz` archive) to
  `/var/tmp/DW5934e/RF_Files/`.
- **`gzip`**: needed by `tar` to decompress the `.dat` archive. Without `gzip`, `tar`
  exits with `Cannot exec: No such file or directory`.
- **`grep`** (gnugrep), **`sed`** (gnused), **`awk`** (gawk), **`coreutils`**: used to
  parse `dmidecode` output and perform platform ID string matching.

## MBIM Session Limit Pitfall (CRITICAL)

The DW5934e has a hard limit on simultaneous MBIM CID sessions. When FoxFlss fails
mid-run (e.g., during an NM reconnect), it leaks the MBIM CIDs it opened — they remain
in modem firmware until MM restarts and closes all sessions.

After multiple failed FoxFlss attempts, the modem hits its session limit. Subsequent
FoxFlss runs fail with:

```
ModuleTypeCheck: Retry to fail connect device: /dev/wwan0mbim0 for the 20 time
Current platform:0D67 do not support FccLock!
```

The `do not support FccLock` message is a **false negative** from connection failure, NOT
a real platform check result. `0D67` DOES support FccLock.

**Fix**: `systemctl restart ModemManager` — this closes all MBIM sessions and resets the
proxy. FoxFlss will work again after ~8s sleep.

## What We Tried That Didn't Work

### `foxflss-init` systemd service (`After=ModemManager.service`)

Discarded. Problems:

1. During `nixos-rebuild switch`, the NM profile change causes NM to reconnect cellular.
   MM is mid-reconnect when the service starts. FoxFlss leaks MBIM CIDs from each failed
   attempt.
2. `switch-to-configuration` restarts the service on each rebuild (even with
   `restartIfChanged=false`, failed units get restarted).
3. After several failed attempts, MBIM session limit hit → FoxFlss broken until MM
   restart.

### Wait-for-proxy poll loop in the service

`ss -xH | grep -q mbim` returns true immediately (MM always has proxy connections) but
FoxFlss still fails because the modem is mid-reconnect at a MBIM protocol level, not
just at the proxy socket level.

## NM Dispatcher Approach (Current)

Wired as `networking.networkmanager.dispatcherScripts` in `foxconn-wwan.nix`.

Script fires on `wwan0 up` and runs via `systemd-run --no-block --collect`:

```
FoxFlss && sleep 5 && FoxFlss -f Check_RF_SSKU
```

### MBIM warm-up requirement

`Check_RF_SSKU` fails at the MBIM connect step if run immediately after bare `FoxFlss`.
The vendor's Ubuntu setup avoids this because `fcc-unlock.d` (bare `FoxFlss`) runs before
`FoxFlss.service` (Check_RF_SSKU), flushing stale MBIM CIDs and leaving the device in a
clean state. On this NixOS system, `fcc-unlock.d` never fires (modem boots with
`power state: on`). The warm-up must be done explicitly: bare `FoxFlss` + `sleep 5` before
`Check_RF_SSKU`.

Confirmed in log: `Check_RF_SSKU: Failed to connect device` when called immediately
after FCC unlock (CrcCompatibilityCheck had just disconnected). After adding the
warm-up, `Check_RF_SSKU: PASS`.

### systemd-run flags

`--collect` is critical. Without it, the transient unit stays in `failed` state after a
failure. On the next `wwan0 up` event, `systemd-run` fails with:

```
Unit foxflss-rf-cal.service was already loaded
```

and FoxFlss never runs for that connection.

`--no-block`: NM waits for dispatcher scripts to finish. Backgrounding via `systemd-run`
prevents the connection from appearing active while FoxFlss is running.

### Passing the script to systemd-run

`systemd-run ... -- sh -c '...'` fails in the minimal service environment (`sh` not on
PATH). Use `pkgs.writeShellScript` to generate an absolute-path shell script, and pass it
directly as the `ExecStart` argument.

**Pending validation**: confirm dispatcher fires and FoxFlss succeeds on a clean cold boot
(no zombie MBIM sessions from previous failed attempts).

## `/opt/foxconn/data/` Setup

FoxFlss hardcodes `/opt/foxconn/data/{DW5932e,DW5934e}_RF.dat`. On NixOS (read-only
root), these are created as symlinks via `systemd-tmpfiles.rules` pointing into the nix
store (`foxflss` derivation's `share/foxflss/`).

## Vendor Reference

`foxconn-pc/fii_linux` on GitHub. Their `FoxFlss.service`:

- `ExecStart=FoxFlss -f Check_RF_SSKU` (only `Check_RF_SSKU`, no bare `FoxFlss`)
- `After=ModemManager.service`
- `Restart=on-abort` (not `on-failure` — only restarts on crash, not graceful exit 1)
- `StandardError=null`
- Bare `FoxFlss` (FCC unlock) is in separate `fcc-unlock.d` scripts, not the service.
