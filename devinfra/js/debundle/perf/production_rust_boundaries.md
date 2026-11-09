# Debundle production Rust dependency boundaries

These changes remove eleven fresh compile actions after an analysis-only edit and allow CLI/fingerprint work to overlap. The two combined runs took 227.241-229.030s, within the controls' 219.914-263.127s range. The dependency improvements are verified; a reliable elapsed-time win is not demonstrated.

Measured 5 November 2026 UTC (4 November Pacific). This experiment isolates
analysis-only source changes in the full nonmanual E2E workload. Its elapsed
clock starts at the inner Bazel test command, after a warm build. It excludes
GitHub scheduling, checkout, runner startup and other required PR gates.

## Changes

[Fingerprint injection](https://github.com/agentydragon/ducktape/pull/14407)
passes the generated analysis identity from the executable entrypoint through
existing command calls. The CLI library can compile without waiting for the
generated identity; final executables still require both. The configured
compiled-input closure, hash algorithm and artifact mismatch rejection remain.

The independent [artifact-model extraction](https://github.com/agentydragon/ducktape/pull/14409)
moves `ChunkId` and `ChunkTable`
to `source_identity`, and `DepKind`, `StableExportValue` and `PrimKinds` to
`analysis_model`. The artifact library no longer imports the analysis engine
for these types. Definitions and their methods move together; callers use
their owning crate directly. Serialization and analysis algorithms stay the
same, and the existing enum diagnostic test moves with its definition.

## Reproduction

Frozen control: `46ac6921a1bb953861bdb6104a4963700f07a059`.
Use the normal hosted `bbr --script` mechanism with Bazel 9.2.0 and the
RBE image pinned by that source's `devinfra/image_pins.json`.

1. Warm each arm with `bazelisk build //devinfra/js/debundle/e2e/...`.
2. Append `pub const CI_HARNESS_BOUNDARY_BENCH: &str = "<unique-arm>";`
   only to `devinfra/js/debundle/lib.rs`, the analysis crate.
3. Run `bazelisk test //devinfra/js/debundle/e2e/...` with
   `--nocache_test_results --test_output=errors`.

Both commands use `--config=ci`, `--build_tag_filters=-manual`, and
`--remote_default_exec_properties=container-image=docker://<pinned-image>`.
Normal lint and remote cache remain enabled. The source constant changes real
engine metadata/archive contents without changing tested behavior. Every arm
uses a different value; inspect `.rmeta` and `.rlib` digests through
`bb execution get` to rule out cross-arm artifact reuse. An unused `--cfg`
flag does not provide this guarantee, as the prior harness experiment showed.

Fetch invocation, export, execution and critical-path evidence through
`bbapi`. Compare the same 190 test labels, requiring pass, no cached tests
and one attempt per target. Count only NEW execution links, deduplicated by
physical execution ID, and separate test XML generation from real tests.
Parallel process/CPU sums are compute totals, not feedback latency.

The unchanged 190-target roster has SHA-256
`7953a43a6234f95c5beb81aac94e7025db8fe71c9f3d536533ca7d55057f1484`
(sorted labels, newline-separated, with one trailing newline). Every target
has one attempt and one NEW real `test/test-setup.sh` execution in each arm.
Exported cache-count fields have their presence flag unset; this report uses
the actual new executions as evidence that the tests ran. XML generation
actions are counted separately.

## Results

| Arm                                                                                         | Inner test elapsed | Action critical path | CLI outputs uploaded | New Rustc / metadata actions |
| ------------------------------------------------------------------------------------------- | -----------------: | -------------------: | -------------------: | ---------------------------: |
| [Control A1](https://app.buildbuddy.io/invocation/b8e2644a-9906-4dee-9166-a3c23383d7ea)     |           263.127s |              227.21s |             204.285s |                     111 / 27 |
| [Fingerprint B1](https://app.buildbuddy.io/invocation/ff7894fb-f791-458f-9086-f7a781309518) |           210.090s |              182.26s |             171.082s |                     111 / 26 |
| [Control A2](https://app.buildbuddy.io/invocation/a2fee55b-c701-405d-905e-9a6b158412d1)     |           234.074s |              205.75s |             194.142s |                     111 / 27 |
| [Models C2](https://app.buildbuddy.io/invocation/1929fd6b-0bbb-4cc5-adf8-483d00a19d9a)      |           217.121s |              205.54s |             186.097s |                     106 / 22 |
| [Combined D1](https://app.buildbuddy.io/invocation/64cd98c4-a681-4118-a491-dbadc28f97e2)    |           229.030s |              206.17s |             194.713s |                     106 / 21 |
| [Combined D2](https://app.buildbuddy.io/invocation/46861d62-a31a-4284-b2e6-eeacb7b69e0b)    |           227.241s |              193.99s |             192.698s |                     106 / 21 |
| [Control A3](https://app.buildbuddy.io/invocation/996a3b0f-5072-41df-8a0d-d8868549c675)     |           219.914s |              187.21s |             176.561s |                     111 / 27 |

[Structured measurements](production_rust_boundaries.json) contain every successful arm, the distinct engine output digests, source revisions, temporary patch hashes, CPU/process totals, selected native action intervals and verification invocations. All seven arms passed the same 190 targets. CLI output times are worker output-upload completion offsets from the inner invocation start; they exclude outer runner startup.

The two controls bracketing fingerprint B1 took 234.074-263.127s, versus
210.090s for B1. The native compiler timeline confirms 4.298s of overlap
between CLI compilation and fingerprint generation. The generated identity's
metadata compile action is absent, because the CLI no longer consumes it;
the final executable still consumes its compiled library. These are direct
dependency effects. The entire 23.984-53.037s elapsed difference should not
be attributed to the overlap.

The extraction eliminates fresh metadata and archive compilation for five
libraries after an analysis-only edit: `artifact`, `identifier_rename_queue`,
`prune_dead_imports`, `prune_module_exports`, and `validate_emitted_exports`.
That is ten avoided library compile actions. All 79 remaining E2E driver
recompiles and their Clippy checks still occur, as expected; neither change
weakens the full 190-target integration workload or its real CLI/Node oracle.

The first combined run took 229.030s and compiled the CLI binary by 194.713s
(worker output-upload completion). Its action critical path was 206.17s,
inside the controls' initial 205.75-227.21s range. CLI compilation itself took
38.084s, versus 15.405-20.627s in those controls. The model-only run spent
57.347s compiling engine metadata, versus 40.316-41.167s in those controls,
on a different compiler executor. The combined run's engine metadata returned
to 43.133s. Source identities, output digests, task requests, worker identities
and the native action timings are retained in the structured attachment.

The two combined samples took 227.241-229.030s; the three controls took 219.914-263.127s. The combined samples fall inside the control range. This experiment does not demonstrate a reliable elapsed improvement, and does not support the earlier goal of a 25-50% reduction. No further performance arms were submitted after this final combined/control pair.

These are bounded observations on a shared executor pool with a single sample
for each individual change and two for their combination. They do not establish
a fleet PR-feedback speedup or a 25-50% latency reduction. Do not add the two
individual elapsed improvements. Both remove demonstrated dependency work;
the remaining serial analysis, transform/CLI compilation and integration-test
tail still dominate this workload. Behavioral assertions and test-oracle checks are preserved.

## Source and validation boundaries

Fingerprint B1 measures implementation commit
`19a1103a00bb64cdce7ff9c09483fe631f435b6b`. Its normal and E2E binaries and
CLI library [build with lint](https://app.buildbuddy.io/invocation/e765936d-8af5-47b2-8af2-3730195537b0).
The existing [identity mismatch test passes](https://app.buildbuddy.io/invocation/8d414ddb-3c70-4899-97cc-c21d4ca85653),
accepting a matching artifact and rejecting an altered compiled-engine digest.

Model C2 measures `8807fee475112a8ef3099ecf97e8fb96c9502a07`. The combined
arms measure `7a1ac4ed59920753a4d322ea9f4575fb18a12936`: fingerprint B1 plus
the extraction and its unused `serde::Deserialize` import cleanup. Later
extraction changes only repair owning-crate imports in two gate unit-test
modules; they change no test assertions, production algorithms or dependency
edges. The pre-rebase extraction head is `421621dd4db33f37249ae7ca9b9c0bf8604a2aa4`. Its [broad package check](https://app.buildbuddy.io/invocation/cc4fe492-51ae-4790-b703-0990d920819e) names all 40 nonmanual Rust unit-test roots with normal lint and `--keep_going`: all 42 analyzed targets passed, including `gate_test` (15 executed, 27 cached). The preceding [14-library explicit-root build](https://app.buildbuddy.io/invocation/71c18d85-b3e3-4ed6-93bc-2fd37effff6b) and [focused model, analysis, artifact and real-CLI tests](https://app.buildbuddy.io/invocation/1fae6ae4-70c7-4c84-851d-0078e21c7cef) also passed. Manual stress targets remain outside these commands.

The remaining PR was then rebased onto `d5a9c93b92c9f5d07c7e4042258894ada99dc4ff`, after fingerprint injection merged. This also includes the separate saved-summary/runtime-analysis change from [#14406](https://github.com/agentydragon/ducktape/pull/14406). The rebased code head is `895f57f182a6fcade23e3a34ad5682742db497d8`; its PR checks validate that integration. The frozen performance experiment above predates #14406 and does not measure current devel or the post-rebase production combination.

Both PRs pass changed-file pre-commit. A first model warm build at
`2c09e76adbc9dfae35c2720f7f363145b0d1677a`
[failed on a missing import](https://app.buildbuddy.io/invocation/6acd308d-9a0d-4db6-8c16-cda14d48497a)
before the source-delta test. It supplies no performance comparison and is
excluded from the result table. Follow-up ordinary builds/tests found and
repaired the remaining stale and wildcard unit-test imports without adding
compatibility reexports.
