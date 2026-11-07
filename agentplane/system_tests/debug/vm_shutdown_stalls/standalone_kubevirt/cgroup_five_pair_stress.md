# Opt-in five-pair cgroup controller stress

The [compact two-pair pilot](cgroup_two_pair_pilot.md#compact-witness-pilot) proved that an ordinary `systemctl set-property --runtime` change on a live service in `pilot.slice` can enter one closed PID 1 `cgroup_subtree_control_write` → `cgroup_update_dfl_csses` → `percpu_down_write` → `rcu_sync_enter` call chain. Both `on` transitions finished cleanly. This arm repeats the controller-set transition five times in one fresh two-vCPU TCG guest to look for an RCU/readiness wedge under a bounded, explicitly perturbed workload. It does not write cgroup control files directly or start Agentplane Runtime.

`AGENTPLANE_TCG_VMI_CGROUP_STRESS=1` is opt-in, mutually exclusive with the two-pair pilot and synthetic slice probe, and requires the 1 Hz guest boot observer. It adds only `agentplane.cgroup-stress=1` to the guest kernel arguments. The policy remains on unless separately opted out, and debug kernel arguments remain off by default. A conditioned timer starts a separate service at 30 guest uptime seconds. It uses the pilot's live BusyBox resident in `pilot.slice` and performs exactly five `IOAccounting=yes/no` pairs through systemd. Before and after **every** property request it records guest uptime, resident PID/liveness, and both root and slice controller masks; it fails as unexercised unless both masks alternate as requested and the resident remains alive.

The first `on` step requires a clean per-CPU trace and one closed nested PID 1 call-chain witness, using the compact extractor. Immediately afterward the probe writes `tracing_on=0`, records the observed value, and does no further ftrace capture. The remaining nine requests are measured by property status and controller-set changes, so their specific kernel call paths are not individually proven. The guest script stops as unexercised before starting another request at 60 seconds of active work or after 75 seconds, leaving the existing 90-second service timeout, 12-second per-property timeout and 120-second driver milestone unchanged. The driver requires the witness, trace-off record, ten ordered successful results, and all alternating root/slice masks before it sends ACPI. A missing witness, mask mismatch, vanished resident, budget exhaustion or missing milestone stops the arm; budget exhaustion is an incomplete workload, not a kernel stall.

The successful two-pair probe took about 35 guest seconds: roughly 11 seconds of trace setup and four changes of 4.5–6.2 seconds. Six additional changes conservatively add about 37 seconds, within the 75-second active budget, while an outer fresh-cluster action may take roughly 9–10 minutes. This is a modest bounded stress arm, not a high-throughput cgroup fuzzer. Its writes target `pilot.slice/cgroup.subtree_control`, whereas the historical full guest's blocked PID 1 stack was in the **root** cgroup. A clean result cannot exclude the original full-stack failure, and any stall must be classified against the retained guest observer, host samples and exact VMI/Pod UIDs.

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --nocache_test_results --flaky_test_attempts=1 --runs_per_test=1 \
  --test_env=AGENTPLANE_TCG_VMI_CYCLES=1 \
  --test_env=AGENTPLANE_TCG_VMI_BOOT_RCU_OBSERVER=1 \
  --test_env=AGENTPLANE_TCG_VMI_CGROUP_STRESS=1 \
  --test_env=AGENTPLANE_TCG_VMI_CGROUP_PILOT=0 \
  --test_env=AGENTPLANE_TCG_VMI_SYNTHETIC_SLICE_PROBE=0 \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=0 \
  --test_env=AGENTPLANE_TCG_VMI_DEBUG_KERNEL_ARGS=0
```

The retained per-cycle output includes `cgroup-stress-console.txt`, guest and passive host boot observations, exact VMI and launcher Pod UIDs, ACPI/shutdown proof, and UID-checked namespace/PVC/PV cleanup. Raw outputs stay in BuildBuddy and task-local scratch; this note records only bounded, sanitized findings.
