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

## Clean-base smoke

On 2026-11-07, the opt-in command below passed once on BuildBuddy invocation
[109b700e-0bbc-4fb5-85c8-5881987d1ca1](https://app.buildbuddy.io/invocation/109b700e-0bbc-4fb5-85c8-5881987d1ca1):

```sh
bbr test //agentplane/system_tests/suites:test_vm \
  --test_env=AGENTPLANE_VM_BOOT_RCU_OBSERVER=1 \
  --nocache_test_results
```

The runner fetched base `83ddf8b1b0a6f4d96708fdfbbbf74b1dbed1e7fd` and applied
the observer patch with SHA-256
`b691af2bd8fdf0b9a737cd58e8d4f5aa22e31d89b6db59eceffd2e4d148253f9`. This run
predates the later mypy-only correction to the summary counter updates. The
suite passed in 379.7 seconds. Host sampling wrote 180 of 360 allowed records
over 179.5 seconds, with no API failures, truncation, or incomplete process
scans. All three observed launcher Pod/VMI identities had QEMU matches in at
least 105 samples and later had deletion timestamps. Guest serials contained
86 samples for `vm-1`, 65 for `vm-2`, and 79 for `vm-codex-oom`; each sample
started and ended without sample errors or serial truncation. Each guest also
logged shutdown proof exit status 0 and `reboot: Power down`. The observer did
not reach its 360-sample completion receipt before test teardown, so these are
partial captures, not a complete 360-second boot window. This single healthy
run did not reproduce the intermittent failure.
