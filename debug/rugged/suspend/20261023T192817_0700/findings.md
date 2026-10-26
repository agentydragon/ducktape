# Findings

Collected 2026-10-23 19:29–19:30 PDT. This snapshot did not trigger suspend or
change system state.

## Current state

- ModemManager is active with `--test-low-power-suspend-resume`.
- Google Fi is connected. ModemManager reports the Foxconn modem connected on
  LTE and 5G NR.
- The system route metrics prefer Wi-Fi (600) over WWAN (1050).
- The modem PCI function currently has `power/control=on` and
  `runtime_suspended_time=0`; sysfs reports `runtime_status=unsupported`.
- The selected sleep mode is `s2idle`.
- `/sys/power/suspend_stats` reports 2 failures and 4 successes since boot.
  The last failure is device `0000:71:00.0`, errno `-16`, step `suspend`.
- At collection time, Chrome held a `shutdown` delay inhibitor for saving
  browser state. It did not hold a `sleep` inhibitor in `systemd-inhibit`; the
  GNOME session inhibitor list was empty. This describes the current time, not
  the inhibitor state during the earlier failures.

## Oct 23 suspend sequence

- 14:54:29 — ModemManager logged that the system was resuming.
- 14:54:49 — ModemManager logged “about to suspend” and immediately dropped its
  inhibitor. No modem `power state updated: low` message appears in this window.
- 14:54:50–14:54:51 — two kernel suspend attempts failed in
  `mhi_pci_suspend` with `-16` (`EBUSY`).
- 14:55:03 — ModemManager logged `modem2` changing from unknown to disabled.
- 14:55:12 — ModemManager again dropped its inhibitor; the kernel entered
  suspend at 14:55:13 and resumed at 16:43:50.
- 17:14:14–17:14:15 — before the next successful suspend, ModemManager logged
  the modem entering low power. The system resumed at 19:15:08; ModemManager
  reported the modem on at 19:15:42.

## Follow-up lead

The two failures happened about 21–22 seconds after resume. The next attempt
succeeded about 43 seconds after resume, after ModemManager had logged the
modem becoming disabled. This makes a post-resume modem-readiness/timing window
a stronger lead than Chrome itself, but one sequence does not establish cause.
The next useful comparison is an otherwise identical suspend attempt soon after
resume versus one after the modem has returned to a stable connected state,
capturing MHI trace events and ModemManager transitions in both cases.

## Initial capture instrumentation

- `nix/nixos/hosts/rugged/foxconn-wwan.nix` now defines the
  `mhi-suspend-trace.service` probe for Ducktape issue
  [#11789](https://github.com/agentydragon/ducktape/issues/11789). It records
  the inner `mhi_pm_suspend()` return value plus `dev_wake` and `pending_pkts`
  at that return, so it can distinguish those two `-EBUSY` causes and reveal
  errors that the PCI callback might map to `-EBUSY`.
- At the time of the initial capture, the running kernel exposed
  `/sys/kernel/btf/mhi` and had kprobe/BPF support, but the tracing service had
  not yet been activated. The full generation build was blocked by a Home
  Manager collision: AIQuota and Plaid Spend both installed compiled bytecode
  for `util.network_monitor`.

## Traced suspend attempt

- After the packaging collision was fixed in PR
  [#11796](https://github.com/agentydragon/ducktape/pull/11796), the user
  switched the configuration. `mhi-suspend-trace.service` was active and
  started at 20:51:48 PDT.
- At 20:53:18 PDT, the kernel entered suspend in `s2idle`. At 20:53:20 PDT,
  the tracer recorded `mhi_pm_suspend_ret=0`, `dev_wake=0`,
  `pending_pkts=0`; the kernel logged suspend exit at the same second.
- This attempt completed successfully. It does not reproduce the earlier
  `-EBUSY` failures, and one successful sample does not explain the intermittent
  failures.
- At service startup, `bpftrace` warned that the return value from its map
  `delete()` call was discarded. The service remained active and recorded the
  suspend result.
