# Opt-in two-pair cgroup controller pilot

The [three-guest synthetic slice arm](synthetic_slice.md#three-fresh-guests-in-one-k3s-cluster) showed one root `io` controller transition per fresh guest, but root `io` stayed enabled after the payload exited. Repeating service starts in that same guest might therefore leave the root controller mask unchanged. This separate, bounded pilot asks systemd to toggle `IOAccounting` on a live service in a top-level `pilot.slice`; it never writes `cgroup.subtree_control` directly. The goal is to measure whether repeated systemd property changes reach the target cgroup update path, not to reproduce or repair the full-suite stall.

`AGENTPLANE_TCG_VMI_CGROUP_PILOT=1` adds only `agentplane.cgroup-pilot=1` to the guest kernel arguments. It requires `AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1` and cannot run with the earlier synthetic slice arm. A conditioned timer starts the pilot after 30 guest uptime seconds. The probe starts a bounded BusyBox resident process in `pilot.slice`, verifies its PID remains in the service cgroup, then makes exactly four `systemctl set-property --runtime` requests: `IOAccounting=yes`, `no`, `yes`, `no`. Before and after each request it records guest uptime, root and slice `cgroup.subtree_control`, resident PID and liveness. It fails as unexercised if a request fails, the resident disappears, or the slice's observed `io` state differs from the requested value. The driver requires the four ordered result records and a success record before sending ACPI.

The guest also tries an explicitly opt-in, PID 1 `function_graph` trace rooted at `cgroup_subtree_control_write`. The graph can show nested calls to `cgroup_update_dfl_csses`, `percpu_down_write`, and `rcu_sync_enter` if those functions are visible. The probe records trace availability and per-request hit counts. The capture requests a 1 MiB per-CPU ring, records its actual size and each CPU's overrun/drop counters before and after each step, and bounds an in-guest scan to 20,000 lines. It emits at most five compact witness records per `yes` step: one closed root with the nested path in order, plus the root, update, lock and RCU lines. A missing root closure, overflow/drop, counter reset or unavailable counters is explicitly unproven; it does not dump the trace to serial. It clears the ring before each property request. Tracing changes global tracefs settings inside this disposable guest; the probe first requires an idle tracer and no preexisting function, graph, or PID filter, and restores its tracer, filter, PID and buffer settings at exit. State transitions alone do not prove a particular kernel function or wait. Even a complete PID 1 call path in the same window does not prove that the property request caused it or that `cgroup_attach_lock` waited. The 1 Hz boot observer and ftrace are deliberate perturbations, and Runtime remains masked in this reduced two-vCPU TCG guest. These property changes target `pilot.slice/cgroup.subtree_control`, whereas the historical PID 1 stack was in a write to the **root** `cgroup.subtree_control`.

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --nocache_test_results --flaky_test_attempts=1 --runs_per_test=1 \
  --test_env=AGENTPLANE_TCG_VMI_CYCLES=1 \
  --test_env=AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1 \
  --test_env=AGENTPLANE_TCG_VMI_CGROUP_PILOT=1 \
  --test_env=AGENTPLANE_TCG_VMI_SYNTHETIC_SLICE_PROBE=0 \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=0 \
  --test_env=AGENTPLANE_TCG_VMI_DEBUG_KERNEL_ARGS=0
```

The retained per-cycle output includes `cgroup-pilot-console.txt`, guest observer and passive host QEMU samples, exact VMI and launcher Pod UIDs, ACPI and shutdown proof, and UID-checked namespace cleanup. A failed or unexercised pilot stops this line of experimentation before any higher iteration workload. Raw logs remain in BuildBuddy and task-local scratch rather than this repository.

## First bounded pilot and trace limit

The [first uncached action](https://app.buildbuddy.io/invocation/f6ecddb3-31ab-4109-88c1-52904fdba667) passed one test attempt from exact pushed head `755183933ab54a267e01c29fa850c1dfe04a89ab`; the runner fetched that SHA without a patch. Policy was on, the boot observer and pilot were on, the earlier synthetic arm and debug arguments were off. VMI UID `4184892d-2dd3-40c3-b77f-22490d00dfc5` had launcher Pod UID `13b944df-87fd-488e-b3d7-ffce516d49cd`. The resident PID was `543` and remained in its service cgroup throughout.

| Step       | Guest uptime before → after | Root and slice mask before → after | Request | Graph counts: update / lock / RCU |
| ---------- | --------------------------- | ---------------------------------- | ------- | --------------------------------- |
| first on   | 36.54 → 42.97 s             | `memory,pids` → `io,memory,pids`   | success | 2 / 1 / 1                         |
| first off  | 43.53 → 50.64 s             | `io,memory,pids` → `memory,pids`   | success | 0 / 0 / 0                         |
| second on  | 51.31 → 57.84 s             | `memory,pids` → `io,memory,pids`   | success | 2 / 1 / 1                         |
| second off | 58.40 → 65.35 s             | `io,memory,pids` → `memory,pids`   | success | 0 / 0 / 0                         |

This demonstrates repeatable observed controller-set transitions on a live descendant task. It does **not** establish a complete nested function path: the 64 KiB trace ring produced 1,003–1,117 lines per step, and the first 48 retained lines began mid-tree. Counts from the same interval cannot prove that the functions were in the same call tree. The test still completed ordinary ACPI shutdown with four exact guest request receipts, ordered proof with `errors: []`, `reboot: Power down`, UID-correlated virt-handler `Shutoff reason Shutdown`, and `sandbox_deleted`. QMP reported KVM disabled.

## Bulk trace capture failed before ACPI

The [second uncached action](https://app.buildbuddy.io/invocation/49440099-31cc-4e36-974e-1434b1547c32) from exact pushed head `a38d656c6ece1cc27957ddd43a4f77d9af07dbc6` failed the 120-second pilot milestone wait. Its requested 1 MiB ring resolved to 1,027 KiB per CPU, and the first `on` step reported zero CPU0/CPU1 overrun, commit-overrun, or dropped events. But that step had 8,155 trace lines; emitting them separately to serial reached only line 766 of the 1,536-line cap before the milestone deadline. No `RESULT`, success, or ACPI request occurred. The boot observer kept sampling to about 146 guest uptime seconds, with PID 1 in `do_epoll_wait` and advancing timer/RCU counters. This is an instrumentation-throughput failure, not evidence of the cgroup/RCU wedge or the late post-ACPI shutdown stall. The test's UID-checked cleanup returned without a secondary error; the retained artifacts do not include a separate post-delete namespace query. The compact in-guest extractor above replaces that bulk serial output without increasing the deadline. Raw logs remain linked through BuildBuddy and task-local scratch, not this repository.

## Compact witness pilot

The [third and final bounded pilot](https://app.buildbuddy.io/invocation/f3e1e18a-d1fa-42e8-a1a2-5f694ab1dbf4) passed one uncached test attempt from exact source `9a65c5eb24d48054bd708b6afeafc7d91460aca4`, the head of [the compact-extractor follow-up](https://github.com/agentydragon/ducktape/pull/14772). The remote runner fetched that SHA without a patch. Policy was on, the 1 Hz boot observer and pilot were on, and debug kernel arguments were off. The VMI UID was `1772af8e-7d48-48fd-8641-ef6668588094`, launcher Pod UID `74f75df7-3cfb-4094-a163-2827118104ff`, and persistent resident PID `497` stayed in its service cgroup. The test took 505.2 seconds; the cluster setup dominated elapsed time.

| Step       | Guest uptime before → after | Root and slice mask before → after | Property request | Trace path proof                                         |
| ---------- | --------------------------- | ---------------------------------- | ---------------- | -------------------------------------------------------- |
| first on   | 50.19 → 56.38 s             | `memory,pids` → `io,memory,pids`   | success          | closed root 372–4233; update 2841 → lock 2900 → RCU 2902 |
| first off  | 57.28 → 62.00 s             | `io,memory,pids` → `memory,pids`   | success          | not extracted on `off` step                              |
| second on  | 62.86 → 68.63 s             | `memory,pids` → `io,memory,pids`   | success          | closed root 36–3745; update 2422 → lock 2481 → RCU 2483  |
| second off | 69.54 → 74.09 s             | `io,memory,pids` → `memory,pids`   | success          | not extracted on `off` step                              |

Both `on` windows had zero overrun, commit-overrun, and dropped-event counters before and after on both vCPUs, and fewer than the 20,000-line scan limit (5,047 and 4,685 lines). Each emitted witness identifies one **closed** PID 1 `cgroup_subtree_control_write` root containing `cgroup_update_dfl_csses` → `percpu_down_write` → `rcu_sync_enter` in nesting order; the serial records include the four indented function lines and root closure line number. This establishes that the synthetic top-level `pilot.slice` operation exercises the targeted kernel call chain at least twice. It does not show a blocked wait, and the historical full guest stalled while writing the **root** cgroup, so the reproduction remains narrower than that failure.

After the pilot success marker, the guest received five exact ACPI request receipts, emitted ordered `/state/workspaces` and `/state` unmount proof with `errors: []`, and logged `reboot: Power down` at guest uptime 87.685923 s. The UID-correlated virt-handler logged `Shutoff reason Shutdown`; there was no retained forced-destroy marker. The VMI reached `sandbox_deleted`, and the test's UID-checked cleanup verified deletion of its ClusterPolicy, PVC, namespace, and state PV before passing. QMP reported KVM disabled. Raw trace/serial/cluster artifacts remain in BuildBuddy and task-local scratch. No stall was reproduced, and this pilot does not justify a causal claim about the original late-shutdown failure.
