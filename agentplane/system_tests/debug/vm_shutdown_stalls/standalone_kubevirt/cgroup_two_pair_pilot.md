# Opt-in two-pair cgroup controller pilot

The [three-guest synthetic slice arm](synthetic_slice.md#three-fresh-guests-in-one-k3s-cluster) showed one root `io` controller transition per fresh guest, but root `io` stayed enabled after the payload exited. Repeating service starts in that same guest might therefore leave the root controller mask unchanged. This separate, bounded pilot asks systemd to toggle `IOAccounting` on a live service in a top-level `pilot.slice`; it never writes `cgroup.subtree_control` directly. The goal is to measure whether repeated systemd property changes reach the target cgroup update path, not to reproduce or repair the full-suite stall.

`AGENTPLANE_TCG_VMI_CGROUP_PILOT=1` adds only `agentplane.cgroup-pilot=1` to the guest kernel arguments. It requires `AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1` and cannot run with the earlier synthetic slice arm. A conditioned timer starts the pilot after 30 guest uptime seconds. The probe starts a bounded BusyBox resident process in `pilot.slice`, verifies its PID remains in the service cgroup, then makes exactly four `systemctl set-property --runtime` requests: `IOAccounting=yes`, `no`, `yes`, `no`. Before and after each request it records guest uptime, root and slice `cgroup.subtree_control`, resident PID and liveness. It fails as unexercised if a request fails, the resident disappears, or the slice's observed `io` state differs from the requested value. The driver requires the four ordered result records and a success record before sending ACPI.

The guest also tries an explicitly opt-in, PID 1 `function_graph` trace rooted at `cgroup_subtree_control_write`. The graph can show nested calls to `cgroup_update_dfl_csses`, `percpu_down_write`, and `rcu_sync_enter` if those functions are visible. The probe records trace availability, per-request hit counts and one bounded first line per symbol. The trace ring is capped at 64 KiB and cleared before each property request. Tracing changes global tracefs settings inside this disposable guest; the probe first requires an idle tracer and no preexisting function, graph, or PID filter, and restores its tracer, filter, PID and buffer settings at exit. Unavailable tracing is reported; state transitions alone do not prove a particular kernel function or wait. Even a PID 1 graph hit in the same window does not prove that the property request caused it or that `cgroup_attach_lock` waited. The 1 Hz boot observer and ftrace are deliberate perturbations, and Runtime remains masked in this reduced two-vCPU TCG guest. These property changes target `pilot.slice/cgroup.subtree_control`, whereas the historical PID 1 stack was in a write to the **root** `cgroup.subtree_control`.

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
