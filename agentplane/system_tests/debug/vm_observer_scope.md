# Limit cgroup sampling to the OOM guest

`guest/cgroup_observer` publishes privileged, read-only kernel snapshots every
250 ms. `MEMORY_PROBE` in `scenarios/vm_sandbox.py` is their only consumer: the
Codex OOM scenario waits for a new snapshot before and after the intentional
OOM, then verifies the same executor leaf and its counters. Ordinary harness
turns read their own kernel properties directly.

The OOM template explicitly adds `agentplane.cgroup-observer=1` to its kernel
arguments. The installed observer unit consumes that flag with
`ConditionKernelCommandLine`. Other VM templates retain the unit for their
unprivileged restart-denial test, but do not start its polling process. Runtime's
existing dependency on the observer remains, so the selected OOM guest starts
sampling before Runtime. No resource budget, deadline or assertion changes.

## Native work removed

The same-source control
[`89ec30fd-5594-40a6-8a53-3f165d1b6967`](https://app.buildbuddy.io/invocation/89ec30fd-5594-40a6-8a53-3f165d1b6967)
passed all three concurrent VM scenarios. Native systemd CPU accounting reports
10.728 CPU seconds for the ordinary Codex guest's observer, 14.426 for Claude,
and 14.742 for the OOM guest. Only the last process's snapshots were consumed.
These are guest CPU seconds, not wall-time savings; guest scenarios overlap.

All experiments use source
`2a7055db3c0e0d529160b73ccd0928b7561c8e5c` plus the identified working-tree
patch. The control TestRunner action digest is
`3569e6efef0d6b0ea77c951752f99846ae5cc24c4ce56f7556b0df9230cefd1a/1891`.
The observer experiment is
[`a9603fff-8663-4dae-8cb5-1014ae9d250c`](https://app.buildbuddy.io/invocation/a9603fff-8663-4dae-8cb5-1014ae9d250c).

## Candidate validation

The VM target passed all three scenarios in `a9603fff`: both ordinary observer
units were condition-skipped with no process started, while the OOM observer
consumed 12.426 guest CPU seconds. Its retained OOM result reports one leaf
`oom_kill`, tool exit 137, follow-up exit 0, and unchanged Runtime/app-server
identities. Guest cleanup completed and all three workspace loops were released.

| Run               | Infrastructure | VM action |   Setup |    Case | Ready: Codex / OOM / Claude |
| ----------------- | -------------- | --------: | ------: | ------: | --------------------------: |
| First control     | Cold           |   401.6 s | 109.5 s | 215.6 s |       81.6 / 86.5 / 100.4 s |
| OOM-only observer | Warm           |   384.0 s |  47.6 s | 258.0 s |     143.2 / 151.9 / 162.7 s |

The candidate removes the two unused polling processes; the control's combined
25.154 guest CPU seconds are the observed work avoided, not an estimated wall
saving. No readiness, case or gate speedup is established. Candidate PodScheduled
transitions occurred 58–59 s after the readiness wait began versus 4–6 s in the
first control, before the guest code ran. Worker/cache/provisioning conditions
are not matched. Native Runtime unit activation was 36.4/36.9/37.6 s in the
candidate versus 37.3/40.0/40.2 s in the first control; these selected guest clocks
do not explain the external provisioning difference.

The host's `WorkloadReady`→connected intervals were 52/60/68 s in the
candidate and 52/61/72 s in the control. The first Runtime bootstrap log's CRI
receipt preceded connection by 9.8/14.1/8.0 s versus 9.3/13.4/8.4 s. These
receipt-based tails have buffering and condition timestamp resolution limits;
they do not map guest monotonic activation directly to host UTC. Most excess
readiness time in this selected candidate is ahead of launcher scheduling,
not an increased observed post-bootstrap tail.

The initial invocation also ran `test_render`, which failed two assertions that
assumed OOM and ordinary templates started with identical boot flags. The tests
now preserve each selected template's original arguments and verify that the
other guest does not receive the requested profiling flag. The corrected
[`a47ef816-3d64-4ac2-a98f-fa4b851f6a23`](https://app.buildbuddy.io/invocation/a47ef816-3d64-4ac2-a98f-fa4b851f6a23)
render test passed. The initial invocation remains failed overall; its VM target
passed independently.

## Other bounded probes

A workspace `nofail` probe preserved Runtime's explicit mount requirements and
overlapped tmpfiles with formatting. Its first run,
[`78f44c55-ad43-48fb-9247-141f90aa5dd8`](https://app.buildbuddy.io/invocation/78f44c55-ad43-48fb-9247-141f90aa5dd8),
failed before intentional OOM: the execution backend's initialize write could
not acquire its lock before its deadline. No kernel OOM was recorded. The run
was slower throughout all three guests: action 507.0 s, case 287.2 s,
readiness 117.0/128.2/150.3 s. The unchanged repeat,
[`a1e1f30d-5e54-4206-8058-10a0cf22d8b1`](https://app.buildbuddy.io/invocation/a1e1f30d-5e54-4206-8058-10a0cf22d8b1),
passed at 337.0 s with a warm 44.1 s setup, but case 220.7 s and readiness
94.2/100.9/113.8 s did not improve on the first control. The lower whole-action
cost is not attributed to mount ordering. The separate real storage replacement
case
[`367546d8-65a8-4f97-85bd-f2ebc8dabf17`](https://app.buildbuddy.io/invocation/367546d8-65a8-4f97-85bd-f2ebc8dabf17)
passed across three guest boots. The mount change is not selected.

The second unmodified control,
[`27dcadb9-746c-4096-8838-3228a953b0bc`](https://app.buildbuddy.io/invocation/27dcadb9-746c-4096-8838-3228a953b0bc),
retains a separate 116.4 s physical attempt terminated by the executor's OOM
killer (provider code 14) before BuildBuddy's internal retry. Its action digest
matches the first control. The final VM case passed but teardown failed because the boot monitor's readiness
receipt was missing; its 503.5 s physical action is not a passing control. A
retry does not erase the earlier provider failure.

A separate `quiet` kernel-argument probe,
[`0c7e1b08-c659-42a9-9484-a68e3fca58dc`](https://app.buildbuddy.io/invocation/0c7e1b08-c659-42a9-9484-a68e3fca58dc),
retained a 161.2 s provider pause/socket failure followed by a passing 301.9 s
physical action (warm setup 43.1 s, case 205.3 s). Its 8.9–9.0 s kernel phases
and 93.5/100.7/110.5 s readiness waits did not demonstrate a guest startup
improvement. This verbosity change is not selected either.

## Reproduction and limits

```bash
bbr test //agentplane/system_tests/suites:test_vm \
  //agentplane/system_tests/deployment:test_render \
  --nocache_test_results --test_output=errors
bbapi artifact list INVOCATION --kind test --json
bbapi execution INVOCATION --json
journalctl --file=SYSTEM_JOURNAL --no-pager --output=short-monotonic
```

Use `test.xml` for the verdict; `test.outputs/test-spans.jsonl` contains setup,
readiness and scenario intervals. Full native journals under
`test.outputs/vm-artifacts/journals/` identify the skipped ordinary observer and
the selected OOM observer's CPU accounting. Retain every physical execution,
including provider failures and retries. These selected experiments have
uncontrolled workers/cache placement and do not establish fleet or required-gate
savings. This is the Debian/QEMU TCG test guest, not production NixOS/KVM.
