# VM suite sizing on the existing KVM pool

The full three-scenario VM suite passed on the existing `kvm-vm` pool's 4-CPU/8-GiB KubeVirt worker.
The previous 10-GB scheduler request was unmeasured and exceeded the worker's advertised 7 GiB.
Replacing that request with 7 GiB admitted the unchanged suite; this run did not require a larger worker
or a separate canary pool.

## Reproduction and identity

```bash
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```

- Base: `2d8fad2b9d8178c23d7ff50fe5a2c512a4d9d83c`; the probe changed only the target's memory estimate
  and its comment. CPU remained 4 and free-disk request 50 GB. The three scenarios and assertions were unchanged.
- [Bazel invocation](https://app.buildbuddy.io/invocation/b0cb5f7d-ee2c-491c-94cd-3123fb685cef),
  parent `13ad1686-cf64-492c-a026-c7c6f0700925`; one uncached attempt, successful.
- Physical execution: `/uploads/7e226724-fe85-4941-bc7d-769c71d62356/blobs/7509008a9cf292567d17d7240fea6a097dd60a532fa4116d5e792d6b49a6a56a/596`;
  worker `d81a9661-db05-446a-a80d-bb4a4ba17367`.
- VMI `buildbuddy-executor/buildbuddy-kvm-worker-0`, UID `898a998f-8025-469a-ba23-e4dcd2ce593a`.
  The pool had one ready worker and the executor allowed one concurrent task. No action occupied it at baseline.
- The action platform confirmed `Pool=kvm-vm`, self-hosted Docker execution, `init-dockerd=false`,
  `recycle-runner=false`, 4 CPUs, 7 GiB estimated memory, and 50 GB estimated free disk.
  All three `vm-artifacts/host-qemu/.../shutdown-0-*.txt` artifacts contain `-accel kvm`.

## Result

| Observation                                    | Value                                               |
| ---------------------------------------------- | --------------------------------------------------- |
| Bazel test result                              | Passed, 276.857 s                                   |
| Bazel command wall time                        | 313.404 s                                           |
| Stack setup                                    | 118.55 s                                            |
| Concurrent scenario span                       | 135.59 s                                            |
| New action's whole Docker cgroup `memory.peak` | 5,914,898,432 B (5.51 GiB), including charged cache |
| Lowest sampled guest `MemAvailable`            | 1,882,676 KiB (1.80 GiB)                            |
| Worker `/proc/vmstat` OOM kills                | 0 before and after                                  |
| Worker swap                                    | No swap configured; swap-in/out counters remained 0 |
| Action cgroup OOM / OOM-kill events            | 0                                                   |

Claude turn, Codex turn, and Codex tool OOM scenarios all completed. The deliberate OOM occurred in the
inner guest's 256-MiB native service: tool exit 137, OOM-kill counter increased, follow-up exited 0.
It was not an OOM in the outer worker. This successful attempt does not repair the separately recorded
[Codex memory capability limitation](../../env/runtime/debug/2026_11_12_codex_memory_capability.md).

## Measurement limits

Guest sampling used the serial console and Linux `/proc/meminfo`, pressure and VM counters, plus Docker
cgroup v2 counters. The new action's parent `docker-<id>.scope` includes systemd/K3s descendants;
sampling only its `/init` child misses siblings. `memory.max=max`: 7 GiB is an admission estimate,
not an enforced container cap. Do not add cgroup charge and `MemAvailable`, since reclaimable cache overlaps.

Samples began at 19:26:29 UTC, after action startup, and included the VM workload and cleanup. A sampler
restart added parent-scope collection; its cumulative kernel peak was not reset. The executor's own
older cumulative peak is not attributed to this test. The 64 samples have a median interval of 4 s and
a 50-s gap from 19:27:23 to 19:28:13 UTC. See the [samples](vm_kvm_existing_pool_2026_11_12_samples.csv)
and [exact provenance and counters](vm_kvm_existing_pool_2026_11_12.json). The collector was stopped
after the action exited; the worker remained ready.

This is one successful run on a preexisting worker, not a fleet percentile, minimum RAM requirement,
concurrency result, or measured end-to-end CI saving. The target remains manual. The action's queued
timestamp is later than its worker-start timestamp, so that cross-clock interval cannot establish queue latency.
No pool configuration, worker size, required-CI routing, or Codex source changed.
