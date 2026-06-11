# DW5934e suspend/resume root-cause investigation

**Investigation started**: 2026-05-14.
**Subject**: Foxconn DW5934e (Qualcomm SDX72) on rugged (Lunar Lake tablet).
**Question**: Why does ordinary Linux suspend/resume wedge the modem into
a state where only reboot recovers? Windows on the same hardware doesn't
seem to suffer this.

---

## TL;DR mechanism (smoking gun)

1. System enters `s2idle` (Lunar Lake doesn't expose S3; slot stays powered).
2. Kernel's `mhi_pci_runtime_suspend` requests modem-side state M3. Some
   conditions cause this — or the corresponding M3→M0 resume — to fail:
   pending packets, dev_wake refs > 0, modem firmware falls to SYS_ERR
   during the half-sleep.
3. On resume, `mhi_pci_runtime_resume` silently queues `recovery_work` and
   returns success to the PM core (`pci_generic.c:1654-1663`). No
   user-visible suspend error.
4. `recovery_work` calls `mhi_sync_power_up` → `mhi_async_power_up`. If the
   modem is in SYS_ERR, the kernel issues `MHICTRL_RESET` (`pm.c:1166`),
   forcing the modem from AMSS into PBL.
5. `mhi_fw_load_handler` (`boot.c:491`) sees `ee == PBL` and tries to load
   the SBL blob from `mhi_cntrl->fw_image`. **For `mhi_foxconn_dw5934e_info`,
   `.fw` is `NULL`** (productized Foxconn modules normally boot from internal
   NAND; no host-side SBL is shipped). The driver aborts with `"No firmware
image defined or !sbl_size || !seg_len"`.
6. `recovery_work` then calls `pci_try_reset_function` (FLR), which retrains
   the PCIe link but leaves the modem's SoC stuck in PBL waiting for an SBL
   the host can never provide.
7. Result: `mhi0` exists in sysfs but no channels enumerate, `/dev/wwan0*`
   is empty, MM sees no modem. Only a reboot — which power-cycles the slot
   via the system halt — clears it.

The wedge is reachable from observable Linux state and the mechanism is
backed by code citations against torvalds/linux v6.19 (≡ current HEAD for
these functions). Windows almost certainly survives by _not_ corrupting
the modem during suspend in the first place, plus possibly having an SBL
blob to recover via the same code path if needed; we have no Windows-side
trace to confirm details.

---

## Runtime-PM autosuspend — the path the workaround missed (2026-06-08)

**A wedge fired from kernel runtime PM, not system suspend.** All of the
above (and the deployed `--test-low-power-suspend-resume` workaround) only
covers _system_ sleep/wake — MM's sleep-signal handler. The MHI pci driver
_also_ enables **runtime PM autosuspend** with a 2s idle delay
(`power/autosuspend_delay_ms = 2000`, driver default). While on WiFi the
modem sits idle and gets autosuspended to M3 every few seconds — the same
broken Foxconn M3 firmware path, reached without MM's low-power dance.

Live trace from rugged, boot 2026-06-04:

```
Jun 05 02:30:48 kernel: pcieport 0000:00:1c.0: Data Link Layer Link Active not set in 100 msec
Jun 05 02:30:48 kernel: mhi mhi0: Resuming from non M3 state (SYS ERROR)
Jun 05 02:30:48 kernel: mhi-pci-generic 0000:71:00.0: failed to resume device: -22
Jun 05 02:30:48 kernel: device recovery started → reset → reset failed → Recovery failed: -25
```

**Proof it was runtime PM, not system sleep:** no `PM: suspend entry`
anywhere near 02:30:48 — the nearest system suspend was 02:50:39 (20 min
later). So `mhi_pci_runtime_resume` ran, found the firmware in SYS_ERR
(left there by an earlier idle autosuspend M3), and the no-SBL
`recovery_work` → FLR wedged it. The resume was provoked by ordinary
traffic poking wwan0 (nebula handshakes / kubelet / discord all retrying
at that timestamp). `power/runtime_suspended_time` had accumulated ~4.1h,
confirming the modem had been autosuspending all along; `power/control`
reads `on` only because `recovery_work` calls `pm_runtime_forbid()`.

**Fix applied:** udev rule pins `power/control=on` for `105b:e11d` in
`foxconn-wwan.nix`, disabling runtime autosuspend. The modem stays at M0
during normal operation; M3 only happens during real system suspend, where
the MM flag already handles it. This addresses the _cause_ (don't enter the
broken M3 path while idle), complementary to the kernel-quirk P1 below
(which would only defang the _symptom_ — the bad FLR — after a crash).
Re-enumeration re-fires the `add` uevent so the rule re-applies each time.
Verify post-reboot: `cat /sys/bus/pci/devices/0000:71:00.0/power/control`
should read `on` with the modem healthy (channels enumerated).

**Non-reboot recovery DOES work (correction to earlier claim).** On
2026-06-08 `modem.sh recover` cleared this exact wedge without a reboot:
the kernel's in-place `recovery_work` FLR had already failed `-25`, but a
full PCI **remove + bus rescan** (`echo 1 > .../remove; echo 1 >
/sys/bus/pci/rescan`) tore the driver down completely and re-probed from
scratch — `mhi mhi0: Power on setup success`, all channels enumerated, MM
auto-unlocked via its wired `fcc-unlock.d`, modem reached `connected` on
5gnr. So the slot does **not** have to physically lose power; a clean
driver-level teardown+reprobe is enough (FLR alone isn't, because it
resets the function while the driver tries to recover in-place against a
SoC still in PBL). The earlier "only reboot recovers" claim was wrong —
`recover` just _reported_ failure because of two script bugs (bare
`FoxFlss` not on PATH under sudo + a premature `die()` before the modem
finished enumerating), both fixed in `modem.sh` on 2026-06-08.

## Current status (2026-05-14, end of session 1)

**Applied (workaround path)**:

- ✅ ModemManager runs with `--test-low-power-suspend-resume` (NixOS
  drop-in in `foxconn-wwan.nix`). Tells MM to issue a firmware-level
  `MM_MODEM_POWER_STATE_LOW` (QMI DMS Set Operating Mode = LOW*POWER)
  \_before* the kernel's MHI suspend path runs. Modem leaves AMSS
  cleanly, kernel's `mhi_pci_suspend` takes the early-exit path
  (`pci_generic.c:1598-1600`), broken M3→PBL chain is avoided by
  construction. **Untested under actual suspend/resume — needs the
  experiment matrix below.**
- ✅ `foxflss-rf-cal-run` `sleep: command not found` bug fixed
  (absolute path to `sleep`). RF cal had been silently skipping on
  every boot.
- ✅ `foxflss-watchdog` exponential backoff: `mmcli -w` respawn delay
  doubles (2s → 60s cap) when `mmcli -w` exits within 5s, resets after
  a healthy run. Eliminates journal spam when modem is absent.

**Staged, awaiting reboot (real-fix path)**:

- ✅ `WwanAutoSense` BIOS attribute flipped `Disabled → Enabled` via
  dell-wmi-sysman (no admin password needed). **Hypothesis**: this is
  the BIOS variable that populates the DSDT's `WWEN` byte, which gates
  exposure of the WWAN slot's ACPI `_RST` / `_PRR` / power-cycle
  methods. If correct, post-reboot `reset_method` will list `acpi flr
bus` (gain of `acpi`) and `recovery_work`'s `pci_try_reset_function`
  will trigger the platform's actual slot power-cycle via FHRF+SHRF.

**Capture tooling shipped**:

- `debug/rugged/modem.sh dump` — snapshot everything to
  `debug/rugged/hw/suspend_research/snapshots/<TS>/`: full dmesg,
  journals (kernel / MM / NM / watchdog / sleep-events) since boot,
  sysfs power state, lspci capabilities, AER counters, PCI
  `reset_method`, ACPI wakeup table, Dell BIOS WWAN/sleep
  attributes, DSDT.aml. Works on a wedged modem. Output chmod'd to
  user-readable. Run **before** rebooting from a wedge to preserve
  the failure trace.
- `debug/rugged/modem.sh status` — fast read-only triage; surfaces
  MHI channel enumeration state (the wedge signature), watchdog
  hot-spin flag, last suspend/resume marker, and filtered kernel +
  MM events anchored to that marker.
- `debug/rugged/hw/suspend_research/flip_wwan_autosense.sh` — toggle
  the candidate BIOS gate from Linux. Idempotent; `TARGET=Disabled`
  to revert.

**Honest summary**: the workaround is in place. The real fix is staged
but unproven (WwanAutoSense → WWEN connection is a hypothesis, not
established). Next reboot tells us if the hypothesis is right. After
that, we still need to run a controlled suspend/resume experiment
matrix to validate whichever path works.

---

## Post-reboot findings (2026-05-19)

**WwanAutoSense=Enabled hypothesis: WRONG.**

After rebooting with `WwanAutoSense` flipped to `Enabled`,
`/sys/bus/pci/devices/0000:71:00.0/reset_method` is still `flr bus` —
no `acpi` method. The BIOS variable does not correspond to the DSDT
`WWEN` byte, or at least does not cause the kernel to discover the
`_RST` method. The WWEN gate remains at 0. `WwanAutoSense` probably
controls something else (perhaps OS power-management handoff or radio
mode selection).

**`--test-low-power-suspend-resume` workaround: confirmed working.**

At least 2 organic suspend/resume cycles observed without wedge:

- 2026-05-18 21:30 suspend → 2026-05-19 01:59 resume → modem1 connected
- 2026-05-19 02:14 suspend → 2026-05-19 12:48 resume → modem2 connected

The modem re-enumerates on each resume (modem index increments per boot
under the current MM PID), which is expected behavior — MM puts modem in
LOW_POWER before the kernel's MHI suspend path, modem comes back from
NAND-boot on resume. No MHI/SBL wedge observed.

## Remaining next steps

**2. Run the suspend experiment matrix**

Each experiment: `sudo modem.sh dump` → `systemctl suspend` (or close
lid) → resume → `sudo modem.sh dump`. Confirm whether `modem.sh status`
shows healthy MHI channels post-resume; if `mhi0 channels: NONE`,
wedge fired — STOP and analyze.

| Run | Setup                           | What it tests                                       |
| --- | ------------------------------- | --------------------------------------------------- |
| E1  | idle modem, no traffic          | baseline — does suspend wedge at all?               |
| E2  | `ping -I wwan0 8.8.8.8` running | in-flight packets → `pending_pkts > 0` → EBUSY path |
| E3  | 5× idle suspends, ~60s apart    | probabilistic vs deterministic wedge                |

E1 vs E2 separates H1 (host-quiesce bug) from H2 (modem firmware fault).
E3 separates deterministic from probabilistic triggers.

**3. Outcome interpretation**

- All three pass → wedge solved by P0 (MM flag) and/or BIOS-side P4
  (WwanAutoSense → WWEN). Document, close the investigation.
- E1 fails → the wedge is independent of host quiesce; P1 (kernel quirk
  patch) is needed, or the WWEN fix is doing the heavy lifting.
- E1 passes, E2 fails → pending packets are the trigger. Workaround:
  also force `nmcli connection down "Google Fi"` via a systemd pre-sleep
  hook before suspend.
- Some pass, some fail → probabilistic. Useful next instrumentation:
  enable MHI ftrace events (see "Diagnostics still to add" below) for
  per-cycle state transitions.

**4. If all of P0+P4 still aren't enough**, drop to P1 (kernel quirk
patch); see "Avenues" below.

---

## Code mechanism (evidence, with line references)

Source pulled to `suspend_research/upstream_*.c` and full
`~/code/linux` clone at v6.19 (= `mhi_pci_*` code is identical to
current torvalds/linux master, verified by diff).

### `mhi_foxconn_dw5934e_info` (`pci_generic.c` ~line 644)

```c
.name = "foxconn-dw5934e",
.edl = "qcom/sdx72m/foxconn/edl.mbn",
.edl_trigger = true,
.config = &modem_foxconn_sdx72_config,
// NOTE: .fw NOT set, .fbc_download NOT set.
```

For comparison, Qualcomm _reference_ designs DO set `.fw`:

- `qcom-sdx55m`: `.fw = "qcom/sdx55m/sbl1.mbn"`
- `qcom-sdx65m`: `.fw = "qcom/sdx65m/xbl.elf"`
- `qcom-sdx75m`: `.fw = "qcom/sdx75m/xbl.elf"`

All _productized_ Foxconn variants AND `telit-fn990`/`telit-fe990a`
(sdx65) ship without `.fw`. Pattern: vendor-productized modules
expected to boot from internal NAND only.

### Suspend path (`pci_generic.c::mhi_pci_runtime_suspend` ~1583)

```c
if (!test_bit(MHI_PCI_DEV_STARTED, ...) || mhi_cntrl->ee != MHI_EE_AMSS)
    goto pci_suspend;            /* skip MHI M3 if not in mission mode */

err = mhi_pm_suspend(mhi_cntrl); /* request M3 */
if (err) {
    dev_err(... "failed to suspend device: %d");
    return -EBUSY;
}
pci_disable_device(pdev);
pci_wake_from_d3(pdev, true);
```

`mhi_pm_suspend` returns `-EBUSY` when `dev_wake` or `pending_pkts > 0`
(`pm.c:883, 911`). It returns `-EIO` when the M3 wait times out.

### Resume path (`pci_generic.c::mhi_pci_runtime_resume` ~1617)

```c
pci_enable_device(pdev); pci_set_master(pdev);
if (!test_bit(MHI_PCI_DEV_STARTED, ...) || mhi_cntrl->ee != MHI_EE_AMSS)
    return 0;

err = mhi_pm_resume(mhi_cntrl);  /* M3→M0 */
if (err) {
    dev_err(... "failed to resume device: %d");
    goto err_recovery;           /* ← key */
}
...
err_recovery:
    queue_work(system_long_wq, &mhi_pdev->recovery_work);
    return 0;                     /* silently returns success */
```

### Recovery work (`pci_generic.c::mhi_pci_recovery_work` ~1219)

```c
dev_warn(... "device recovery started");
pm_runtime_forbid(...);          /* explains 'forbidden' state in wedged dump */
mhi_power_down(); mhi_unprepare_after_power_down();
pci_set_power_state(pdev, PCI_D0); pci_load_saved_state; pci_restore_state;

if (!mhi_pci_is_alive(mhi_cntrl))   /* PCI vendor-id != 0xFFFF check */
    goto err_try_reset;
err = mhi_prepare_for_power_up(...);
err = mhi_sync_power_up(...);     /* ← fw_load_handler hits "no fw" here */
if (err) goto err_unprepare;
return;

err_unprepare: mhi_unprepare_after_power_down(...);
err_try_reset:
    err = pci_try_reset_function(pdev);   /* FLR */
    if (err) dev_err(... "Recovery failed: %d");
```

### fw_load_handler (`boot.c:491`, gate at `:514`)

```c
if (!MHI_FW_LOAD_CAPABLE(mhi_cntrl->ee))      /* AMSS → skip, PBL/EDL → load fw */
    goto fw_load_ready_state;
fw_name = (ee == EDL) ? edl_image : fw_image;
if (!fw_name || ...) {
    dev_err(... "No firmware image defined or !sbl_size || !seg_len");
    goto error_fw_load;
}
```

`MHI_FW_LOAD_CAPABLE(ee) = (ee == PBL || ee == EDL)` (`internal.h:78`).
So the error message **fires only when modem is in PBL**.

---

## The WWEN gate finding (settles H3 — platform DOES have a power-cycle, BIOS gates it)

DSDT decompile of the rugged tablet (snapshot
`20260514-144702/DSDT.dsl`) reveals a fully-wired WWAN slot power
management infrastructure on `\_SB.PC00.RP02.PXSX`, gated by a BIOS
variable `WWEN`:

```
Scope (_SB.PC00.RP02.PXSX)
{
    If (((WWEN != Zero) && (WWRP == SLOT)))     // outer gate
    {
        Method (FHRF, 1, ...) {        // Full Hardware Reset Function
            DL23()                     //   1. drop PCIe link to L2.3
            SGOV(PRST, ...)            //   2. assert PERST# GPIO
            SGOV(WBRS, ...)            //   3. assert WWAN reset GPIO
            SPCO(WCLK, 0)              //   4. disable WWAN reference clock
            SGOV(WFCP, ...)            //   5. toggle WWAN POWER GATE (slot VCC)
        }
        Method (SHRF, 0, ...) {        // Soft Hardware Reset — reverse sequence
            SPCO(WCLK, 1); SGOV(WFCP, ...); SGOV(WBRS, ...);
            SGOV(PRST, ...); L23D()
        }
        Method (_RST, 0, Serialized) {
            If ((WWEN == 0x02)) {      // inner gate: WWEN must be 0x02
                FHRF(Zero); SHRF()     // full slot power-cycle
            }
        }
        PowerResource (MRST, ...) {
            Method (_RST, ...) {       // PLDR variant
                If ((WWEN == 0x02)) { FHRF(One); SHRF() }
            }
        }
        Method (_PRR, ...) { Return (Package(One){MRST}) }   // ACPI 6.x
        Method (_DSM, 4, ...) { ... }  // MS WWAN UUID bad01b75-22a8-4f48-8792-bdde9467747d
    }
}
```

**Plain English**: the platform firmware has a GPIO power gate
(`WFCP`), a clock gate (`WCLK`), PERST# + WWAN reset GPIO controls,
and the standard ACPI methods to invoke them (`_RST`, `_PRR`, Microsoft
WWAN power `_DSM`). But the **entire block is conditionally compiled
into the ACPI namespace** based on `WWEN != 0` (existence) and
`WWEN == 0x02` (useful path). `WWEN` is a Field inside an OperationRegion
populated by BIOS at boot.

**Linux uses this if exposed**: kernel's reset-method priority is
`device_specific, acpi, flr, af_flr, pm, bus, cxl_bus`
(`pci.c:5011-5018`). The `acpi` reset method calls `_RST`
(`pci-acpi.c:971-985`) via `acpi_evaluate_object`. So
`pci_try_reset_function` in `recovery_work` would invoke this BEFORE
trying FLR. Our wedge would be solved by the platform actually
power-cycling the slot: modem boots fresh from NAND, no PBL trap.

**Currently `WWEN == 0`**, proven by:

```
/sys/bus/pci/devices/0000:71:00.0/reset_method = flr bus
```

No `acpi` method discovered. `acpi_has_method(handle, "_RST")` returns
false because `_RST` wasn't compiled into the namespace.

### WWAN_AutoSense hypothesis (staged for next reboot)

dell-wmi-sysman exposes 187 firmware attributes; the WWAN-related ones
captured in the dump are:

| Attribute         | Current       | Possible                  | Display name           |
| ----------------- | ------------- | ------------------------- | ---------------------- |
| WirelessWwan      | Enabled       | Disabled/Enabled          | WWAN/GPS               |
| WwanAntSwitch     | SystemAntenna | DockAntenna/SystemAntenna | WWAN Antenna           |
| **WwanAutoSense** | **Disabled**  | Disabled/Enabled          | **Control WWAN radio** |
| WWanBusMode       | PcieMode      | PcieMode/UsbMode          | WWAN Bus Mode          |
| DisWWANRadio      | Enabled       | Disabled/Enabled          | Disable WWAN radio     |

`WwanAutoSense` is the only WWAN-related setting currently **Disabled**.
Display name suggests it controls runtime radio management (i.e.,
platform-driven power state coordination), which is plausibly what
flips `WWEN`. **No direct evidence** linking `WwanAutoSense` to `WWEN`;
this is a name-and-elimination guess. Already flipped to `Enabled` via
`flip_wwan_autosense.sh`; reboot pending.

If it turns out NOT to be the gate, alternatives:

- F2 BIOS Setup, look in any "Service" / "Maintenance" / "Power" /
  "POST Behavior" tab for less-obvious WWAN-power options.
- Search Dell's BIOS XML schema for this specific Latitude rugged
  tablet for `WWEN` or "WWAN Power Reset" under a different attribute
  name not surfaced by dell-wmi-sysman.

---

## Why Windows likely survives (honest assessment)

**Known** (from observed Linux/Foxconn ecosystem evidence):

- Foxconn release notes (v1.0.5, v1.0.9, v1.0.11) explicitly call out
  Linux suspend bugs and ship a workaround (`mm-suspend-resume-options.conf`
  installing `--test-low-power-suspend-resume`). If the modem worked
  cleanly with stock Linux MM, they wouldn't have. So **Linux MM's
  default suspend handling is documented to be insufficient for this
  modem**.
- Stock Linux MM sleep handler is just
  `mm_base_manager_cleanup(CLEANUP_REMOVE)` — no power-state change.
- The Foxconn-recommended flag makes MM issue
  `MM_MODEM_POWER_STATE_LOW` (QMI DMS Set Operating Mode = LOW_POWER)
  before suspend, which is a graceful firmware-managed low-power
  transition.

**Speculated** (no Windows-side evidence captured):

- That Windows uses an analogous "set radio low-power before host
  sleep" sequence at the driver level. Plausible — the QMI/MBIM ops
  are standard — but unverified.
- That Windows ships the SBL blob in its driver package, so even when
  the modem does fall into PBL, host-side recovery succeeds. Plausible,
  again unverified.

**To settle the Windows question** would require a USBPcap of a Windows
sleep/wake cycle on the same device. Not blocking — the Linux-side
mechanism trace and the WWEN finding are self-contained and dispositive.

---

## Avenues to attack (ranked by tractability and evidence)

**P0 ✅ APPLIED — Foxconn-recommended MM flag**: `--test-low-power-suspend-resume`.
See `nix/nixos/hosts/rugged/foxconn-wwan.nix`. On the `--test-` prefix:
overloaded MM naming convention — these aren't unstable test stubs,
they're "MM upstream hasn't picked a per-modem default yet, integrator
chooses." Wired into mature `mm_base_manager_cleanup` paths.

**P4 ✅ STAGED — BIOS WwanAutoSense flip** (real platform-level fix).
Awaits reboot to validate WWEN gating hypothesis.

**P1 — Kernel quirk patch** (if P0+P4 both fail). Add
`.no_recovery_reset = true` to `mhi_foxconn_dw5934e_info` (and other
productized Foxconn variants); short-circuit the
`pci_try_reset_function` call in `mhi_pci_recovery_work` when set.

- Effort: ~half-day (out-of-tree patch + NixOS plumbing + reboot/test).
- Send upstream to `loic.poulain@linaro.org` / `mhi@lists.linux.dev`.
  The current behavior is wrong for the productized-vendor class.

**P2 — Obtain SBL firmware blob for SDX72**. Locate
`qcom/sdx72m/foxconn/sbl1.mbn` (or `xbl.elf`) from a Windows driver,
Foxconn engineering channel, or extracted from device. Install in
`/lib/firmware/qcom/sdx72m/foxconn/`. Patch
`mhi_foxconn_dw5934e_info.fw` to point at it. Then the existing
recovery path would actually succeed: FLR → PBL → host loads SBL → AMSS.

- Risk: legal-ish (binary blob redistribution).

**P3 — Hibernate (S4) as heavy workaround**. Verify hibernate clears
the wedge (the freeze path does a clean MHI teardown; S4 powers off
the slot via system halt). Requires swap configured.

**P5 — Force-detach driver before suspend (Plan B if P0 fails)**.
Unbind `mhi-pci-generic` from `0000:71:00.0` via `systemd-sleep`
pre-hook, re-bind in post-resume hook. Skips kernel's broken suspend
path by removing the device from kernel view. Risk: rebind may still
fail if modem state changed during sleep without host's knowledge.

---

## Diagnostics still to add

- **MHI ftrace events**: best per-cycle visibility into M0/M1/M2/M3
  transitions. Wire into `modem.sh dump` as `--trace` flag that does
  `echo 1 > /sys/kernel/tracing/events/mhi_host/enable` before, then
  `cat /sys/kernel/tracing/trace > $out/mhi_trace.txt` after.
- **MM debug logging**: `mmcli --set-logging=DEBUG` (or restart MM
  with `--debug`) before suspend. Surfaces the `mm_base_manager_cleanup`
  flow in MM journal so we see exactly what MM did before s2idle.
- **DSDT WWEN read at runtime**: requires `acpi_call` kernel module or
  custom-method debugfs (CONFIG_ACPI_CUSTOM_METHOD). Would let us
  directly read the current `WWEN` value to verify what BIOS variables
  populate it.

---

## Open hypotheses

**H1 — host fails to checkpoint MHI cleanly before suspend.**
Strongest test: E2 (loaded suspend) vs E1 (idle suspend). If only
loaded suspends wedge, supports H1. With P0 (MM low-power flag)
applied, the host always quiesces correctly — H1 isn't reachable.

**H2 — modem firmware itself crashes during sleep regardless of host
behavior**, only a real power cycle recovers. Best test: E3 (repeat
idle). If even 5 quiet suspends produce occasional wedges, the trigger
isn't fully host-controllable. P4 (BIOS WWEN) addresses this — gives
us a real reset path even on the H2 failure mode.

**H3 — platform-side ACPI hooks missing.** **SETTLED**: hooks exist
in DSDT (FHRF/SHRF/`_RST`/`_PRR`) but gated by `WWEN`. P4 unlocks them.

**H4 — combination of H1 + H3.** Most likely. P0 addresses H1, P4
addresses H3. If both are in place and wedge still happens, it's
genuinely H2 and we move to P1/P2.

---

## Honest claims-vs-evidence ledger

Confidence: 🟢 verified from code/data · 🟡 plausible/partly-evidenced · 🔴 my speculation.

| #   | Claim                                                                                                                                | Confidence | Evidence                                                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------- |
| 1   | Wedge dmesg signature is `No firmware image defined or !sbl_size \|\| !seg_len` → `failed to power up MHI controller` → `error -110` | 🟢         | Live dmesg from `modem.sh recover` 2026-05-14                                                           |
| 2   | Wedge means modem-side SoC in PBL waiting for host SBL                                                                               | 🟢         | `boot.c:514` gate `MHI_FW_LOAD_CAPABLE(ee)` proves message only fires when `ee == PBL` (internal.h:78)  |
| 3   | DW5934e ships without `.fw` because it normally NAND-boots                                                                           | 🟢         | `pci_generic.c::mhi_foxconn_dw5934e_info` lines verified                                                |
| 4   | Lunar Lake: only `s2idle`, no S3; slot stays powered through suspend                                                                 | 🟢         | `/sys/power/mem_sleep = [s2idle]`                                                                       |
| 5   | Modem PCI advertises PME from D3cold                                                                                                 | 🟢         | `lspci` Capabilities: `PME(D0+,D1-,D2-,D3hot+,D3cold+)`                                                 |
| 6   | Cold boot from NAND is clean — wedge is suspend-induced only                                                                         | 🟢         | post-reboot dmesg: `Power on setup success` → channel enum                                              |
| 7   | Kernel `recovery_work` calls `pci_try_reset_function` (FLR) unconditionally for these devices                                        | 🟢         | `pci_generic.c::mhi_pci_recovery_work` line 1267                                                        |
| 8   | `recovery_work` returns success to PM core even when reset fails                                                                     | 🟢         | `pci_generic.c:1663` (returns 0 after queueing)                                                         |
| 9   | Foxconn ships `--test-low-power-suspend-resume` as their workaround                                                                  | 🟢         | `foxconn-pc/fii_linux Application/FoxFlss/data/{mm-options.sh,mm-suspend-resume-options.conf}`          |
| 10  | `--test-low-power-suspend-resume` issues firmware-level LOW_POWER via QMI DMS                                                        | 🟢         | MM source `main.c:65-71` → `mm-base-manager.c:1026-1036` → `mm-broadband-modem-qmi.c:2592`              |
| 11  | DSDT contains full WWAN slot power-cycle methods, gated by `WWEN`                                                                    | 🟢         | DSDT decompile, captured at `snapshots/20260514-144702/DSDT.dsl`                                        |
| 12  | `WWEN == 0` currently                                                                                                                | 🟢         | `/sys/.../reset_method = flr bus`; no `acpi` means `_RST` isn't in namespace                            |
| 13  | `WwanAutoSense=Enabled` will set `WWEN >= 1`                                                                                         | 🔴         | Pure guess from display name + process of elimination. **Next reboot validates.**                       |
| 14  | Windows uses an analogous firmware low-power sequence                                                                                | 🔴         | No Windows trace captured. Plausible by analogy to MBIM/QMI standards.                                  |
| 15  | Upstream kernel has no fix for this wedge                                                                                            | 🟢         | Diff v6.19 vs torvalds master: zero changes to `mhi_pci_recovery_work`, `mhi_pci_runtime_*`, `mhi_pm_*` |

---

## Notes on the dmesg ring buffer (gotcha for future investigations)

The kernel ring buffer gets overrun within ~5 minutes by
`hid-sensor-hub` debug-level events (hundreds per second). After that,
`dmesg` no longer contains early-boot MHI probe trace. **Use
`journalctl -k -b 0` for any analysis past boot+5min.** `modem.sh dump`
captures both; researchers should prefer the journal file. Disabling
hid-sensor-hub debug spam is a separate side-todo.

## Side issues discovered (not blocking suspend work)

- **`foxflss-rf-cal-run` sleep bug**: FIXED. `pkgs.writeShellScript`
  doesn't put coreutils on PATH; `sleep` was failing silently and the
  `&&`-chain skipped RF cal on every boot. Fixed by using absolute
  path `${pkgs.coreutils}/bin/sleep` in `foxconn-wwan.nix`.
- **`foxflss-watchdog` hot-spin**: FIXED. When modem is absent
  (wedged), `mmcli -w` exits immediately; the watchdog was respawning
  it every 2s, flooding the journal. Now has exponential backoff
  (2s → 60s cap), resets after a healthy run.
- **`hid-sensor-hub` debug spam**: OPEN. Floods kernel ring buffer.
  Likely needs a sysfs / kconfig knob to drop the level from DEBUG.
- **`nmcli connection show "Google Fi"` slow** in dump: OPEN. Should
  be sub-second; sometimes takes multiple seconds. Timing instrumentation
  added to `modem.sh dump` to localize the slow call on next run.

---

## Files / locations

- This document: `debug/rugged/hw/modem_suspend_research.md`
- Pointer in `debug/rugged/hw/modem.md` "Where to find what" table.
- Cross-reference in `debug/rugged/hw/foxflss_wwan.md` §SBL-stage
  firmware wedge.
- Capture script: `debug/rugged/modem.sh` (`dump`, `status`, `recover`).
- BIOS toggle script: `debug/rugged/hw/suspend_research/flip_wwan_autosense.sh`.
- Snapshots: `debug/rugged/hw/suspend_research/snapshots/<TS>/`.
- Captured upstream sources: `debug/rugged/hw/suspend_research/upstream_*.c`.
- Local clones (read-only for reference):
  - `~/code/linux` — torvalds/linux v6.19 (kernel sources).
  - `~/code/modemmanager` — MM 1.24.2 (sleep handler verified).
  - `~/code/fii_linux` — Foxconn FoxFlss repo (their suspend workaround).
- NixOS config: `nix/nixos/hosts/rugged/foxconn-wwan.nix` carries the
  MM `--test-low-power-suspend-resume` drop-in + `foxflss_watchdog.py`
  backoff + sleep-bug fix.
