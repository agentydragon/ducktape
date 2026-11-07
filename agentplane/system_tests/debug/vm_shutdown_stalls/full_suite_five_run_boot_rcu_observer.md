# Five-run full-suite boot RCU observation

On 2026-11-07 UTC, the opt-in full-suite diagnostic ran five uncached executions of
`//agentplane/system_tests/suites:test_vm` on BuildBuddy invocation
[9b5c7957-9a8f-4dfc-ae3c-2956b9271be2](https://app.buildbuddy.io/invocation/9b5c7957-9a8f-4dfc-ae3c-2956b9271be2).
The runner checked out `devel` at
`704872b9e0265128a4f7bce33083530aa280b35e` with no patch applied. The command
was:

```sh
bbr test //agentplane/system_tests/suites:test_vm \
  --test_env=AGENTPLANE_VM_BOOT_RCU_OBSERVER=1 \
  --runs_per_test=5 --flaky_test_attempts=1 \
  --nocache_test_results --jobs=5
```

All five test runs passed. The initial RBE execution for run 2 failed its worker
VM health check with `DeadlineExceeded` before yielding a test result; BuildBuddy
re-executed that run and the replacement passed. This was an executor failure,
not a guest failure or test-level flaky retry.

Each guest serial contained paired begin/end markers for every boot sample
captured, with no sample errors or serial truncation. Per-run sample counts are
shown in `vm-1`, `vm-2`, and `vm-codex-oom` order:

| Run | Result | Guest samples   | Host records | Pod-list query timeouts |
| --- | ------ | --------------- | -----------: | ----------------------: |
| 1   | Passed | 126 / 100 / 109 |          284 |                      10 |
| 2   | Passed | 71 / 58 / 60    |          144 |                       0 |
| 3   | Passed | 98 / 77 / 93    |          200 |                       1 |
| 4   | Passed | 92 / 72 / 80    |          182 |                       1 |
| 5   | Passed | 94 / 76 / 84    |          200 |                       1 |

All 15 guest serials showed shutdown proof exit status 0, ACPI S5 entry, and
`reboot: Power down`. The retained serials contained no `rcu_preempt` or
`rcu_sched` stall reports, panic/oops, soft-lockup, or hung-task messages. The
suite completed readiness and shutdown paths in all five runs; run 1's slowest
`sandbox.ready` span was about 130 seconds.

The host observer correlated three launcher Pod UIDs to their VMI owner UIDs in
each run. It found QEMU process matches for 92–232 samples per launcher; process
scans were not incomplete, and neither the launcher list nor captured host
bytes were truncated. It wrote 144–284 sample records per run and stopped at
test-context teardown, before the 360-second window limit. The guest summary
is partial in every run because ordinary guest shutdown occurred before the
360-sample completion receipt; partial sampling does not establish absence
outside those windows.

The host collector recorded Pod-list query timeout counts of 10, 0, 1, 1,
and 1 for runs 1–5. Run 1's JSONL identifies all ten as `TimeoutExpired`; the Pod-list
query has a 1.5-second timeout. The first launcher was observed at
07:04:10.383 UTC. Two timeouts, at 07:04:39.114 and 07:04:40.631 UTC, overlapped
the three VMs' readiness spans (07:04:06.691–07:06:16.574 UTC). The other eight
occurred from 07:06:22.531 through 07:06:50.069 UTC, after readiness and before
the first sandbox cleanup at 07:07:47.106 UTC. These are gaps in the host
samples, not evidence of a VM or API stall. The remaining runs had at most one
such gap each.

This batch did not reproduce the intermittent stall. The observer captured
partial guest windows with several short host-query gaps, so the result does not
rule out behavior outside the retained samples. Raw per-run logs and serials
were reviewed from BuildBuddy and remain local; this note contains no raw test
artifacts.
