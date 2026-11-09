# Rust CI chain profile, 2026-11-04

The useful changes are to remove the selector solver's dependency on the large
analysis crate, precompute reusable input digests for the analysis identity, and
measure compiler admission separately from test concurrency. The profiles do
not establish a CI speedup. In particular, the identity's measured read time
cannot simply be subtracted from CI elapsed time: another compilation branch
already overlaps it.

[Structured measurements](ci_rust_chain.json) accompany this note.
Source inspection is at `d1b9fcbf2d`; temporary profiling commits are linked below.
No compiler, concurrency, runner, snapshot or validation defaults were changed.

## Actual CI path and clocks

The two existing hosted push invocations were selected because their last test
was `//devinfra/js/debundle/e2e:propose_commutations_budget_test`:

| Observation                            | [5716744a](https://app.buildbuddy.io/invocation/5716744a-14a7-5769-8511-29fa5fbd6417) | [bc368612](https://app.buildbuddy.io/invocation/bc368612-a68e-5bee-8856-d84b925c765c) |
| -------------------------------------- | ------------------------------------------------------------------------------------: | ------------------------------------------------------------------------------------: |
| Bazel invocation elapsed               |                                                                               305.5 s |                                                                               318.1 s |
| Recorded action critical path          |                                                                              246.70 s |                                                                              242.41 s |
| Pre-test actions on that path          |                                                                              175.35 s |                                                                              171.07 s |
| Budget test action                     |                                                                               71.35 s |                                                                               71.34 s |
| Selector metadata actions on that path |                                                                               42.52 s |                                                                               45.47 s |
| Identity remote process                |                                                                               27.33 s |                                                                               29.69 s |
| Identity remote CPU                    |                                                                           23.89 CPU-s |                                                                           25.44 CPU-s |

All 93 first-party Rust/identity execution rows in each invocation are originating
executions (`invocationLinkType=1`). These are not cached target durations.
The recorded critical path is a sum of selected action lifetimes; it is not the
invocation's entire elapsed time or a sufficient counterfactual scheduling model.
These historical invocations also precede the inspected source, so the fresh
profiles below establish mechanisms rather than a matched before/after result.

The path crosses analysis metadata, selector IR, constraint backend, model builder,
backend solver, runtime, selector resolution, spec resolution, staged analysis,
identity generation, CLI compilation, final linking, then the budget test.
Other prerequisites and parallel branches matter at each join.

## Compiler profiles

The [successful 13-target profile build](https://app.buildbuddy.io/invocation/4e127f0a-3cc0-400d-ae52-340d0938dbe2)
used the pinned Rust 1.98.1 compiler in normal `fastbuild`, ordinary RBE/cache,
`--config=nolint`, rules_rust's `self_profile` output group and Python cProfile.
It profiles compilation, so using the actual CI build mode is intentional; the
optimized-binary prescription for profiling debundler runtime is a different task.

The [instrumentation commit](https://github.com/agentydragon/ducktape/commit/29a321db339810631bdfbec6faaa4aac4249b672)
only enables the compiler's profiler on 12 selected crates and declares the
hasher's profile outputs. Its temporary `RUSTC_BOOTSTRAP` enables profiling with
the pinned stable compiler; it is not a proposed production setting. The Nix
shell adds `measureme` for reading the profiles. No dependency or RBE image repin
is involved. The separate
[argument/group profile](https://app.buildbuddy.io/invocation/8df3c1c6-20b5-4ca0-a8e1-c62a40cc6cb6)
adds compiler query arguments and splits hasher attribution at
[1442b445](https://github.com/agentydragon/ducktape/commit/1442b445327b6a459923206ec3697a67b6063864).

The analysis crate's default-profile main-thread phases include 6.64 s type
checking, 8.07 s borrow checking and 14.01 s metadata generation. The latter
contains a 9.73 s monomorphization graph walk. The profile records about 113,600
`items_of_instance` queries. This substantial work precedes metadata emission,
so additional LLVM code-generation parallelism does not remove it.

The argument profile finds distributed work rather than one huge handwritten
function. Its largest recorded `typeck_root` is
`facts::sync_effects::execution_index::collect`, 0.263 s; its largest
`mir_borrowck` is the same function, 0.140 s. These are instrumented observations,
not the sum of each function's compilation cost. They do not justify promising
large savings from a local rewrite of that function.

For selector IR, full compilation records 2.12 s of self time in metadata events
out of 5.54 s summed profiler event time. It registers 172 crates and decodes
27,885 trait implementation headers. Its actual CI metadata action takes about
7-8 s even though the source file's direct analysis import is only
`analysis::SourceUnitId`. Constraint backend, model builder and backend solver
have the same unnecessary dependency for that type. Several later selector
stages inherit it transitively.

Move `SourceUnitId` from `graph/dependency_graph.rs` into the existing small
`source_identity` crate, retain its distinction from `SourceUnitOrdinal`, update
the crate documentation, and update all imports and BUILD deps.
That allows the solver branch to compile while the 89-file analysis crate is
still compiling, and reduces repeated metadata loading. Analysis-backed fact
extraction and selector lowering still need analysis; staged analysis and final
linking still need both branches. The observed 31-40 s analysis metadata phase is
an overlap opportunity, not an independently removable saving. Measure the full
CLI/test path after the move before considering consolidation of solver crates.

Profiler totals sum events across threads and can exceed worker wall time.
For example, analysis records 72.09 s summed events while its remote process
lasts 49.61 s. They are not interchangeable with CPU or elapsed time.

The final E2E executable's full compiler profile spends 15.14 s in `run_linker`
within 17.32 s summed event time. The actual compiler arguments already use
`opt-level=0`, `debuginfo=0`, `embed-bitcode=no`, LLD and `-Cstrip=symbols`.
Pipelining is already enabled. Switching to these settings is not a new fix;
there is no measured case here for a global optimization-mode or toolchain change.

## Identity: mostly reads, mostly reusable inputs

The first cProfile records 22.51 s inside `main`: 18.66 s buffered reads,
1.23 s SHA-256 updates, and about 1.70 s including stat calls. Its enclosing
remote process takes 31.54 s. Launcher/import time is outside that profile; do
not silently attribute the entire difference to any particular component.

The second profile preserves the exact hash record order, records bytes read,
and profiles the two input groups separately:

| Group                                               | Manifest entries | Expanded files read |    Bytes read | Profiled function time |
| --------------------------------------------------- | ---------------: | ------------------: | ------------: | ---------------------: |
| First-party compiled/data inputs                    |               34 |                  34 |   362,952,269 |                 4.54 s |
| External crates, build outputs, data and toolchains |           11,082 |              11,161 | 1,736,179,231 |                22.05 s |

External reads take 18.63 s and external SHA updates 1.28 s in this second run.
Its remote process is 39.26 s; the group functions sum to 26.59 s. Extra inventory
work and different worker/input-cache conditions preclude treating this as a
controlled comparison with the first profile. Reads may include virtual
filesystem and input-fetch costs; cProfile does not identify their lower layers.

Both profiling builds produce exactly the same generated Rust identity file:
SHA-256 `eea81d1435b1d9e9edfba92689d0ad1e2c0d45057ab4738ebfdb73030d6a6b0d`.
The grouping therefore preserves the identity on this real compiled closure.
It is an attribution experiment, not an implemented digest cache.

A specific follow-up is a Bazel action that hashes the unchanged external/toolchain
input group independently of first-party engine compilation. It can start earlier
and be reused when only first-party code changes. The final action hashes changed
first-party bytes and folds the cached path/kind/content-digest records in the
original manifest order, with the existing configured-linking record. Preserve
all current inputs, directory and symlink checks, and preferably the exact final
identity byte stream. Test native/configuration/data invalidation as well as
compiled Rust changes. Do not substitute a source-only or final-executable hash.
This targets repeated reads; the earlier threaded-hasher experiment lost and is
not reopened by this profile.

The direct feedback benefit depends on the other branch. In `5716744a`, pipeline
metadata finishes 0.612 s after identity metadata, so removing identity work alone
has no direct CLI-start benefit with other completions fixed. In `bc368612`,
identity metadata finishes 16.019 s after pipeline metadata. That is this join's
conditional headroom, not proof of a 22 s CI saving. Moving identity injection to
binary entry points would face the same other-branch limit and should not be
counted as another additive saving.

## Driver admission and the next bottleneck

The canonical command line explicitly has `--jobs=50`, expanded from `--config=rbe`.
The profiles reach exactly 50 simultaneous client action-processing intervals.
Between invocation-relative seconds 15 and 185, all 50 slots are occupied for
84.97 s and 86.12 s respectively. Many slots hold leaf Rust tests' compilation or
Clippy actions while critical-path metadata remains unstarted. Low BuildBuddy
queue time does not exclude waiting for admission inside Bazel.

In `5716744a`, staged-analysis metadata's remote process starts 17.013 s after
its full rlib process starts; the other run starts them together. The inspected
[aquery](https://app.buildbuddy.io/invocation/50f75c9e-0b28-4a75-b755-0d04ea243bfb)
shows identical sets of 3,298 declared inputs for the two staged-analysis actions.
The inspected graph therefore permits the two actions to start together. For the
historical delay, saturated client capacity is a plausible scheduling explanation;
this does not distinguish admission policy from delayed Skyframe discovery or
prove that raising the cap would recover 17 s. Historical source and inspected
source differ, and scheduling varies between the two observations.

The previous [50-versus-100 full sweep](../../../buildbuddy/debug/bazel_jobs_full_sweep.md)
had a warm compilation closure and fresh tests; the 100-job arm was slower and
failed. Keep that negative result. The new question is compilation-heavy Rust
changes: can earlier admission of prerequisite compilation shorten feedback
without doubling the simultaneous fresh browser/Docker/VM workload? A bounded
comparison should separate compilation concurrency from test concurrency, retain
all Clippy/rustfmt/test coverage, and include any extra driver/analysis overhead.
No such comparison or concurrency-default change was performed in this profile.

After shortening the build graph, the 71 s budget test and other E2E tests remain.
The 30 s constant in the budget test is an assertion, not a sleep; its real
pipeline fixture runs before the assertion's timer starts. Profile that fixture
and command separately before changing the workload. Preserve the 4,096-step,
96-reader regression and its real CLI/graph/provenance coverage. Neither deleting
that coverage nor hand-constructing an intermediate graph is a valid speedup.
The observed build and hash costs overlap, so this note does not establish a
combined 25-50% reduction in PR feedback.

## Reproduction and profiling friction

Use the temporary instrumentation commit and ordinary `bbr` execution:

```sh
bbr --run_from_branch=codex/rust-ci-profile \
  --run_from_commit=29a321db339810631bdfbec6faaa4aac4249b672 \
  build --config=nolint \
  --@rules_rust//rust/settings:zself_profile_events=//devinfra/js/debundle@ \
  --output_groups=+self_profile,+profile \
  //devinfra/js/debundle:analysis \
  //devinfra/js/debundle:selector_ir \
  //devinfra/js/debundle:selector_constraint_backend \
  //devinfra/js/debundle:selector_constraint_model_builder \
  //devinfra/js/debundle:selector_backend_solver \
  //devinfra/js/debundle:selector_runtime \
  //devinfra/js/debundle:selector_resolve \
  //devinfra/js/debundle:spec_resolution \
  //devinfra/js/debundle:staged_analysis \
  //devinfra/js/debundle:analysis_identity \
  //devinfra/js/debundle:analysis_identity_source \
  //devinfra/js/debundle:cli \
  //devinfra/js/debundle:debundle_e2e
```

Download the advertised profile artifacts with `bbapi artifact list` and
`bbapi blob download`. In that commit's Nix shell, the analysis tools were
`summarize summarize --json <profile-prefix>` and
`crox --minimum-duration=1000 <profile-prefix>`, plus Python `pstats.Stats`.
The [compiler argument profile](https://doc.rust-lang.org/unstable-book/compiler-flags/self-profile-events.html)
enables `default,args` for analysis before the
broader default filter. Its raw trace is 1.68 GB, versus 199 MB for the default
analysis profile. Use argument capture selectively and retain the overhead caveat.
Profiler setup here is temporary; no profiling code is proposed for production.

Two initial attempts failed before yielding a usable full-chain profile:

- [52211c22](https://app.buildbuddy.io/invocation/52211c22-dd2e-4068-bfb0-ae23e72b4e7b)
  had duplicate `rustc_env` keywords from instrumentation. Formatting checks did
  not reject that duplication; checking parsed keyword uniqueness caught it.
- [263f6bff](https://app.buildbuddy.io/invocation/263f6bff-84ec-4573-b161-1868874790bd)
  used `-Ztime-passes`, whose plain stderr broke the pinned rules_rust metadata
  wrapper's JSON parser after compilation. Use the supported binary
  `self_profile` output group instead; do not combine plain pass timing with
  that metadata parser.

The successful profiling runs build selected targets without lint aspects; they
do not run behavioral tests or constitute exact-head full-CI validation. The
findings PR contains only this note and summarized evidence, with file-scoped
formatting checks. It changes no executable behavior.
