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

| Time (PST) | Event                                                                                       |
| ---------- | ------------------------------------------------------------------------------------------- |
| 19:11:02   | ModemManager: `connected -> disabled`, `power state updated: low`, drops inhibitor          |
| 19:11:03   | First suspend; probe `ret=0`; resume at 19:11:14                                            |
| 19:11:14   | Six MBIM `Transaction timed out`; ports reprobed from 19:11:16                              |
| 19:11:26   | "about to suspend" and "ready to sleep; dropping inhibitor" in the same second              |
| 19:11:27   | Kernel suspend fails twice: `ret=-16 pending_pkts=4`                                        |
| 19:11:35   | ModemManager creates `modem20`; `unknown -> disabled` at 19:11:47                           |

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
