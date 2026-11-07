# Opt-in boot RCU observer for full-stack VM tests

The full-stack VM system test can opt into read-only boot samples without
changing its default guest behavior. Run one test action with:

```sh
bbr test //agentplane/system_tests/suites:test_vm \
  --test_env=AGENTPLANE_VM_BOOT_RCU_OBSERVER=1
```

The observer is disabled unless the environment variable is exactly `1`. In
that mode, only the test's rendered VM `SandboxTemplate` kernel arguments gain
`agentplane.boot-rcu-observe=1` and
`agentplane.boot-rcu-observe-window=360`. The guest service is guarded by the
first argument; it samples read-only `/proc` boot state to serial, at most once
per guest second for 360 samples. The service has a 420-second guest runtime
limit. The normal guest observer remains unchanged when the test env is absent.

The host collector starts with `VmRuntime.capture`, before the test body creates
its Sandbox. It polls the test namespace once per second for up to 300 seconds
to find a launcher. That interval emits no host records and cannot consume the
subsequent sample window. After the first launcher has passed namespace, Sandbox
label, VMI owner UID, and KubeVirt label checks, it samples for up to 360 host
seconds at one-second intervals. Each QEMU match must also carry that exact
launcher Pod UID in its cgroup. It reads host QEMU thread state passively; it
does not issue QMP commands.

Bounds are explicit: at most 8 launchers per sample, 16 QEMU threads per
launcher, 4096 `/proc` entries or 0.5 seconds per host process scan, 360 samples,
4 MiB of host JSONL, and 16 serial files / 32 MiB while summarizing guest
markers. The summary records first-launch and sample timestamps, stop reason,
API errors, rejected identities, scan incompleteness, and truncation. Missing
guest markers are reported as missing or partial, not as proof of host-side
absence. A completed set of 360 guest samples can still cover less host time
than expected because guest time can lag wall time under software emulation.

Artifacts are written under the test's undeclared `vm-artifacts/` directory:

- `boot-rcu-host/full-suite.jsonl` and `full-suite-summary.json` contain the
  timestamped, Pod-UID-correlated host observations and their coverage status.
- `boot-rcu-guest-summary.json` correlates guest marker counts with host Pod
  UIDs using retained serial logs. `boot-rcu-guest-summary-error.json` is written
  if this final correlation fails; a summary failure cannot replace an active
  test/shutdown exception.
- `serial/` contains the guest serial files retained by the existing VM
  diagnostics.

This observer adds diagnostic serial output and host `/proc` polling, so it is
intended for an explicitly opted-in diagnostic action. It does not reproduce or
alter the standalone TCG VMI's topology, and a passing test only verifies
capture and ordinary test lifecycle, not coverage of an intermittent boot or
shutdown failure. Any sample, time, identity, or byte bound reached must be
considered when interpreting absent evidence.
