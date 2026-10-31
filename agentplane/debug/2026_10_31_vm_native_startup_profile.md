# VM native app-server startup profile (2026-10-31)

This is the Debian 6.12, EROFS-root VM system-test guest under QEMU TCG on a
BuildBuddy worker. It is not production's NixOS guest. The separate
[boot critical-chain note](https://github.com/agentydragon/ducktape/blob/d4f8162f9c9f33ac290803fe146fbdceb5031664/agentplane/debug/2026_10_31_vm_boot_critical_chain.md) covers systemd
activation ordering.

## Collection boundary

`//agentplane/system_tests/suites:test_vm_profile` is a manual variant of the
ordinary VM suite. Bazel supplies Debian `linux-perf` only to its profile guest
image. An image-size probe measured an ordinary root of 552,833,024 bytes and a profile root of
597,110,784 bytes (+44,277,760 bytes, 8.0%). The 4,096-byte difference between
the ordinary root here and the earlier 552,828,928-byte ordinary image is the
opt-in service/script in the root archive. The manual target imports only the
profile root and selects it for its VM templates; the recording kernel flag is
added only to the OOM SandboxTemplate. The corresponding no-profile control
uses the same manual target and root image but omits that runtime flag.

The guest's opt-in systemd service starts before Runner and is outside the
96 MiB native executor leaf. It probes `cpu-clock` with the guest's root
capabilities, samples all user-space processes for a bounded 60 seconds, and
writes `/state/.agentplane-native-startup.perf.data.partial`. A successful
`perf record` atomically publishes the final file in the VM's state virtiofs.
The test checks the state PVC's Sandbox owner UID and PV claim UID, then copies
the raw file into `test.outputs/vm-artifacts/profiles/<sandbox UID>.perf.data`
before Sandbox teardown. The existing `process-spans.jsonl` OTel artifact gives
app-server launch/start/initialize intervals; profiling adds no tracing
recorder. The native app-server process/PID and same-session OOM leaf checks
remain unchanged.

To repeat either run:

```sh
bbr test //agentplane/system_tests/suites:test_vm_profile \
  --noremote_accept_cached --test_env=AGENTPLANE_VM_NATIVE_PROFILE=1
bbr test //agentplane/system_tests/suites:test_vm_profile \
  --noremote_accept_cached
```

Use `bbapi artifact list <invocation ID>` to locate the selected guest's raw
profile, serial console and `process-spans.jsonl`. Download the raw file with
`bbapi artifact download <invocation ID> <UID>.perf.data -o <path>` and inspect
it with `perf report -i <path>` or `perf script -i <path>`. Match samples by
app-server TGID/PID across all its worker threads, and convert the sample clock
to the OTel wall clock before attributing cold-start CPU. The 60-second whole
window also contains later turn work and cannot alone identify cold-start
hotspots. If sample-clock alignment cannot be established, report that limit.

## Evidence and interpretation

The earlier [successful guest perf record](https://app.buildbuddy.io/invocation/361f0887-e462-4777-8d59-a7a8be3afd7c)
captured 1,123 `cpu-clock` samples with zero lost and overlapped the OOM
Session's native app-server start. Its serial raw-data transfer was truncated,
so the comm-only console report is not a process-wide or cold-start CPU profile.
In particular, Rust worker threads appeared as `tokio-rt-worker`; the 2.85%
`codex-code-mode` comm entry did not represent all native app-server CPU.

An [experimental two-root run](https://app.buildbuddy.io/invocation/a35bb168-6ee9-44a4-a4db-02d852bf9159)
retained a valid 3,240,452-byte `perf.data` artifact despite the suite reaching
pytest's 600-second limit. It imported the ordinary and profile root images
sequentially, taking 70.766 and 40.670 seconds respectively during 322.485
seconds of fixture setup. Removing the unused ordinary-root import saves that
measured import step from the manual profile path; it is not a measured
full-job improvement. The OOM scenario itself passed before the suite timeout.
The raw file has 1,026 `cpu-clock` samples and no lost samples. The OOM Session's
app-server TGID was 432, including `codex-main`, `tokio-rt-worker`, and
`sqlx-sqlite-worker` threads. Within its 35.728-second `app_server.start` span,
195 of 561 system-wide samples belonged to this TGID, roughly four seconds of
sampled user-space CPU at 49 Hz. No separate initialize span was retained for
this OOM Session; an initialize interval from another Session must not be joined
to its PID. The guest clock markers map `perf` sample uptime
to OTel UTC within about one second. The remaining wall time is not attributed
by this user-space profile: kernel CPU, scheduling, TCG effects, and waits are
not distinguished. The pinned Codex ELF is stripped. Its exact 0.162.1
[separate-symbol archive](https://github.com/openai/codex/releases/download/rust-v0.162.1/codex-symbols-x86_64-unknown-linux-musl.tar.gz)
has SHA-256 `3521f16ef1edb243890a31d86117e2c1b62f78ee0a52ad4f849ff11827cb4b9c`.
The extracted `codex.debug` matches that ELF's `.gnu_debuglink` CRC
`942abfee`. Install the matching `bin/codex` and `bin/.debug/codex.debug` in a
local symfs, then inspect the cold interval using guest uptime timestamps:

```sh
perf report -f --stdio --no-inline -i <perf.data> --symfs <symfs> \
  --pid 432 --time 103.286,139.014 --percent-limit 0 \
  --sort comm,dso,symbol --no-children
```

Host perf 7.2.9 needs `--no-inline` to avoid its repeated addr2line protocol
failure on this debug file. The report's denominator is all 561 system-wide
samples in the selected interval, even with the PID filter. The largest
individual resolved functions have three samples each (0.53% of those 561),
including clap command construction, `MessageProcessor::new`, and SQLite
expression resolution. The sample count supports no dominant individual CPU
hotspot in this run.

The [479-second passing VM run](https://app.buildbuddy.io/invocation/b4194d2a-0038-4bf5-b3c3-7d63229d81a1)
was unprofiled because its opt-in flag was consumed during Bazel's build-time
manifest rendering. It is not a profiler-overhead control. Runtime fixture
selection now reads the flag after rendering, with a focused
[`test_render` pass](https://app.buildbuddy.io/invocation/a666ae7c-6600-4be1-99d0-ea1bd8fa1f9d).

## Passing single-root profile and unprofiled control

The [single-root profile](https://app.buildbuddy.io/invocation/44d682a7-5b89-4544-ae8e-259557148aa6)
and [unprofiled control](https://app.buildbuddy.io/invocation/83f4fdd3-3722-4987-8f57-b68f8ee421c3)
both passed all six scenarios without increasing the 600-second pytest deadline.
The profile's outer remote-runner invocation was `d23f2ba8-58cf-4c37-a0ec-b92c6113d952`;
the control's was `7bdb8978-0842-4bf6-b186-efc516ce3b12`.

The profiled build checked out `61d18e604e37757ebc9838404a292fec6348181f` and applied workspace
patch `98771aff4fd8e6feeac6c9a4318ee4659d6ba7b70da724cc638e250347f00cec` plus documentation
patch `32993579e4015a758eb5af0c0140143e7ffe80e366a9e07c8baf09ada2db6f25`.
The control checked out `c544fff5391b2b422e8026ec29daf190373fe706`, which includes later
guest summary/report-argument changes. Both traces record the manual profile-root and boot-image
labels, but their exact image-byte identity is unverified. These measurements precede the merge
of the boot and resource-identity changes into this branch.

| Measurement               |   Profile |  Control |
| ------------------------- | --------: | -------: |
| VM test action            |   376.6 s |  284.0 s |
| Fixture setup span        | 171.012 s | 94.315 s |
| Profile-root import       |  29.864 s | 21.820 s |
| OOM guest ready span      |  75.433 s | 73.057 s |
| OOM app-server start span |  20.072 s | 19.098 s |

These are individual observations, not a causal estimate of profiler overhead or an improvement.
Source snapshots differ, and worker/cache conditions were uncontrolled. Both OOM commands exited
137, both follow-up commands exited 0, and both Runners remained ready. The control produced no
profile artifact.

The passing profile retained `test.outputs/vm-artifacts/profiles/59593079-8554-4fd4-947b-feec389cb07a.perf.data`,
3,687,840 bytes, SHA-256 `3c6fde75c61be22a8f5b79fb7be8d0f0834f88c532d570deda9f4c9df024d75a`.
It contains 1,054 samples with zero lost. The OOM Session was
`c93426ee-8f4b-40c8-a7a6-555cec7c5c91`, and its app-server TGID was 400.
The whole recorded interval contains 434 samples from that TGID, across its worker threads.

Use the **guest's emitted** clock fields to align samples, rather than the CRI log's host receipt
timestamp. The begin/end pairs were `unix_seconds=1793456145 uptime_seconds=32.33` and
`unix_seconds=1793456216 uptime_seconds=103.07`. Their nominal UNIX-minus-uptime offset is
`1793456112.8` seconds, with about one second of uncertainty from the integer wall clock and
the separate clock reads. The matching Session's `app_server.start` span was
`14:16:00.403830`–`14:16:20.476289` UTC, giving a nominal sample interval
`47.603830`–`67.676289` seconds. It contains 108 TGID-400 samples out of 315 sampled userspace
events in this guest, roughly 2.2 sampled user-CPU seconds at 49 Hz. Shifting the entire interval
by minus/plus one second yields 96/120 TGID samples; the boundary precision does not support
finer CPU claims. No separate initialize span was retained for this Session either.

```sh
perf report -f --stdio --no-inline -i <passing-perf.data> --symfs <symfs> \
  --pid 400 --time 47.603830,67.676289 --percent-limit 0 \
  --sort comm,dso,symbol --no-children
```

The control's watch RPC ended with `ResourceExhausted: Log index limit exceeded`; direct invocation
and test artifacts establish that execution succeeded. This log-following failure is distinct
from the HTTP/2 `RST_STREAM INTERNAL_ERROR` investigation. An earlier profile dispatch also lost
its event-log stream during authentication. Its retained analysis output proves that the command
had been accepted; the subsequent manual retry was mistaken. `DISCONNECTED` plus zero recorded
actions does not establish that an accepted command never ran.
