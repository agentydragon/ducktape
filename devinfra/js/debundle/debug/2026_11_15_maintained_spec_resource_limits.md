# Shared-execution performance investigation

Source analysis regressed during the cross-chunk work. The measured bottleneck
is repeated callback and native-integrity analysis, not the exact ESM import
scheduler. The latter has a separate worst-case complexity problem, documented
in [the scheduling proof](../docs/polynomial_esm_scheduling.md).

## Before and during this PR

The earlier [Excalidraw measurements](../perf/excalidraw_roughjs_analyze.md)
completed the split roughjs/clsx corpus in 39–49 seconds. Keeping those packages
inlined instead took 304–341 seconds, despite nearly unchanged total JS bytes.
Expensive analysis and sensitivity to chunk boundaries therefore predate this
PR. More complete cross-chunk propagation exposes additional provider bodies,
captures and effects that the old chunk boundary did not model equivalently.

During this PR, Excalidraw analysis remained active beyond 26 minutes. A later
remote attempt ran for 2 h 17 min and was killed by the guest kernel OOM killer.
After the census-storage repairs below, a native run still reached a 600-second
diagnostic cap without an artifact: 1,771.80 seconds user CPU and 1,442,056 KiB
peak RSS with three Rayon threads. These measurements involve different engine
revisions, environments and, in some cases, projections. They establish the
observed regression and current failure, not an isolated per-commit slowdown
factor or a controlled completed-analysis comparison.

`debundle analyze` produces source facts from an assignment-independent
projection; the emitted-module schedule is constructed during apply. The
scheduler's NP-completeness does not explain an Analyze profile. This PR does
not change the exact schedule constructor. Stronger inferred edges could make
later scheduling instances harder, but that has not been measured as the cause
of these long runs.

## Execution and memory evidence

The pre-rebase core/icons invocations
[0d0fa243](https://app.buildbuddy.io/invocation/0d0fa243-dcc7-5bec-bbae-473e426a5c3b)
and
[ec5f3b4a](https://app.buildbuddy.io/invocation/ec5f3b4a-c593-5941-99f9-aa96f089c510)
shared one physical Excalidraw Analyze action. Worker queueing was about 63 ms;
execution took 2 h 17 min and peaked at 7,630,225,408 bytes. The guest kernel
explicitly records the analyzer invoking the OOM killer and being killed, with
7,203,920 KiB anonymous RSS. This was an executing analyzer, not runner setup or
queue delay. GitHub cancellation did not stop the action promptly.

Private maintained-spec invocation
[0f356891](https://app.buildbuddy.io/invocation/0f356891-b7b8-48f0-8fa9-5917b0f44778)
built gaffer-private `c221d4bd9c3f2772a24b09c752705052ac585d14` against engine
`e68222c09c961c639286d5234448b886da7a9bdf`. Claude CLI 2.1.292 analysis retried
six times with executor OOM failures and 43.0–57.8 GiB reported peaks. Tana Web
78d928dca7 analysis was killed in two completed attempts at 7.1 GiB reported
peak. Neither produced a successful analysis or apply/runtime result. Tana's
target-level `BUILT` label accompanied an aborted BEP event and is not a pass.

## Profiled costs and implemented repairs

An early 90-second Excalidraw CPU sample attributed 82.02% inclusive cost to
`retain_foreign_callback_effects`: available provider source incorrectly took
the unavailable-source fallback, repeatedly promoting whole-source reads and
rebinds. Source-aware promotion repaired that path. Later profiles still show
repeated binding/availability promotion, rather than import-order search.

A reliable 90-second frame-pointer capture of the census/source-index candidate
recorded 12,916 cycle samples without loss: 89.91% inclusive in
`retain_foreign_callback_effects`, 43.88% in `promote_top_effects`, and 34.77% in
`record_program_cell_read`. These overlapping shares must not be added. It is
not a completed-analysis benchmark.

Heaptrack identified repeated native prototype-census construction and
copy-on-write cloning during location attachment and native point-effect
initialization. The implementation shares the immutable histogram, uses weighted
foreign contributions plus local activation deltas, and only copies mutation
vectors that need location updates. Duplicate, unknown-key and symbol-only
counts remain additive; public input mutation invalidates the cached census.
Prepared sources and imported binding spellings also have immutable indexes;
callback root unions deduplicate before cloning values. These changes preserve
live binding reads, ordered availability and conservative effects.

Matched 60-second Heaptrack runs used the same projection SHA-256
`9d424dac670d1548676d400169af9986e2293589d9fd188e78cfea3278e449a1`, three
Rayon threads and an 8 GiB virtual-memory cap. Earlier-in-PR peak heap was
812.77 MB versus 502.08 MB after the census/source-index changes; peak RSS was
1,476,960 versus 1,116,276 KiB, about 24% lower. Both hit the diagnostic cap
without an artifact. This is a memory improvement within the PR, not a
completed-analysis speedup over devel. Rootless mutation witnesses may still
exercise a separate witness-vector copy-on-write path.

## Small reproduction and next measurement

[PR #16652](https://github.com/agentydragon/ducktape/pull/16652) reproduces roughly
quadratic callback analysis with no imports or extraction. A factory returns a
closure after attaching its reader with `Object.defineProperty`. On engine
`46648793df6d00e2bda9d58f633929a3d9df13ca`, 512/1,024/2,048 registrations took
0.80/3.23/12.77 seconds of analysis; projection and apply stayed small. The
lexical-only 1,024-reader control took 0.28 seconds. The optimized frame-pointer
build is
[9c5133d1](https://app.buildbuddy.io/invocation/9c5133d1-2e79-442b-af6f-ad186a7a92c6).
These are single profiled native runs on a shared Ryzen 9 9950X3D with four
Rayon threads, not a latency distribution. The missing function-target
`DefineProperty` transfer triggers conservative retained-callback walks.

The owned generator is `perf/gen_callback_registration_corpus.py`. Generate a
fresh corpus through Bazel, with `$scratch` set to a new directory under the
task scratch root:

```sh
bb run //devinfra/js/debundle/perf:gen_callback_registration_corpus_bin -- \
  --out "$scratch/attached-1024" --callbacks 1024 --invocations 1
```

Use `--omit-attached-method` for the lexical control. Native optimized build,
staging and profiling instructions are in
[the Bazel integration guide](../docs/bazel_integration.md#profiling).
`e2e/callback_registration_scale_test.rs` preserves original/emitted Node output
for local and imported factories. A whole-query callback-graph cache experiment
made a 2,048-entry run worse (19.07 seconds to 31.12–74.03 seconds) and is not
shipped; mostly growing roots do not justify execution-effect reuse.

The remaining work is to reduce repeated binding/availability promotion with
complete dependency witnesses, or use an explicit conservative precision limit.
An already-promoted owner is insufficient: captures can be havoced, superclass
promotion can execute code, and each query point retains its ordered reads.
Compare completed original/current-engine runs on the same frozen corpus and
record Analyze versus apply CPU/wall/RSS separately. Canonical gate,
Node/browser output and private maintained-spec acceptance remain unverified;
a capped run or passing reduced fixture does not establish them.
