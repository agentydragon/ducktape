# MHI suspend follow-up and MBIM resume timeouts

Reviewed existing journals and sysfs on rugged on 2026-10-27 at approximately
08:45 PDT. No suspend was triggered and no configuration changed. Kernel:
`7.2.9`; same boot as the Oct 23 failures.

## Additional suspend evidence

Since the [Oct 25 control sample](../20261025T182859_0700/findings.md), five
more suspend operations completed normally. All five probe samples recorded
`mhi_pm_suspend_ret=0 dev_wake=0 pending_pkts=0`.

| Suspend entry (PDT) | Resume (PDT)    |
| ------------------- | --------------- |
| Oct 25 19:28:48     | Oct 25 22:41:43 |
| Oct 26 00:22:45     | Oct 26 14:59:11 |
| Oct 26 18:39:54     | Oct 26 18:40:00 |
| Oct 26 18:40:33     | Oct 26 22:04:32 |
| Oct 27 00:19:25     | Oct 27 08:05:04 |

These are journal event timestamps, not measurements of hardware sleep time.
Before each attempt, ModemManager logged `power state updated: low` before
`ready to sleep; dropping inhibitor`.

The most useful comparison is Oct 26: the machine resumed at 18:40:00 and
suspended again at 18:40:33 while the modem was still being reprobed. At
18:40:33, ModemManager logged `unknown -> disabled`, confirmed low power,
and released its inhibitor. Suspend succeeded. Short time since resume alone
is therefore insufficient to explain the earlier failures; completion of the
modem low-power transition remains a candidate distinguishing condition.
This does not establish causality or test the exact 21–22-second failure window.

`/sys/power/suspend_stats` reports 11 successes and 2 failures. Both failures
are still the Oct 23 events: device `0000:71:00.0`, errno `-16`, step
`suspend`. No additional kernel MHI suspend failure appeared in the journal.
The tracer remains active; no failing inner MHI return has yet been captured.
Runtime PM remains pinned off: `power/control=on`,
`runtime_status=unsupported`, `runtime_suspended_time=0`.

## Open follow-up: recurring MBIM timeouts after resume

ModemManager repeatedly logs `[/dev/wwan0mbim0] MBIM error: Transaction timed out`
at or within two seconds of resume, typically six messages per resume. This
appears at the Oct 25 18:28:59 and 22:41:43 resumes, Oct 26 14:59:11,
18:40:00 and 22:04:32 resumes, and Oct 27 08:05:04 resume.
The modem is reprobed under a new modem number. On the five resumes followed
by reconnection rather than rapid re-suspend, it reached `connected` roughly
36–40 seconds after resume. The rapid re-suspend interrupted that sequence.

These warnings coexist with successful suspend operations. They do not by
themselves establish a suspend blocker, firmware wedge, or cause of the
reconnection delay. Possible actionable work is reducing cellular recovery
latency after wake, if the timeouts contribute to it.

When revisiting:

- Capture timestamped ModemManager and MBIM transaction diagnostics across
  sleep and wake; identify which requests expire and whether they originated
  before sleep or during resume. Keep raw captures private and sanitize findings.
- Separate transaction expiry, modem reprobe, registration, and bearer setup
  intervals to locate the actual recovery delay.
- Inspect the installed ModemManager/libmbim sleep handling and compare with
  current upstream changes once the failing transaction path is known.
- Compare a candidate change using resume-to-connected latency and successful
  subsequent suspend. Do not increase timeouts or alter low-power handling
  without evidence identifying the delayed operation.

For the original suspend failure, retain the current probe and capture an
actual `-EBUSY` result with the surrounding ModemManager transitions.
