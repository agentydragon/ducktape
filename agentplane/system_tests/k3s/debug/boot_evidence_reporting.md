# VM boot-evidence reporter deadlines, 2026-11-02

The retained-handler experiment exposed two failures in the guest boot-evidence
producer. Neither was lost console output or the scenario's intentional OOM. The
native journals and original CRI console artifacts agree on the failure boundary.
The fixture's existing completeness assertions detected the incomplete reports.

## Failure evidence

- Candidate invocation
  [`5cdf4230-56bc-4cbe-b4ae-e199cf5e09b0`](https://app.buildbuddy.io/invocation/5cdf4230-56bc-4cbe-b4ae-e199cf5e09b0):
  seven required commands ended with status 143 under `busybox timeout 2`.
  They were three unit-property queries, three unit-definition queries and the
  filtered journal query. The service started at guest monotonic 45.123388s;
  systemd stopped it at 80.279765s with `Service reached runtime time limit`,
  then recorded result `timeout`. The script was in an optional critical-chain
  probe and never reached its terminal receipt. It consumed 6.737 guest CPU
  seconds. The intentional OOM occurred later, around 160.86s.
- Control invocation
  [`b60e8735-0ff4-491e-be29-887156b17e15`](https://app.buildbuddy.io/invocation/b60e8735-0ff4-491e-be29-887156b17e15):
  the required `systemctl show agentplane-boot-device-monitor.service` query
  ended with status 143. Other required queries completed. The reporter
  emitted `status=partial` and `AGENTPLANE_BOOT_EVIDENCE_REQUIRED_FAILURES 1`,
  then deactivated successfully at guest monotonic 87.548556s. Its optional
  critical-chain and blame commands also timed out. It consumed 4.580 guest
  CPU seconds; the deliberate OOM occurred later, around 198.6s. This is the
  final physical attempt; an earlier separate provider executor OOM remains
  a separate failure.

The serial end receipts are written directly to `/dev/ttyS0`, so their absence
from native journal messages alone is not evidence of a missing upload. The
original console confirms the control's partial receipt and the candidate's
missing terminal receipt. Neither journal exposes a numeric `ExecMainStatus`
for the reporter's exit; the systemd result and command receipts are the
observed evidence.

## Repair

Query the same seven units' properties and the same five units' definitions in
two batched systemctl calls. The manager clock query remains separate. Required
commands share the unchanged 35-second service lifetime instead of racing
individual two-second deadlines. A failed required command still increments
the partial-report count; a wedged required collector still reaches the
service deadline without producing a complete receipt.

Certify required sections before running optional analyzers. Each optional
analyzer retains its two-second bound and command status, but cannot prevent
an already complete required report from being recognized. Monitor readiness,
all four required sections, selected-guest identity and the exact terminal
receipt remain mandatory in `guest_logs.py`; shutdown and scenario assertions
are unchanged.

## Validation

Fresh RBE invocation
[`4ef89740-adbd-4462-8cc6-b1507999bc50`](https://app.buildbuddy.io/invocation/4ef89740-adbd-4462-8cc6-b1507999bc50)
passed both `test_guest_logs` (15.550s) and the unchanged `test_vm` target
(429.862s). The latter completed the real OOM containment and successful
follow-up, guest-shutdown validation, complete retained-baseline cleanup and
release of all three workspace loop devices. Its infrastructure baseline was
cold; this is not an aged warm-baseline experiment.

The OOM guest's raw console and native journal contain all seven expected
`Id=` property blocks, all five unit definitions, all required section receipts
and a complete required report. All 16 required commands exited zero.
Its optional critical-chain and blame probes
still returned 143, demonstrating that optional timeouts remain visible without
invalidating completed required collection. The guest journal received the
service-start message at monotonic 46.970245s and successful deactivation at
60.450927s; service accounting reports 2.102 guest CPU seconds.
The first optional probe's journal message was recorded
at 56.072725s, after the required completion receipt in the serial stream.
These are guest journal receipt times, separate from host CRI receipt times.

BuildBuddy records base commit `f551411f922a5f71398617c1d51b13fa832dc4d7`;
`bbr` mirrored the uncommitted reporter patch for this run. The resulting
batched command arguments and completion-before-optional order are present in
the artifacts. Commit `744755014987570633de41688daf3db9a6fcc1ab` contains the
same reporter bytes. Formatting and the unchanged property/definition-roster
comparison passed. Exact-head PR CI is separate from this fresh test invocation.
