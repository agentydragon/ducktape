# Opt-in synthetic `agentplane.slice` activation

The full-suite pre-Ready failure in [invocation `c4d8ad7d`](https://app.buildbuddy.io/invocation/c4d8ad7d-afbd-4d14-8a93-f54b968fce8f) sampled guest PID 1 in `cgroup_subtree_control_write` and `synchronize_rcu_normal`. The direct-VMI harness masks `agentplane-environment-runtime.service`, the normal service in `agentplane.slice`, so its default boot does not establish whether that slice's activation contributes to the failure. This separate manual arm activates one short synthetic service in that slice; it does not start Runtime or reproduce the full guest workload.

`AGENTPLANE_TCG_VMI_SYNTHETIC_SLICE_PROBE=1` adds only `agentplane.synthetic-slice-probe=1` to the guest boot arguments. A conditioned timer starts the probe at guest uptime 30 seconds. The probe records uptime, root and slice `cgroup.controllers`/`cgroup.subtree_control`, slice cgroup presence, and payload state before activation, while the payload is active, and after it stops. The payload is a bounded eight-second BusyBox sleep with `Slice=agentplane.slice`; the existing `agentplane.slice` unit requests CPU, memory, tasks, and IO accounting. The driver waits up to 90 seconds for ordered baseline, active, after, and exact success receipts before requesting VMI deletion and ACPI. Missing receipts fail the test rather than silently skipping the exercise. Each guest record is capped at 1024 bytes and emitted through one write to the serial console.

The arm requires `AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1`, which independently samples PID 1, per-CPU timer and RCU activity at roughly 1 Hz and collects bounded host QEMU `/proc` states. It is an explicit guest-observer perturbation. No debug kernel arguments or policy-off behavior are implied by the synthetic flag.

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --nocache_test_results --flaky_test_attempts=1 --runs_per_test=1 \
  --test_env=AGENTPLANE_TCG_VMI_CYCLES=1 \
  --test_env=AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1 \
  --test_env=AGENTPLANE_TCG_VMI_SYNTHETIC_SLICE_PROBE=1 \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=0 \
  --test_env=AGENTPLANE_TCG_VMI_DEBUG_KERNEL_ARGS=0
```

The output includes `run-config.json`, per-cycle `synthetic-slice-console.txt`, serial/boot observer records, host QEMU samples, VMI and Pod identities, shutdown proof, and UID-checked cleanup evidence. One-second sampling cannot prove every short kernel write occurred or exclude an intervening transient RCU stall. Changes to root controller sets show state at the three sampled phases, not which exact kernel call wrote them. A clean direct-VMI result would remain a reduced, Runtime-masked observation rather than a causal test of the original full-suite stall.

## One uncached cycle

The [single action](https://app.buildbuddy.io/invocation/f94b8e78-4978-4d44-90a7-3591fb4c4885) passed with one attempt and `doNotCache=true` on code head `09188c9899e28b2a01aeed1c7ad5d7c3366ad762`. The runner fetched base `c5bd152185777002c503430d411c45c52353816d` and cleanly applied its seven-file patch (SHA-256 `1548c9a6fd2be82498e114dd84fc09ce046cfe9fef831584542690a0ccfdf8ac`). The retained `run-config.json` confirms one cycle, policy on, the boot observer and synthetic slice probe on, and debug arguments off. The VMI UID was `5e5766e2-2d33-42d5-b52e-425d60694cd7`; its launcher Pod UID was `8fdc627d-8275-4cf4-938b-55724d9cc56f`.

The exact guest serial records show:

| Guest uptime | Phase    | Root `cgroup.subtree_control` | Slice cgroup | Slice `cgroup.subtree_control` | Payload  |
| ------------ | -------- | ----------------------------- | ------------ | ------------------------------ | -------- |
| 31.15 s      | baseline | `memory,pids`                 | absent       | absent                         | inactive |
| 31.56 s      | active   | `io,memory,pids`              | present      | `memory,pids`                  | active   |
| 39.81 s      | after    | `io,memory,pids`              | present      | empty                          | inactive |

The exact `AGENTPLANE_SYNTHETIC_SLICE_SUCCESS` receipt followed the after record. The new `io` entry at the root and appearance of the slice establish that this reduced arm exercised cgroup controller activation. They do not identify the kernel writer or prove when the brief write occurred. The 1 Hz observer recorded 28 paired samples with no errors. Around the transition, samples 19, 20 and 27 saw PID 1 in `do_epoll_wait`; both CPUs' TIMER and RCU softirq counters advanced. It did not sample PID 1 in `cgroup_subtree_control_write` or `synchronize_rcu_normal`.

The host's passive `/proc` observer wrote 71 samples, 50 with the exact Pod-UID-matched QEMU process, with no incomplete or truncated process scan, zero capture errors, and clean teardown. Its QMP mode was `disabled-passive-baseline`; the separate pre-shutdown `query-kvm` reported `enabled=false`. The guest emitted four exact ACPI request receipts, ordered shutdown proof with `errors: []`, and `reboot: Power down`. The UID-correlated virt-handler reported `Shutoff reason Shutdown`, and cleanup reached `sandbox_deleted`. The single clean cycle neither reproduces the distinct pre-Ready wedge nor establishes its absence under full Runtime load.

The focused synthetic-marker test [passed](https://app.buildbuddy.io/invocation/07902bef-9167-4b68-a9e1-2f107c2dffc4) with configured lint aspects, the [guest root-image build passed](https://app.buildbuddy.io/invocation/70ae2615-84f6-4d8c-bb99-558e217052ea), and changed-file pre-commit passed. Raw guest and host outputs remain in BuildBuddy and task-local scratch; this note adds no raw log bundle to the repository.
