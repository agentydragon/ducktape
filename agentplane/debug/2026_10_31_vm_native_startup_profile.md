# VM native app-server startup profile (2026-10-31)

This is the Debian 6.12, EROFS-root VM system-test guest under QEMU TCG on a
BuildBuddy worker. It is not production's NixOS guest. The separate
[boot critical-chain note](https://github.com/agentydragon/ducktape/blob/d4f8162f9c9f33ac290803fe146fbdceb5031664/agentplane/debug/2026_10_31_vm_boot_critical_chain.md) covers systemd
activation ordering.

## Collection boundary

`//agentplane/system_tests/suites:test_vm_profile` is a manual variant of the
ordinary VM suite. Bazel supplies Debian `linux-perf` only to its profile guest
image; the ordinary root image is 552,833,024 bytes, and the profile root is
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
sampled user-space CPU at 49 Hz. Its overlapping 30.474-second initialize span
contained 187 of 468 samples. The guest clock markers map `perf` sample uptime
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

<!-- Add the raw-profile/control invocation IDs, exact PID and time-sliced sample
     findings, and paired action/OTel durations after the current runs finish. -->
