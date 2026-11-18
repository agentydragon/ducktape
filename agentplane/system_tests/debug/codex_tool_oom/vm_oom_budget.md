# VM tool OOM budget probe, 2026-10-31

## Current status, 2026-11-10

The OOM fixture now uses the ordinary 256 MiB native executor limit; its only special setting is
the guest cgroup observer. The 96 MiB override in the historical experiment below was removed after
a 96 MiB run killed both the intended command and the Codex process. A 256 MiB RBE control passed
with the existing single-kill and same-session recovery assertions intact; details and artifacts are
recorded at the end of this note.

The Codex tool OOM case originally used the ordinary VM test profile. Its native executor leaf had
`memory.max=268435456` (256 MiB), `memory.swap.max=0`, and `memory.oom.group=0`. The guest runs a
real Codex shell command that touches pages until kernel memcg OOM selects the command as its
victim. The original experiment assumed the exec-server, runner service, and rest of the VM suite did
not need that full tool budget.

The 96 MiB experiment cloned the VM SandboxTemplate and Runner config for this case alone, setting
the native executor leaf's `memory_max_bytes` to 96 MiB. Guest RAM remained 1536 MiB; the service,
aggregate, and process memory budgets remain identical to the ordinary template. This keeps the
exec-server cold-start path and systemd/cgroup hierarchy under test while reducing the amount of
memory the tool had to charge before the kernel killed it. A rendered-fixture test asserted the two
profiles differed only in that executor leaf limit.

## Evidence

The baseline source was `7d4079880e93f57bbc6019ba1134854fbf0219f8`. The candidate source was that
same base plus a four-file working-tree patch with SHA-256
`124b210296080b4e8b214f6318b370c3f596f9173610dfc5e3928aa7e5e3c408`. `bbr` synchronized from
`c032c1e5` and applied its full-tree patch, which reverted the intervening source changes to the
fixed `7d407988` base before adding this candidate diff. Thus the invocation's commit metadata
(`c032c1e5`) is the sync base, not the effective source revision. The baseline was the parent-run
full `//...` invocation; the candidate was a working-tree patch to `test_vm`. Both target results
passed. The OOM telemetry records one sample at each budget:

| Evidence                               |           Baseline |   96 MiB candidate |
| -------------------------------------- | -----------------: | -----------------: |
| Executor leaf `memory.max`             |          268435456 |          100663296 |
| `memory.swap.max` / `memory.oom.group` |              0 / 0 |              0 / 0 |
| Executor subtree OOM counter           |              0 → 1 |              0 → 1 |
| Native leaf `oom_kill` counter         |              0 → 1 |              0 → 1 |
| OOM command result                     | exit 137, 19973 ms | exit 137, 13108 ms |
| Same-session follow-up                 |             exit 0 |             exit 0 |

The guest consoles identify the selected process as `awk`, UID 107, with `oom_score_adj=1000`.
The candidate kernel report names the executor subtree as `oom_memcg`, the per-native-session
service as `task_memcg`, and PID 998 (`awk`) as the victim; its anonymous RSS was 85760 KiB. The
baseline report likewise killed the selected `awk` process (PID 979, 247296 KiB anonymous RSS).
In the candidate telemetry, the Runner boot ID, Runner process ID/PID, app-server native session
ID, and executor native session ID are unchanged before OOM, after OOM, and after the successful
follow-up. The follow-up probes the same leaf's counters and reports `VM_OOM_FOLLOWUP_OK`.

The full suite still starts its six independent VM scenarios and passed:

- Baseline [`test_vm` result](https://app.buildbuddy.io/invocation/7ea29197-b0ed-467f-adff-ee1720988897): 320.006 s test duration. The target was part of `//...`.
- Candidate [`test_vm` invocation](https://app.buildbuddy.io/invocation/c25af2fc-3f8b-47a3-9a3b-aef0ceb3bf8e): 310.6 s test duration (310.555 s TestRunner action).

The two TestRunner actions each waited about 0.06 s for a worker. The baseline worker was
`122c8a92-7593-4826-ac8a-3a587a6257ae`; the candidate worker was
`6ab38eac-87e0-45d4-9a1d-bd0b14336637`. They are different workers, not a controlled same-host
pair. The candidate TestRunner action overlapped the parent baseline's final 92 seconds and ran while the
parent's parallel RBE sweeps were active. The 9.4-second raw full-suite difference is therefore
not attributed to this change. The measured native OOM command span was 6.9 seconds (34%) shorter
in the candidate; this is one sample per budget and includes tool protocol and scheduling around
the native command as well as allocator work, not just allocator CPU time. This is a bounded
demonstration, not a population estimate.

## Reproduction

Run the full suite (it boots fresh VMs for each scenario) with:

```bash
bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results --test_output=errors
```

Baseline test telemetry and console came from the parent invocation above; candidate telemetry and
console came from the candidate invocation above. The candidate passed all assertions, including
the actual kernel OOM, the single leaf kill, runner/session survival, and the same-session follow-up.

The candidate Python libraries built in BuildBuddy invocation
[`dd96dd80`](https://app.buildbuddy.io/invocation/dd96dd80-b97a-434e-90ad-5c54804a24d8), and the
rendered-fixture test passed in
[`19a0dd4b`](https://app.buildbuddy.io/invocation/19a0dd4b-5575-4889-9583-9cefff2cdfa8). Normal
pre-commit hooks passed on all changed source and documentation files.

## Follow-up, 2026-11-10

The `test_vm` failure in [BuildBuddy invocation `198b8476`](https://app.buildbuddy.io/invocation/198b8476-2718-5d75-bbef-01a1c53beb72)
occurred under the 96 MiB limit. Its pre-OOM telemetry reported `memory.max=100663296`, swap and
`oom_group` disabled, and all counters at zero. The guest console first shows the selected `awk`
process killed at 95.7 seconds (`anon-rss=60544 KiB`). A later EROFS page fault reaches the same
96 MiB limit (`usage=limit=98304 KiB`) and the kernel kills Codex (`anon-rss=35916 KiB`), which
accounts for the lost tool session. The [invocation artifacts](https://app.buildbuddy.io/invocation/198b8476-2718-5d75-bbef-01a1c53beb72)
can be read with:

```bash
bbapi artifact cat 198b8476-2718-5d75-bbef-01a1c53beb72 vm-codex-oom.json
bbapi artifact cat 198b8476-2718-5d75-bbef-01a1c53beb72 1415586-virt-serial0-log.log
```

With the limit restored to 256 MiB, [RBE invocation `09bf7d75`](https://app.buildbuddy.io/invocation/09bf7d75-a63d-45e3-9d70-a1b3eef17660)
passed `//agentplane/system_tests/suites:test_vm` in 368.361 seconds. The full before/after cgroup
measurements were:

| Field                                             | Before OOM            | After same-session follow-up |
| ------------------------------------------------- | --------------------- | ---------------------------- |
| Native executor cgroup                            | Same native leaf path | Same native leaf path        |
| `memory.max`                                      | 268435456             | 268435456                    |
| `memory.swap.max` / `memory.oom.group`            | 0 / 0                 | 0 / 0                        |
| Executor subtree `oom` events                     | 0                     | 8                            |
| Native leaf `oom` / `oom_kill` / `oom_group_kill` | 0 / 0 / 0             | 0 / 1 / 0                    |

The OOM command returned 137 after 18471 ms. The console records exactly one kernel kill: `awk`
PID 1226, `oom_score_adj=1000`, `anon-rss=217856 KiB`, at a 256 MiB cgroup usage. The Runner boot
ID, process ID/PID, app-server session, and native session remained unchanged immediately after
the kill and after the follow-up, which returned 0 and printed `VM_OOM_FOLLOWUP_OK`. The executor
subtree event count is not a victim count; the test checks it increased, while the leaf's
`oom_kill` counter verifies the one killed process. This one control demonstrates that the
256 MiB profile can complete the full asserted path in one RBE run; it does not estimate its
failure rate.

The [control invocation artifacts](https://app.buildbuddy.io/invocation/09bf7d75-a63d-45e3-9d70-a1b3eef17660)
can be read with:

```bash
bbapi artifact cat 09bf7d75-a63d-45e3-9d70-a1b3eef17660 vm-codex-oom.json
bbapi artifact cat 09bf7d75-a63d-45e3-9d70-a1b3eef17660 1415560-virt-serial0-log.log
```
