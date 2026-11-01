# Concurrent VM scenario cleanup

[PR #12999](https://github.com/agentydragon/ducktape/pull/12999) gives each concurrent
VM scenario its own client and Sandbox ownership scope. Diagnostics precede deletion;
the finished scenario releases its guest while its peers continue. Both groups await
teardown before advancing on the shared k3s stack. These measurements describe the earlier
six-guest layout, before the two credential scenarios moved into the turn guests.

## Measurements

All three full `//agentplane/system_tests/suites:test_vm` actions passed on RBE on
2026-10-30. Measurements use the existing `vm-phases.jsonl` spans,
`k3s-timeline.jsonl`, and Bazel `TestResult` events. No timing instrumentation was added.

| Measurement, seconds          | Baseline | Scoped cleanup | PR CI |
| ----------------------------- | -------: | -------------: | ----: |
| Test action                   |    808.0 |          739.4 | 592.3 |
| Stack ready                   |    118.9 |          308.3 | 112.9 |
| Credentials and sampler group |    270.2 |          171.1 | 197.2 |
| Credential scenario window    |    217.9 |          151.0 | 176.2 |
| Final credential cleanup tail |     52.0 |           20.0 |  20.9 |
| Three-VM turn/OOM group       |    363.9 |          218.1 | 233.4 |
| Turn/OOM scenario window      |    280.0 |          197.1 | 210.9 |
| Final turn/OOM cleanup tail   |     83.6 |           21.0 |  22.5 |

The scenario window includes boot, readiness, setup, and assertions, ending when the
last named scenario body returns. A cleanup tail starts there and ends when the last
owned Sandbox deletion completes; diagnostics are included if they occur between
those events. The sampler owns its resources separately and has no named scenario
span here, so the credential window/tail measures only the two credential guests.
The group time includes all three tasks and their teardown.

The combined final cleanup tails fell from **135.6 s to 41.0–43.4 s**, a reduction of
**92–95 s (68–70%)**. Total test-action time fell by 69 s in the isolated comparison
and 216 s in CI. Those totals also include substantial startup and scenario variation;
these samples establish cleanup overlap, not a stable whole-suite speedup percentage.

The isolated candidate spent another **396 s queued** before execution, versus 0.06 s
for the baseline. Queueing is excluded from the test-action durations above and
explains why waiting for the remote result took longer despite a shorter action.

## Observed overlap

Seconds below are relative to the start of the turn/OOM group's first scenario:

| Scenario       | Baseline body ends | Baseline cleanup | Scoped body ends | Scoped cleanup |
| -------------- | -----------------: | ---------------: | ---------------: | -------------: |
| Codex turn     |              203.7 |      334.4–363.7 |            151.5 |    151.5–173.2 |
| Claude turn    |              249.2 |      306.0–334.4 |            178.7 |    178.8–201.0 |
| Codex tool OOM |              280.0 |      280.0–306.0 |            197.1 |    197.1–218.1 |

The candidate deletes the Codex turn guest before either peer finishes. Claude
deletion overlaps the OOM scenario, and the final two deletions overlap each other.
The CI repeat shows the same ordering, with a 22.5 s final tail.

The candidate artifacts contain one stack-ready marker and six distinct VM launcher
console identities. The common ownership traces record five distinct Sandbox UIDs,
including three overlapping boot/readiness spans for the turn/OOM group; the sampler
uses its own lifecycle. Each traced diagnostic capture finishes before its Sandbox's
deletion starts. All five traced deletions finish before their group's exit, and the
credential group completes before the next three guests are created.

Six focused ownership tests also passed: cleanup while a peer runs; readiness,
assertion, diagnostic, and deletion failures; and cancellation during teardown. Four
existing Sandbox lifecycle tests passed. CI passed on the implementation head.

## Reproduction and evidence

Run the full suite with `bbr test //agentplane/system_tests/suites:test_vm
--nocache_test_results --test_output=errors`. For ownership and lifecycle checks,
run `bbr test //agentplane/system_tests:test_scenario
//agentplane/system_tests:test_sandbox`.

- Baseline: `013375be4156cf5e9471e166914fa206b3611757`,
  [invocation `42c598db`](https://app.buildbuddy.io/invocation/42c598db-032a-4702-a70f-25043641171d).
- Scoped cleanup: the patch committed as `72358840c97fc35a90dcca6cb829b4a23c87e7bd`
  on the same base,
  [invocation `907c5240`](https://app.buildbuddy.io/invocation/907c5240-0e12-4712-ac0a-44de4aacbda4).
- PR CI: merge snapshot `92c75c9a93fb028dc9634bdad29f030968ebe8ba`,
  [invocation `6e48e8fd`](https://app.buildbuddy.io/invocation/6e48e8fd-2b4f-51e6-9a06-0c856f145271),
  [successful job](https://github.com/agentydragon/ducktape/actions/runs/41026311865/job/123965253227).

Download `test.outputs/vm-phases.jsonl` and `test.outputs/k3s-timeline.jsonl` for the
VM target from each invocation. Match cleanup and diagnostic spans by `sandbox.uid`,
then compare their timestamps with the named `vm.scenario` spans and enclosing
`vm.case` spans. Use the target's `TestResult.testAttemptDuration` for action time,
not the outer remote-runner or workflow duration.
