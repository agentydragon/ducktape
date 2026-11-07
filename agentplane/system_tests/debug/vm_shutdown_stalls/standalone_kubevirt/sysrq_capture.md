# Opt-in shutdown SysRq capture

On 2026-11-07 we tested a one-shot guest SysRq `w` request during direct VMI
shutdown. `AGENTPLANE_TCG_VMI_SHUTDOWN_SYSRQ=1` adds
`agentplane.shutdown-sysrq=1` to that guest's kernel arguments. After the
watchdog receives ACPI notification, a separate worker waits 12 guest seconds.
If the shutdown proof's mount checks have already completed, it skips the dump;
otherwise it writes `w` to `/proc/sysrq-trigger` and reports its attempt and
return status on the serial console. This opt-in does not enable the diagnostic
for ordinary CI guests. The proof check and request are not atomic; a proof
can race with a dump already beginning.

We used two concurrent uncached actions with one attempt, both based on
`fad147c598f266fe02d0528457be696e0c661eeb` plus the experimental patch in
commit `f3134abf3ddc2666db0f4023ad7f4128daa0c1c5`. A `git diff` of the patch
before committing had SHA-256
`32b95ba2bc28638cfc456059a1f95db1bbb539a86463a7304d773da308963dd9`.
Each run used a disposable VMI and a fresh isolated k3s cluster.

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual --nocache_test_results --flaky_test_attempts=1 \
  --test_env=AGENTPLANE_TCG_VMI_SHUTDOWN_SYSRQ=1 \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=0

# Intentionally stops the virtiofs sidecar early: the test is expected to fail.
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual --nocache_test_results --flaky_test_attempts=1 \
  --test_env=AGENTPLANE_TCG_VMI_SHUTDOWN_SYSRQ=1 \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=1
```

## Results

- **Policy on:** [test invocation `0cf051b6-a6c9-437b-86f1-f8cc44d16a80`](https://app.buildbuddy.io/invocation/0cf051b6-a6c9-437b-86f1-f8cc44d16a80),
  runner `6770d377-e316-4a0d-9834-4be649161b87`, passed. VMI UID
  `6a628041-cc4a-40c9-83bc-86c4ce70da6c` emitted `AGENTPLANE_SYSRQ_ARMED`,
  shutdown proof, and `reboot: Power down`; the retained serial log contains no
  SysRq begin marker. The guest powered down before the delayed worker emitted
  a skip marker, so this run does not independently validate the skip branch.
- **Policy off:** [test invocation `7417fe17-b471-4acc-95e9-e9fa6ded8dc0`](https://app.buildbuddy.io/invocation/7417fe17-b471-4acc-95e9-e9fa6ded8dc0),
  runner `e1c74657-710e-4356-a4e8-843ebf123726`, failed with missing shutdown
  proof, as intended. VMI UID `0cfe5ac3-1e62-494a-a292-1d1250d6a77a` had no
  proof or guest powerdown, and the UID-correlated host reported grace expiry
  and VMI kill at 19:57:53 UTC. Its retained serial log includes
  `AGENTPLANE_SYSRQ_BEGIN command=w`, kernel `sysrq: Show Blocked State`, four
  `sd-sync` tasks in D state with `fuse_sync_fs` and `ksys_sync` frames, and
  `AGENTPLANE_SYSRQ_END command=w status=0`.

After extracting the worker to a separately testable script, a second
[policy-off invocation `94ba1919-5747-4445-9163-e878d6904cc6`](https://app.buildbuddy.io/invocation/94ba1919-5747-4445-9163-e878d6904cc6)
again failed the shutdown assertion, as expected. VMI UID
`3d03322d-2888-4958-b53c-27bead277a93` armed the packaged script and retained
`sysrq: Show Blocked State`, three `fuse_sync_fs` task stacks, and the
successful end marker. The [focused Bazel invocation
`442d4d44-1a6d-4f98-bbbd-b40d6e385d06`](https://app.buildbuddy.io/invocation/442d4d44-1a6d-4f98-bbbd-b40d6e385d06)
passed both the full-suite template opt-in test and a worker test that exercises
the proof-present skip and proof-absent request using a temporary trigger file.
The latter does not write to the host kernel or replace the guest tests.

The timestamped console placed the arm at 19:57:23.661 UTC, begin at
19:57:35.686 UTC, and successful return at 19:57:35.813 UTC. The raw serial
interval from begin to just before the end marker was 7,099 bytes. These are
observations for one small guest, not hard bounds. The runtime SysRq mask was
`438` (`0x1b6`); the privileged proc write succeeded despite the masked
serial-key dump bit. Existing watchdog output also contained FUSE waits.
Per-cycle `cycle-identity.json`, `vm-artifacts/shutdown-diagnostic.json`, and
the CRI console and raw serial artifacts are available from the linked
BuildBuddy invocations. Raw logs are not copied into this report.

This confirms that SysRq worked through the guest proc interface in this
deliberately induced FUSE stall. It does not reproduce or explain the original
CI stalls. `w` omits tasks outside uninterruptible sleep, may perturb shutdown,
and cannot be relied on if the guest worker, kernel, or serial path stops
progressing. There is no demonstrated host serial-BREAK fallback. Neither this
single normal run nor the induced stall establishes a safe production threshold
or rules out false triggering on a slow but healthy shutdown.

## Healthy full-suite timing baseline

We also examined the CRI console timestamps for all 15 guests in the earlier
[five-run full-suite boot-observer batch](../full_suite_five_run_boot_rcu_observer.md).
These ran without this SysRq experiment and all reached proof and powerdown.
The time between the first `AGENTPLANE_GUEST_SHUTDOWN_REQUESTED` receipt and
the first `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE` receipt ranged from 3.200 to
15.881 wall-clock seconds; one of the 15 was at least 12 seconds. From the
first `AGENTPLANE_SHUTDOWN_SNAPSHOT_BEGIN` receipt to proof, the range was
3.137 to 15.294 seconds, again with one at least 12 seconds. The slow guest was
run 1's `vm-codex-oom` (launcher Pod UID
`fc2c09ef-4456-428d-bfdd-5421300e6eaf`).

These are console receipt intervals, not guest sleep durations; buffering and
guest clock behavior can differ. The concurrent batch is small and was run with
the boot observer enabled. The result warns that a 12-second diagnostic could
fire during a healthy full-suite shutdown, but does not establish a false-dump
rate or a safe threshold. A later trigger leaves less time before the host's
grace limit and could miss a dump if the guest clock runs slowly.

## Full-suite opt-in control

The full VM suite accepts `AGENTPLANE_VM_SHUTDOWN_SYSRQ=1` to add the argument
to each VM template. One uncached [full-suite invocation
`0d35e9ac-8036-4424-8935-dbc4409dde96`](https://app.buildbuddy.io/invocation/0d35e9ac-8036-4424-8935-dbc4409dde96)
passed with the opt-in. All three CRI guest consoles contained the boot
argument and one arm marker, followed by proof and powerdown; none contained a
SysRq begin marker. Request-to-proof console receipt intervals were 4.539,
4.655, and 7.496 seconds. The VMI UIDs were
`3fb6b484-94c5-4bca-9135-879e2ee7ae3b`,
`090e77ce-2500-43f2-97b6-e76eea9c8234`, and
`1068d0ce-f5e3-4402-a578-aeeca7c1945c`. These observations show that the
experimental flag reached and armed all three guests in this action; one clean
action neither validates a default trigger delay nor reproduces the flake.

A separate [four-run full-suite batch
`021e7522-13f6-40a0-ab14-393daf41950d`](https://app.buildbuddy.io/invocation/021e7522-13f6-40a0-ab14-393daf41950d)
used `--runs_per_test=4 --flaky_test_attempts=1 --nocache_test_results --jobs=4`
with the same opt-in. All four runs passed. All 12 guest CRI consoles had the
kernel argument and exactly one arm marker, followed by proof and powerdown;
request-to-proof receipt intervals ranged from 4.093 to 13.939 seconds.

One successful guest did emit a dump: run 2 `vm-2`, VMI UID
`a717432a-6045-4f1c-b5a3-e6aa8edc883a`, launcher Pod UID
`14415e18-cfe1-4c7a-86a9-8b9735aab0e6`. The CRI console timestamps place
arming at 20:18:54.390 UTC, SysRq begin at 20:19:06.422, successful return
at 20:19:06.583, proof at 20:19:08.083, and powerdown at 20:19:09.679. The
dump included `kworker/0:2` in `inode_switch_wbs_work_fn` waiting through
`synchronize_rcu_normal` and `__wait_rcu_gp`. That single snapshot was followed
by successful shutdown; it does not establish a stalled grace period or a link
to the distinct pre-Ready PID 1 cgroup/RCU wait. It demonstrates that a
12-second trigger can fire during an ultimately successful shutdown, and that
an RCU wait in one snapshot is not itself proof of the original flake's cause.
The run is still a perturbing experiment, not evidence that SysRq had no timing
effect. No shutdown stall reproduced in this four-run batch.
