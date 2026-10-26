# MHI suspend trace sample

Collected after the suspend that resumed at 2026-10-25 18:28:59 PDT.

## Observation

- `mhi-suspend-trace.service` was active and emitted a sample for this attempt:
  `mhi_pm_suspend_ret=0 dev_wake=0 pending_pkts=0`.
- The kernel logged `PM: suspend exit`, and systemd reported that the suspend
  operation returned normally.
- No MHI `-EBUSY` result appeared for this attempt.
- After resume, ModemManager reprobed the WWAN ports and reconnected the modem.

## Interpretation

This is a useful successful control sample: the probe is attached to the MHI
suspend path and observes a clean return with neither `dev_wake` nor queued
packets set. It does not reproduce the earlier `-EBUSY`, so it does not identify
the cause of those failures. The journal timestamps for entry and exit are
emitted around resume and do not establish how long the machine was asleep.

## Focused journal evidence

```text
2026-10-25T18:28:59-07:00 bpftrace: mhi_pm_suspend_ret=0 dev_wake=0 pending_pkts=0
2026-10-25T18:28:59-07:00 kernel: PM: suspend exit
2026-10-25T18:28:59-07:00 systemd-sleep: System returned from sleep operation 'suspend'.
```
