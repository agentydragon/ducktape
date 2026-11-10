# Standalone KubeVirt/TCG shutdown diagnostic

`//agentplane/system_tests/vm:test_tcg_vmi_shutdown` boots one real Agentplane CI guest in an isolated k3s/KubeVirt test cluster and requests normal VMI deletion. It does not start the Agentplane application stack, Sandbox controller, session machinery, or service runtime. The test attaches a disposable workspace disk, a PVC-backed `state` virtiofs volume, and placeholder config/launch/trust disks. It verifies QEMU software emulation from the libvirt domain, QEMU command line, and QMP `query-kvm` response.

The guest shutdown proof markers and bounded proof-helper sampling are described in
[`proof_progress.md`](proof_progress.md), including the uncached one-guest validation and
its diagnostic limits.
The opt-in blocked-task SysRq experiment and its control results are in
[`sysrq_capture.md`](sysrq_capture.md).
The separate [synthetic slice probe](synthetic_slice.md) tests whether activating a
short service in `agentplane.slice` exercises the boot-time cgroup controller path
without starting Runtime.

The VMI is direct, so this is not a full-stack reproduction of the Sandbox controller or production sidecar topology. The default arm installs only the production-derived `keep-virtiofs-through-guest-shutdown` ClusterPolicy rule, scoped to the test namespace and direct VMI. The opt-in policy-off arm skips that rule only for this test. It does not modify cluster-wide KubeVirt settings. The test retains serial, launcher, virtiofs, containerd, lifecycle, QEMU, VMI, policy, and cluster evidence and deletes only its UID-owned namespace and policy.

## Manual repeated cycles

The target can reuse one k3s/KubeVirt installation for up to three sequential VMI create/delete cycles. Each cycle has a fresh namespace, PVC, scoped policy, VMI UID, launcher Pod UID, and `cycles/NNN/` artifact directory; `cycle-identity.json` records the correlation. The test stops at the first cycle without ordered guest shutdown and correlated host shutdown evidence, then performs UID-checked cleanup. Its normal deletion path is graceful; any force deletion is confined to cleanup after the measured cycle has failed. The default is one cycle with policy on and production-like kernel log arguments.

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual --nocache_test_results --flaky_test_attempts=1 \
  --test_env=AGENTPLANE_TCG_VMI_CYCLES=3
```

After a clean baseline, a separate opt-in arm can add the exact historical debug arguments (`systemd.log_level=debug systemd.log_target=console loglevel=8`) with `--test_env=AGENTPLANE_TCG_VMI_DEBUG_KERNEL_ARGS=1`. Policy-off remains a separate explicit `--test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=1` arm. The output `run-config.json` records these choices. A debug-arm stall could be affected by console verbosity; none of these direct-VMI arms reproduces the full CI topology.

An uncached [two-cycle policy-on baseline](https://app.buildbuddy.io/invocation/83effe66-210e-465c-ac90-25c20125a390) passed in one 331.5-second test action. The distinct VMI UIDs were `a275228e-9347-4723-be8b-7168e26278b4` and `db74eceb-61be-4023-bf04-09c07b819f5f`; the corresponding guest ACPI request-marker counts were 3 and 4. Both retained shutdown diagnostics classify `orderly_guest_powerdown`, with ordered guest completion/powerdown markers and VMI-UID-correlated virt-handler `Shutoff reason Shutdown` events; neither has a correlated grace-expiry, launcher-kill, or destroyed-domain marker. The passing test also verifies UID-checked namespace, policy, PVC, and local PV cleanup after each cycle. Two clean cycles bound only this manual harness run; they do not establish a flake rate or explain the historical CI stalls.

A separate [three-cycle debug-argument arm](repeated_debug_args/README.md) used the same policy-on manual harness with the historical console verbosity enabled. Its raw per-cycle evidence is retained separately from the production-like baseline.

## Policy A/B outcome

The exact target ran three times in each arm, with test-result caching disabled and one allowed attempt. The arm commands were:

```bash
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=0 \
  --runs_per_test=3 --flaky_test_attempts=1 --nocache_test_results

bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual \
  --runs_per_test=3 --flaky_test_attempts=1 --nocache_test_results \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=1
```

- Policy on: [BuildBuddy test invocation `d2d5853d`](https://app.buildbuddy.io/invocation/d2d5853d-62d8-43e2-a56b-960dc6f90bb8) (runner wrapper: [c0d4a2d1](https://app.buildbuddy.io/invocation/c0d4a2d1-9523-436e-a787-d2d74b19c77c)), 3/3 passed. Admission evidence shows `virtiofs-state` as an `initContainer` with `restartPolicy: Always`. All three guests emitted the shutdown proof and powered down.
- Policy off: [BuildBuddy test invocation `6c6dde43`](https://app.buildbuddy.io/invocation/6c6dde43-5fd9-444a-a8e6-263a7c4c3a04) (runner wrapper: [4f90f797](https://app.buildbuddy.io/invocation/4f90f797-ca4c-486f-a998-308d8e876501)), 3/3 failed with `GuestShutdownError` because shutdown proof was missing. Admission evidence shows `virtiofs-state` as an ordinary container. The guest ACPI request-marker counts were 3, 3, and 4; the test did not observe QMP `system_powerdown` command counts. All three serial traces contain no shutdown proof or `reboot: Power down`.

In the policy-off runs, the `virtiofs-state` container received containerd `StopContainer`/SIGTERM about 0.2–0.3 seconds after VMI deletion began, then exited with status 1. The serial snapshots show `sd-sync` blocked through `fuse_sync_fs`/`ksys_sync` and unmount/journal helpers blocked through FUSE request paths (`request_wait_answer`, `__fuse_simple_request`, `fuse_do_getattr`/`fuse_permission`). KubeVirt later stopped QEMU after its shutdown grace. This is consistent with shutdown work waiting on FUSE as the sidecar is stopped, but the delayed host snapshots do not establish whether virtiofsd had already stopped servicing requests before SIGTERM. The KubeVirt Pod's `Running` status is not used as daemon-liveness proof; containerd's per-container stop/exit record is retained.

These arms were run sequentially, not as overlapping pairs: policy-on executions started 00:26:54.291–00:26:56.418 UTC and finished by 00:32:51.441; policy-off executions started 00:33:12.571–00:33:12.586 UTC. Runs within each arm overlapped. Corresponding run numbers reused the same RBE worker UUID, so warm-worker and time-order effects remain possible. This small sample is evidence about this direct-VMI harness, not proof of the original CI failure's root cause or equivalence to production topology.

## Source and input identity

The A/B used the same test source (SHA-256 `6d5597908d6187f724dcb96a3cf504af5aeac6ffd9e618730bd8ca2e93312191`) and the same temporary guest child-snapshot observer patch as [PR #14691](https://github.com/agentydragon/ducktape/pull/14691), patch SHA-256 `8f144442022f1a43c476bf83b866631b3299460c1580ff0c7574ec77a74592d7`. The observer scans direct PID 1 children and records bounded `cmdline`, `wchan`, `syscall`, and `stack` only for shutdown helpers; it is an additional observer relative to the original arm. It sampled at first observation and every 10 seconds, with bounded process/field sizes. That instrumentation was identical in both policy arms.

Paired policy-on/off REAPI actions had identical input roots, identical guest/VM image inputs, and the same RBE container image (`sha256:c73af98850208e2a898120f9992f4cb8c320c488bc3f3c8607cc06ace96be3c5`). The three paired input-root digests were:

| Pair | Input-root digest                                                  |
| ---- | ------------------------------------------------------------------ |
| 1    | `7e040063a31eef0c5e1b62230453fbb81ce1f4b60c8aee87e69151ada05f43a1` |
| 2    | `b70b1f4343e46a35c14b4cea59d5c99a5b9fc685f0adbfa45df34f607fe2c908` |
| 3    | `3e188644f5918409bbb76ffe3ebca0cc650d046f85f2bcc6ddec9ef23e27a770` |

The Command digests differ as expected because the opt-in policy flag differs and the `runs_per_test` index is part of the command. All six actions set `doNotCache=true`; the Bazel test invocation allowed one attempt, with no flaky retry. `ab_source.patch.gz` records the standalone test's code changes from base `be1b2d70506b2259129900392a0e9c11b1e63ef8`, excluding the temporary guest observer. The observer patch is identified above and was not added to this standalone target change.

## Retained evidence

- [`ab_policy_on/raw_artifacts.tar.gz`](ab_policy_on/raw_artifacts.tar.gz) contains the three policy-on test output trees.
- [`ab_policy_off/raw_artifacts.tar.gz`](ab_policy_off/raw_artifacts.tar.gz) contains the three policy-off test output trees.
- [`ab_source.patch.gz`](ab_source.patch.gz) is the standalone implementation diff from the recorded base.
- SHA-256 sums for these files are in [`SHA256SUMS`](SHA256SUMS).

The tarballs retain raw guest serial and child snapshots, host QEMU samples, launcher and virtiofs logs, containerd lifecycle events, policy admission evidence, VMI/domain/QMP state, test logs, and per-run input image maps. They do not contain host virtiofsd `/proc` thread snapshots; those are being added in a separate, explicitly instrumented follow-up arm. The initial standalone smoke and original host evidence are retained in the sibling `original_806989c8.tar.gz` and `policy_corrected_44599a13.tar.gz` bundles.
