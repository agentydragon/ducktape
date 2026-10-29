# PR 12798: backwards runner clock crashes Bazel profiling

The second CI attempt hit a backwards monotonic clock on a restored BuildBuddy
Firecracker VM. Bazel 9.2.0 completed its target-count query, then crashed while
serializing profiler counters. Firecracker's restored-vCPU TSC offset defect is
the leading explanation for the clock regression; the exact host-side cause is
not yet confirmed.

## Run identity

- [GitHub run 40787876182](https://github.com/agentydragon/ducktape/actions/runs/40787876182), attempt `1`, October 28, 2026.
- PR head: `a2e5ecbbcbe47fd351c5d87e2903dd3c2133c79c`.
- Tested merge: `eb60f701c563dab744befad06893ec828117b63f`.
- [Remote runner](https://app.buildbuddy.io/invocation/7db4a037-5b71-4e62-8774-210831b20e06): `7db4a037-5b71-4e62-8774-210831b20e06`.
- [Crashing query](https://app.buildbuddy.io/invocation/b2461568-5767-4770-98e8-53bcc2a83862): `b2461568-5767-4770-98e8-53bcc2a83862`.
- Executor hostname: `sjc-prod-r2204m06vm5wyv`; executor ID: `1dcafa1b-8e6b-44db-8185-b1b9b277757e`.
- VM ID: `1b0c0430-22fd-4fa5-8ae2-84028257643c`; snapshot ID: `e5c513f4-c14f-45da-bbcc-ee579ae341bc`.

## Evidence chain

1. Target selection found `44` affected targets. The next command was
   `bazel query --query_file=.../test-query.txt`, used to count tests.
2. Immediately before that query's invocation announcement, the runner log says:

   ```text
   WARNING: Invalid duration: start=267730901, end=267727312
   ```

   These millisecond readings regressed by `3589`. Bazel's
   [DurationMillis](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/cpp/blaze_util.h#L142-L164)
   emits this warning when the second reading precedes the first. The Linux
   [timer](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/cpp/blaze_util_linux.cc#L86-L93)
   reads `CLOCK_MONOTONIC`.

3. The query's BES events record Bazel `9.2.0`, server PID `22977`, start
   `23:33:50.598Z`, and a successful `BuildFinished` at `23:33:52.350Z`.
   This success describes the query before its profiler cleanup crashed; it
   does not mean the command or CI finished successfully.
4. Cleanup throws `NegativeArraySizeException: -1` from
   `TimeSeries.toDoubleArray`, called by `CollectLocalResourceUsage.logCollectedData`.
   In the [exact Bazel source](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/profiler/CollectLocalResourceUsage.java#L225-L246),
   array length is `(int) (elapsedNanos / 1_000_000_000) + 1`.
   The stopwatch uses `System.nanoTime()` through Guava `33.5.0-jre`.
   For this short invocation, length `-1` implies an elapsed value between
   `-3` and `-2` seconds, excluding `-3` and including `-2`.
5. Cleanup runs again and throws `IllegalArgumentException` at
   `CollectLocalResourceUsage.stop:212`: the first call already set
   `stopLocalUsageCollection`, but threw before clearing `collector`.
   This is a secondary exception from the same failure.
6. The Bazel client exits `37`; Python raises `CalledProcessError`; remote CI
   exits `1`. At `23:34:06Z`, our status finalizer publishes `error` because
   the deterministic test invocation was never started.

The earlier `//external` query errors did not stop target selection. They are
not the terminal failure in this attempt.

## Likely infrastructure cause

The executor's retained `vm_log_tail.txt` reports `Running Firecracker v1.16.2`
at `23:31:32.063Z`, followed by `/snapshot/load` with `resume_vm:true` and
multiple vCPUs resuming. This directly establishes VM restoration.

[Firecracker PR 6200](https://github.com/firecracker-microvm/firecracker/pull/6200),
merged September 28, 2026, fixes restored vCPUs receiving different TSC
offsets. With a TSC-backed guest clock, moving between vCPUs can make time go
backwards. The [v1.16.2 restore code](https://github.com/firecracker-microvm/firecracker/blob/v1.16.2/src/vmm/src/builder.rs#L481-L501)
lacks that synchronization. This closely matches
the observed clock regression and profiler failure, but the retained evidence
does not identify the guest's active clocksource or per-vCPU offsets.

The probe archive was SHA256-verified against its advertised digest. Its
`probes.jsonl` and `previous/probes.jsonl` both contain the previous run's
samples on commit `ef1e694f0abe5052c41b44ca95b2d544235668c6`, ending at
`23:29:27Z`. This attempt failed before `_probe("before-test")`, so finalization
packaged stale samples. They cannot establish this attempt's guest clock state.

## Re-fetch evidence

Run these with the repository devshell and BuildBuddy credentials:

```bash
bb view 7db4a037-5b71-4e62-8774-210831b20e06 --lines=200000
bbapi invocation b2461568-5767-4770-98e8-53bcc2a83862 --json
bb execution get bb-snapshot/uploads/0c41945c-3a2b-4f88-ac47-da2936d1710f/blobs/blake3/bc5972e68869dbd678995b5fc0e2bfe131641ac07279d0e36231b59bfa2da548/149 --output=json
bb download bb-snapshot/blobs/blake3/860b8186e0933f592f38c5c861d87ff976194246fec479e11cdc470c86cd4be6/11076 > /tmp/pr12798-vm-log-tail.txt
bb download 2cb4f8e249862d3f6a94bb20a9e0d72098373b9d4b9bda4e6d753de391cd6c05/32567 > /tmp/pr12798-probe.tgz
```

For BuildBuddy escalation, ask whether the deployed Firecracker binary includes
PR 6200 or a backport, which guest clocksource was active, and whether the
executor has TSC/clocksource diagnostics for this snapshot restoration. The
runtime identifiers and backwards-clock warning above locate the incident.
