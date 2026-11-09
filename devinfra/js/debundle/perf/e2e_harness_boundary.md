# Debundle E2E harness dependency split

The merged [harness split](https://github.com/agentydragon/ducktape/pull/14402)
avoids 111 of 190 test-driver recompiles and their Clippy checks after an
analysis-only source edit. Driver compile-and-lint CPU fell 51-54%. The complete
E2E workload took 316.916s against controls of 335.651s and 320.719s: only
3.803-18.735s (1.2-5.6%) shorter. This is a compute improvement, with a small and
uncertain elapsed benefit; it does not establish a 25-50% CI latency improvement.

Measured 5 November 2026 UTC (4 November Pacific). This is a bounded compiler
invalidation experiment for the harness change, not a new estimate of fleet PR
feedback latency. [Structured measurements](e2e_harness_boundary.json)
include source revisions, temporary patch hashes, engine output digests, native
action totals, and the verified test-roster digest.

## Hypothesis and preserved coverage

The shared E2E harness compiled against `analysis` and `artifact`, so an analysis
change rebuilt all 190 nonmanual E2E drivers and reran their Clippy checks.
Keep process/CLI/Node fixtures in `:support`, and move typed graph readers,
assertions, and `GraphFixture` into `:graph_support`. Its 77 consumers opt in
through normal deps and imports; other direct production dependencies remain
explicit where used.

The macro still includes the real `:debundle_e2e` executable as runtime data and
sets `DEBUNDLE_TV_ORACLE=1`. Existing graph export options, assertions, and graph
purity-disagreement guards remain. Source review found no test body changes
after normalizing imports and the moved helper namespace. Comparing BUILD ASTs
found only the new graph-support target and the 77 additional graph deps; test
data, env, tags, and roster were unchanged.

## Reproduction

Control source: `1793d427db3184b85fb2b314f8813febcf92e7a0`.
Candidate source: `35ba3faab463d4b651aa1308bda6421d4ea4c3be`, based on that control.
Run each arm through the normal hosted `bbr --script` mechanism, using Bazel
9.2.0 and the source-pinned RBE container:
`ghcr.io/agentydragon/rbe-container-image@sha256:c73af98850208e2a898120f9992f4cb8c320c488bc3f3c8607cc06ace96be3c5`.

1. Warm the arm with `bazelisk build`, the common flags below, and
   `//devinfra/js/debundle/e2e/...`.
2. Temporarily append `pub const CI_HARNESS_BOUNDARY_BENCH: &str = "<arm>";`
   to `devinfra/js/debundle/lib.rs`, the **analysis crate only**. Use a unique
   value for each arm.
3. Run `bazelisk test` with the same flags, `--nocache_test_results`,
   `--test_output=errors`, and the same E2E pattern. Normal lint stays enabled.

Common flags (replace `<pinned-image>` with the image and digest above):

```text
--config=ci
--remote_default_exec_properties=container-image=docker://<pinned-image>
--build_tag_filters=-manual
```

The unused public constant changes Rust metadata and archive content without
changing tested behavior. Verify distinct `.rmeta` and `.rlib` output digests
through `bb execution get`. Each arm uses a different value to avoid reusing
another arm's changed-engine outputs. This approximates an analysis-only engine
edit; it is not a timed production bug fix. Keep other compiled inputs cached.
Do not use a broad per-crate flag matching all E2E drivers, which would force the
recompilation this experiment is intended to avoid.

Fetch native invocation/export/execution data and the Bazel critical-path tool
log with `bbapi`. Restrict compute totals to NEW links (`invocationLinkType=1`)
and deduplicate physical `executionId`. Split test-XML generation from actual
TestRunner execution. Process and CPU sums include parallel work and must not be
interpreted as elapsed feedback time. Compare test summary labels and require
`totalNumCached=0`, one attempt, and success for every target.

## Results

The order was control A1, candidate B1, control A2. Each arm rebuilt the production
CLI and identity once, and executed the same 190 targets once with zero cached
test results and no retries. The engine `.rmeta` and `.rlib` digests differ across
all three arms, preventing the rejected probe's cross-arm artifact reuse.

| Arm                                                                                       | Inner test elapsed | Action critical path | Fresh E2E driver Rustc | Fresh driver Clippy | Fresh tests |
| ----------------------------------------------------------------------------------------- | -----------------: | -------------------: | ---------------------: | ------------------: | ----------: |
| [Control A1](https://app.buildbuddy.io/invocation/1edd1655-2d13-49a6-ad9f-038b2ac0f377)   |           335.651s |              272.22s |                    190 |                 190 |  190 passed |
| [Candidate B1](https://app.buildbuddy.io/invocation/f57c331e-1c05-402d-80fd-a3da8f937245) |           316.916s |              262.41s |                     79 |                  79 |  190 passed |
| [Control A2](https://app.buildbuddy.io/invocation/e74d0d33-999a-4ef8-9ecf-3eaca0b12f43)   |           320.719s |              240.25s |                    190 |                 190 |  190 passed |

Driver Rustc + Clippy CPU was 2,744.693 / 1,350.950 / 2,905.481 CPU-s in that
order. The candidate retains all 77 graph consumers and two additional tests
with direct production-analysis dependencies: `purity_hint_diagnostics_test`
and `simulator_node_differential_sweep_test`. The other 111 drivers reused their
compiled outputs. Both support libraries were explicit wildcard roots, including
their normal Clippy and rustfmt checks.

No driver compilation is on the recorded action critical path in any arm. The
candidate still has the serial path through analysis metadata (59.15s), artifact /
program analysis / preparation / staged analysis (70.25s), fingerprint generation
and its metadata (36.69s), CLI library and final executable compilation (65.16s),
then a 31.16s integration test. These selected action lifetimes sum to the
262.41s action critical path; they are not independent savings estimates.

The candidate critical path lies inside the controls' 240.25-272.22s range.
Reducing parallel compiler work did not remove that serial production chain.
For a large elapsed improvement, prioritize production dependency boundaries and
the fingerprint-to-CLI compilation tail, then measure the complete workload
again. The harness split alone is not evidence for that larger improvement.

Changed-file pre-commit and focused basic/typed-graph fixture tests passed.
All three required PR checks passed at implementation commit `35ba3faab4` before
merge as `cfae05e6576b0d026c6187e1ce9d25b8ad1499b1`. Other production changes
landed between the frozen benchmark base and the merge; these measurements isolate
the harness change and do not measure current `devel` or a post-merge CI cohort.

The elapsed clock starts at the inner Bazel test command. It excludes the warm
build, checkout, outer runner startup, GitHub scheduling, and other required PR
gates. This experiment does not measure cold builds or unrelated PR classes.

## Rejected compiler-flag probe

The first experiment used different unused `--cfg` flags on `:analysis` instead
of a source constant. All three arms passed all 190 targets, but the flags
produced identical engine `.rmeta` and `.rlib` digests. Later arms reused
downstream compiler outputs from earlier arms, so their elapsed comparison is
invalid. Distinct action keys alone did not establish distinct output contents.

| Arm                                                                                       |  Elapsed | Fresh E2E drivers |
| ----------------------------------------------------------------------------------------- | -------: | ----------------: |
| [Control A1](https://app.buildbuddy.io/invocation/140f58fd-afcf-41f1-a576-ac8c1f8201f7)   | 245.685s |               190 |
| [Candidate B1](https://app.buildbuddy.io/invocation/950197ba-f1f0-462c-8786-ab572dff63ef) | 148.940s |                79 |
| [Control A2](https://app.buildbuddy.io/invocation/a35c51c9-8a80-420e-8783-912795dde541)   |  96.662s |                 0 |

Do not quote these elapsed differences as an intervention speedup. The shared
engine digests were `.rmeta` `83167142197f08207685c94b26978a0ba3426e8b9ad302d5aff6fb71c27f19f0`
and `.rlib` `a13d1b6298db998eb5f0c2e979620feecf720ec4c66b17c36d4ee390164a57b3`.
