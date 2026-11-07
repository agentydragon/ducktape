# Opt-in shutdown SysRq capture

On 2026-11-07 we tested a one-shot guest SysRq `w` request during direct VMI
shutdown. The experiment below used a 12-second delay; the later
[CI-default experiment](sysrq_default_ci.md) changes that delay and full-suite
configuration. `AGENTPLANE_TCG_VMI_SHUTDOWN_SYSRQ=1` adds
`agentplane.shutdown-sysrq=1` to that guest's kernel arguments. After the
watchdog receives ACPI notification, a separate worker waits 12 guest seconds.
If the shutdown proof's mount checks have already completed, it skips the dump;
otherwise it writes `w` to `/proc/sysrq-trigger` and reports its attempt and
return status on the serial console. At the time of this experiment, the
direct-VMI opt-in did not enable the diagnostic for ordinary CI guests. The proof check and request are not atomic; a proof
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
