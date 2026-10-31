# VM suite concurrency and image storage

The VM suite has six independent guests: Claude and Codex credential cases, the
central sampler, Claude and Codex turns, and Codex tool OOM containment. The
baseline runs them in two sequential groups of three on a 4-unit executor.

The selected change runs all six in one TaskGroup on an 8-unit executor.
Each scenario retains its Sandbox and cleanup ownership, and the selected
Claude boot still runs the private supervisor checks. The image-storage
experiment also puts containerd's unpacked layers on tmpfs. The larger worker
provides room for both those layers and the guests; the original 4-unit VM
worker kept layers on disk to fit its memory budget. The final implementation
retains the disk-backed VM store: the tmpfs repeat lost an executor, and a
reliable warm improvement has not been established.

## Measurements

Runs below execute the full `//agentplane/system_tests/suites:test_vm` target
with `--nocache_test_results`, sequentially on RBE on 2026-10-30. They use base
`ac8d8c4dd15afdb99aa191af5405030dae867334`, concurrency commit `5317c0b9`,
and image-storage commit `13db78e5`. The existing Bazel result, VM trace,
k3s timeline and execution resource statistics supply the measurements.

| Variant                          | Test action, s | Stack ready, s | Scenario groups, s | CPU-seconds | Peak memory, GiB |
| -------------------------------- | -------------: | -------------: | -----------------: | ----------: | ---------------: |
| Baseline, guest root imported    |          455.3 |          134.5 |              280.7 |       1,470 |              6.3 |
| Baseline, warm disk store        |          417.1 |          111.1 |              271.0 |       1,363 |              6.2 |
| Six concurrent, cold disk store  |          499.1 |          250.7 |              216.9 |       2,285 |              8.1 |
| Six concurrent, warm disk store  |          325.3 |           95.5 |              196.3 |       1,490 |              8.3 |
| Six concurrent, cold tmpfs store |          433.5 |          186.0 |              204.4 |       1,914 |             11.8 |

The baseline's scenario time sums its two sequential `vm.case` spans; each
candidate has one enclosing case. These spans include diagnostics and cleanup.
Stack readiness starts at k3s startup, excluding Python startup and final stack
teardown; it is not the complete test setup or invocation time. Peak memory is
BuildBuddy's execution-level measurement, not the sum of guest RAM allocations.

All five tabulated runs passed, each retaining six distinct launcher console identities
and six orderly shutdown receipts. Launcher start and shutdown timestamps show
at most three live guests in the baseline and six in each candidate. Scenario
assertions and independent boots are unchanged.

Cache state and workers differ. Both baselines ran on executor `ad60deae`; the
three candidates ran on `fb1b38ba`. The first baseline reused infrastructure images
but imported its new guest root; the second reused all images. The first concurrency run had a cold image
store; its repeat reused all images. Mounting tmpfs resets disk-backed snapshot
metadata, so the first image-storage run imported all images again.

The warm concurrency run was 28.6% shorter than the first baseline action, but this
pair mixes worker and image-cache effects. Against the warm baseline it was
22.0% shorter (417.1 to 325.3 seconds), still on different executor shapes. Its
scenario phase was 74.7 seconds shorter than the warm baseline. Comparing the
two cold candidate stores on the same executor, tmpfs
reduced stack readiness by 64.7 seconds. Neither comparison establishes a 30%
reduction in complete PR CI time. The concurrency request also doubles the
worker allocation; similar CPU-seconds do not imply identical allocated cost.

## Evidence

- [Baseline](https://app.buildbuddy.io/invocation/d36ae3fa-896f-4439-b923-31f400dcb4e3).
- [Baseline, warm](https://app.buildbuddy.io/invocation/a40545a7-8fa5-4c21-a271-95bb4131e140).
- [Concurrency, cold](https://app.buildbuddy.io/invocation/f551427c-3cab-4a0c-bf52-306c4671cb06).
- [Concurrency, warm](https://app.buildbuddy.io/invocation/0448563e-b926-437d-aa21-6d4f2442b7c4).
- [Tmpfs, cold](https://app.buildbuddy.io/invocation/95868ef8-127b-4e07-bd0c-cae47ae8137f).

The tmpfs repeat, invocation `98b48492-560e-4fdc-8617-70472600aa63`, lost its
first executor attempt after 399.7 seconds: BuildBuddy reported `UNAVAILABLE`
because Firecracker's control socket refused a connection while pausing the VM.
That attempt recorded 1,871 CPU-seconds and 12.1 GiB peak memory, but exposed no
test outputs through `GetExecutionDownloads`. Its cause is unconfirmed; it is
not a passing sample. BuildBuddy automatically retried the action.

Run `bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results
--test_output=errors` at the chosen revision. Inspect `vm-phases.jsonl`,
`k3s-timeline.jsonl`, and retained launcher and guest console logs in the target's
undeclared outputs. Use `TestResult.testAttemptDuration` for action time and
execution metadata for queue and resource statistics; keep full invocation and
GitHub job duration separate.

## Remaining startup cost

Retained launcher Pod status shows four serial init containers: the container-disk
binary, root disk, runner disk and kernel boot volume. Each process finishes in
0–1 seconds, with roughly 1–3 second gaps between them. With six guests, the
compute container starts 17–26 seconds after Pod creation. The same gaps remain
with tmpfs, so faster image unpacking does not remove this part of startup.

The cold tmpfs run's longest scenario is Codex tool OOM: 187.2 seconds through
its assertions, followed by 17.1 seconds of cleanup. Its initial Sandbox readiness
takes 82.1 seconds and its first scripted command takes 58.9 seconds. These spans
identify the remaining critical path without removing independent boots or
weakening assertions. All measurements here use the existing QEMU software CPU
emulation; they do not assume a KVM-capable executor.
