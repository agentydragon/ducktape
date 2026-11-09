# MHI `-EBUSY` suspend failure captured

Reviewed from existing journals on rugged on 2026-11-08 at approximately
19:45 PST. No configuration changed. Kernel `7.2.9`, boot started 2026-10-27.
Tracking issue: [#11789](https://github.com/agentydragon/ducktape/issues/11789).

## Captured failure

`mhi-suspend-trace.service` recorded the inner return for both failed
attempts at 19:11:27:

```text
mhi_pm_suspend_ret=-16 dev_wake=0 pending_pkts=4
```

The `-EBUSY` therefore comes from queued packets (`pending_pkts`), not a held
wake reference (`dev_wake=0`). The kernel logged
`mhi-pci-generic 0000:71:00.0: failed to suspend device: -16` and
`PM: Some devices failed to suspend, or early wake event detected`; the retry
failed identically and the machine stayed awake. The next suspend at 19:16:46
succeeded (`ret=0`). Between Oct 27 and this event the probe recorded 22 clean
suspends (all `ret=0 dev_wake=0 pending_pkts=0`).

## Sequence

| Time (PST) | Event                                                                              |
| ---------- | ---------------------------------------------------------------------------------- |
| 19:11:02   | ModemManager: `connected -> disabled`, `power state updated: low`, drops inhibitor |
| 19:11:03   | First suspend; probe `ret=0`; resume at 19:11:14                                   |
| 19:11:14   | Six MBIM `Transaction timed out`; ports reprobed from 19:11:16                     |
| 19:11:26   | "about to suspend" and "ready to sleep; dropping inhibitor" in the same second     |
| 19:11:27   | Kernel suspend fails twice: `ret=-16 pending_pkts=4`                               |
| 19:11:35   | ModemManager creates `modem20`; `unknown -> disabled` at 19:11:47                  |

At 19:11:26 ModemManager logged no `power state updated: low`, and the new
modem object did not exist yet: the suspend came 12 s after resume, mid-reprobe.

## Interpretation

The Oct 23 failures (21–22 s after resume, no `low` message) and this one
match: suspend during the post-resume reprobe window, with ModemManager's
inhibitor dropped before the modem reached low power. The Oct 26 rapid
re-suspend (33 s after resume) succeeded because `power state updated: low`
preceded the inhibitor drop. Three failing-vs-passing sequences support, but do
not prove, "ModemManager releases the sleep inhibitor before the modem is in
low power." What is now established is the failing inner condition:
`pending_pkts > 0` at `mhi_pm_suspend()` return.

Open:

- What queues the four packets while the modem is half-reprobed (MBIM control
  traffic from the timed-out transactions is the obvious candidate).
- Why ModemManager drops its inhibitor without having entered low power.
- The recurring post-resume MBIM timeouts
  ([Oct 27 note](../20261027T084500_0700/findings.md)); present on both resumes
  today.

Candidate fixes need this evidence first; none is applied.

## State check and upstream review (2026-11-08)

Read-only; nothing changed. The modem is in use, so all flag experiments wait.

### BIOS

`WwanAutoSense=Enabled` is still set. It was flipped from `Disabled` on
2026-05-14 to expose the ACPI `_RST` slot reset, which did not happen
(`reset_method` is `flr bus`). It went in the same day as the ModemManager
flag, so every "flag works" observation and every suspend failure since ran
with it `Enabled`; its effect on sleep is unknown. The other WWAN attributes
match the May capture (`WirelessWwan=Enabled`, `WwanAntSwitch=SystemAntenna`,
`WWanBusMode=PcieMode`, `DisWWANRadio=Enabled`). Reading them needs root
(`/sys/class/firmware-attributes/dell-wmi-sysman/attributes/*/current_value`).

### Upstream

- Mainline `mhi_foxconn_dw5934e_info` is unchanged: no `.fw`, no `.no_m3`. No
  change touches the `mhi_pm_suspend` `-EBUSY` / `pending_pkts` path.
- July 2026 added a core `mhi_cntrl->no_m3` flag (`753aa72545`: the host
  enters M3 without the device M3 handshake) and its `pci_generic` wiring
  (`0936a8780e`, runtime-PM reference fix `95601e9948`). Only QDU100 sets it.
  Whether running 7.2.9 contains these is unchecked.
- FoxFlss v1.0.17 (pinned here) is the latest. Its binary writes
  `autosuspend_delay_ms` itself; our `power/control=on` pin overrides runtime
  autosuspend regardless.
- Foxconn's release history shows the same symptom class: v1.0.5 "fix failed
  to enter suspend while browsing web pages" added
  `--test-quick-suspend-resume`, v1.0.9 used it for DW5934e, v1.0.11 switched
  to `--test-low-power-suspend-resume` for ModemManager 1.24.2+. The reason
  for the switch is not documented. ModemManager 1.24.2 on rugged supports
  both flags.

### Plans (none started)

1. Revert `WwanAutoSense` to `Disabled` (`flip_wwan_autosense.sh` with
   `TARGET=Disabled`) to remove an unexamined variable.
2. Test `--test-quick-suspend-resume` against the current flag, accepting the
   risk that the original stay-awake wedge returns.
3. Instrument which packets are pending at the `-EBUSY` (MBIM control traffic
   from the post-resume timeouts is the candidate).
4. Run the never-run E1–E3 matrix from
   `nix/debug/rugged/hw/modem_suspend_research.md` (E2 is the loaded-suspend
   case that predicted this failure).
5. Optionally carry `.no_m3` for `105b:e11d` once its behavior on a running
   kernel is understood.
