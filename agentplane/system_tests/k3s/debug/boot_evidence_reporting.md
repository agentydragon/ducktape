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

## Candidate change

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

Pending hosted validation of the candidate reporter with the existing guest-log
checks and the unchanged VM scenario target. Inspect the raw console, all seven
property blocks, all five unit definitions, journal section, required command
statuses, service result, OOM containment and cleanup receipts before selecting
the change. A passing unit parser test alone does not establish the producer
repair.
