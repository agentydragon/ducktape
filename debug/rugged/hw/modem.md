# 5G Modem — Foxconn DW5934e (SDX72)

**Goal**: Google Fi internet on the tablet.

**Current state (2026-05-02)**: Fully declarative. FCC unlock, modem
enable, and Google Fi connection all happen automatically via the NixOS
module <nix/nixos/hosts/rugged/foxconn-wwan.nix>. Works alongside WiFi
(IPv6 is temporarily disabled while we distinguish a proven IPv4 PMTU issue
from untested native Fi IPv6; see <../network.md>).
**Suspend/resume (2026-05-19)**: At least 2 suspend/resume cycles
survived without a wedge (2026-05-18/19). The
`--test-low-power-suspend-resume` MM flag appears to be doing its job.
Modem re-enumerates on each resume (modem0 → modem1 → modem2) but
connects to Google Fi reliably. No MHI/SBL wedge observed. See
`modem_suspend_research.md` for the full workaround history.
**Runtime-PM wedge found (2026-06-08; recurred 2026-06-22)**: the
system-suspend workaround left a gap — kernel _runtime-PM_ autosuspend
(2s idle delay) was driving the modem into the same broken M3 path
independently of system sleep, and wedged it on 2026-06-05 and again on
2026-06-22 (SBL/PBL trap, no channels, invisible to MM). The first
mitigation, a udev `ACTION=="add"` rule pinning `power/control=on`, was
installed and valid but incomplete: current live state still showed
`power/control=auto` because the MHI PCI driver enables runtime
autosuspend after the early add event. Current local fix in
`foxconn-wwan.nix`: broaden the direct udev write to add/bind/change and
start a `foxconn-wwan-disable-runtime-pm.service` verifier when `wwan0`
appears. This is intentionally userspace-only for now: no local kernel patch
is carried. Remaining upstream-grade work would be teaching the Linux MHI
driver not to enter M3 for this SKU. See
`modem_suspend_research.md` §"Runtime-PM recurrence after the udev rule"
and §"Avenues to attack".
**Throttle appears lifted**: `curl --interface wwan0
http://speedtest.tele2.net/1MB.zip` measured **~158 KB/s (~1.27 Mbps)**
on LTE on 2026-05-02 with WiFi off — ~20× over the previously
documented ~7.3 KB/s Google Fi QoS cap. Re-verify with the 10MB file
and confirm the device shows up in the Fi app before closing the
throttle TODO.

**5G NR works at this location** (2026-05-02, via
`debug/rugged/modem.sh try-5g`): switched modem from `allowed: 4g;
preferred: none` to `allowed: 4g, 5g; preferred: 5g` and it registered
on 5G NR with **RSRP -100 dBm, S/N +5.5 dB**, throughput
**~397 KB/s (~3.2 Mbps)** on the same tele2 1MB probe (~2.5× LTE).
LTE coverage at this spot is essentially absent (RSRP -156 dBm), so
5G is the only realistic option here — leave preferred=5g. Note: the
modem briefly drops the GSM bearer during the mode switch and NM
auto-reconnects within a few seconds; `try-5g` masks any transient
failure with `|| true` on the `nmcli connection up` call, which can
make the down-and-back look alarming. See "Harden try-5g" TODO.

## Where to find what

| Topic                                | File                                          |
| ------------------------------------ | --------------------------------------------- |
| Status, TODOs, what to do next       | this file                                     |
| Hardware specs, NixOS config         | <esim.md> §Hardware, §NixOS Configuration     |
| SIM inventory + slot setup           | <esim.md> §SIM Inventory, §eSIM Slot Setup    |
| `lpac` / `mbimcli` reference cheats  | <esim.md> §lpac Commands, §Useful Diagnostics |
| FCC unlock root cause + history      | <foxflss_wwan.md> §What Works, §Watchdog      |
| eSIM provisioning & wedge debugging  | <foxflss_wwan.md> §Modem Reset Methods etc.   |
| Throughput / throttle investigations | <foxflss_wwan.md> §Physical SIM throughput    |
| Suspend/resume root cause + fix plan | <modem_suspend_research.md>                   |
| Wi-Fi/WWAN DNS and Git SSH delays    | <../network.md>                               |

## Tools

`debug/rugged/modem.sh` is the single entry point for diagnostics and
SIM operations: `status`, `diagnose [--kill-wifi]`, `slot <0|1>`,
`esim {status|wipe|activate}`, `unlock`, `try-5g`, `recover`. Run with
no args (or `--help`) for the full subcommand list.

`status` opens with an MHI / PCI section that identifies the SBL
firmware-wait wedge at first glance (mhi0 channels NONE, runtime_status
active, `/dev/wwan0*` empty), followed by a `foxflss-watchdog`
section that flags hot-spinning when MM has no modem to watch. The
final section shows a filtered kernel + MM event log anchored to the
last suspend/resume marker — that's where MHI-side errors land if
there are any to see.

**TODO**:

- **Re-verify the Google Fi QoS throttle is gone** — spot-check on
  2026-05-02 hit ~158 KB/s on the 1MB tele2 file, but a single
  measurement isn't conclusive. Run `debug/rugged/modem.sh diagnose`,
  pull the full 10MB (`curl --interface wwan0
http://speedtest.tele2.net/10MB.zip`), and confirm the device is
  listed in the Fi app. If both pass, delete this TODO and the
  "Physical SIM throughput" section in `foxflss_wwan.md`. If throttle
  is back, the original fix is to bind IMEI `356398950074094` to the
  Fi account alongside ICCID `8901240270139815559` via data-only SIM
  activation at <https://fi.google.com>.
- ~~Verify FCC unlock + auto-connect works from cold boot~~ — resolved
  2026-04-30: `fcc-unlock.d/105b:e11d` was failing silently (no
  `dmidecode` on the systemd PATH); fixed in `nix/packages/foxflss.nix`,
  MM's reactive unlock now fires correctly. Watchdog
  (`foxflss-watchdog.service`) added as safety net. See
  `foxflss_wwan.md` "Watchdog + dmidecode fix".
- **Try removing `foxflss-watchdog.service`** after ~1 month of zero
  fires. Verify with
  `journalctl -u foxflss-watchdog --since '30 days ago' | grep -c stuck`
  (should be 0 across boots, suspend/resume, and slot switches), then
  delete the systemd unit, `foxflss_watchdog.py`, and the
  `watchdogScript` let-binding in `foxconn-wwan.nix`. The watchdog was
  built before we found the dmidecode root cause and turned out never
  to have been the actual unlock path; it's defense-in-depth against
  MM giving up on a future transient FoxFlss failure (per upstream
  contract, MM doesn't retry the script after one failure).
- **Suspend/resume wedge — workaround confirmed (2026-05-19)**:
  Full mechanism trace in <modem_suspend_research.md>. At least 2
  suspend/resume cycles survived without wedge (2026-05-18/19). The
  `--test-low-power-suspend-resume` MM flag in `foxconn-wwan.nix` is
  doing its job: MM puts the modem in LOW_POWER before suspend, modem
  avoids the broken MHI M3 path, and re-enumerates cleanly on resume
  (modem0 → modem1 → modem2 per boot). **WwanAutoSense=Enabled
  hypothesis was disproved**: post-reboot `reset_method` is still `flr
bus` (no `acpi`); see research doc for the DSDT/WWEN analysis.
  Remaining: run formal E1/E2/E3 matrix (especially E2 — in-flight
  traffic) to confirm behavior is deterministic.
- ~~Investigate weak signal (9% vs 92% observed manually)~~ — root
  cause confirmed 2026-05-02: at this location LTE coverage is
  essentially absent (RSRP -156 dBm), so the auto-connect path locked
  to LTE-only and got terrible signal. Switching to `allowed: 4g, 5g;
preferred: 5g` via `modem.sh try-5g` lands on 5G NR (RSRP -100 dBm,
  S/N +5.5 dB, ~3.2 Mbps). Open question: should the NixOS auto-bring-up
  default to allowed=4g+5g preferred=5g instead of the implicit 4g?
  Test elsewhere first — at locations with strong LTE the current
  default may behave better. Calibration-data theory was wrong; signal
  is fine when the modem is allowed to pick its tech.
- **Harden `modem.sh try-5g`** — the mode change forces the modem to
  re-register, which drops the GSM bearer for a few seconds; NM
  auto-reconnects but the script's `nmcli connection up "$GSM_CONN"
|| true` swallows transient failures and makes diagnosis confusing.
  Better: explicitly `nmcli connection down` before the switch, then
  `nmcli connection up` with retries and surface the result. Also
  consider downing the connection during the revert path so the trap
  output reflects what actually happened.
- **Long-term: drop the closed-source `FoxFlss` binary**. Once nixpkgs
  ships libqmi ≥ 1.38.0 (currently 1.36.0), upstream MM's
  `fcc-unlock.available.d/105b` script can do the job via the FOX
  service (`qmicli --fox-set-fcc-authentication`). The FOX service
  (0xE3) is confirmed working on our SDX72 —
  `qmicli --fox-get-firmware-version` returned
  `FDE2.F0.0.0.1.2.TO.003.062`. Track the libqmi bump in nixpkgs and
  rewrite `fcc-unlock.d/105b:e11d` to call qmicli instead of FoxFlss.
  At that point the `foxflss` package, its `/opt/foxconn/data/`
  symlinks, and the dispatcher script all collapse. The watchdog
  remains separate-cleanup (see TODO above).
