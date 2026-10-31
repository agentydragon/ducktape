# VM tool OOM budget probe, 2026-10-31

The Codex tool OOM case previously used the ordinary VM test profile. Its native executor leaf had
`memory.max=268435456` (256 MiB), `memory.swap.max=0`, and `memory.oom.group=0`. The guest runs a
real Codex shell command that touches pages until kernel memcg OOM selects the command as its
victim. The exec-server, runner service, and the rest of the VM suite do not need that 256 MiB
tool budget.

The fixture now clones the VM SandboxTemplate and Runner config for this case alone, setting only
the native executor leaf's `memory_max_bytes` to 96 MiB. Guest RAM remains 1536 MiB; the service,
aggregate, and process memory budgets remain identical to the ordinary template. This keeps the
exec-server cold-start path and systemd/cgroup hierarchy under test while reducing the amount of
memory the tool must charge before the kernel kills it. A rendered-fixture test asserts the two
profiles differ only in that executor leaf limit.

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
