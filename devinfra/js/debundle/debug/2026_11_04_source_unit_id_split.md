# SourceUnitId dependency split, 2026-11-04

`SourceUnitId` belongs to the small `source_identity` crate. Moving the same
transparent `usize` newtype there lets the selector solver compile independently
of the large analysis crate. `SourceUnitOrdinal` remains a distinct type. Existing
analysis-backed lowering and resolution retain their analysis dependencies.

The production solver closure is independent of analysis:

```sh
bbr cquery 'deps(//devinfra/js/debundle:selector_ir + //devinfra/js/debundle:selector_constraint_backend + //devinfra/js/debundle:selector_constraint_model_builder + //devinfra/js/debundle:selector_backend_solver + //devinfra/js/debundle:selector_runtime) intersect //devinfra/js/debundle:analysis' --output=label
```

The [successful query](https://app.buildbuddy.io/invocation/e5f1475c-db6a-4fc0-92f3-c6ec1aa82bfd)
returns no targets. Existing CLI behavior, serialization and test coverage remain
the same; no compatibility re-export is retained.

## Bounded timing comparison

The workload is `//devinfra/js/debundle/e2e:propose_commutations_budget_test`,
including its real CLI build. Control source is
`39bb1710f4ffe69ccce27e373c459b76de27ce36`; candidate source is
`d624882d92cad1f94cc49120038aaf927b6471b2`. Both use ordinary hosted RBE,
the existing cache and executor policy, `--config=nolint`, and uncached test
results. Different unused Rust cfg values force all first-party compiler actions
to execute while preserving external dependency caching:

```sh
bbr --run_from_branch=<branch> --run_from_commit=<sha> test \
  --config=nolint --nocache_test_results --test_output=errors \
  --@rules_rust//rust/settings:per_crate_rustc_flag=//devinfra/js/debundle@--cfg=<unused-marker> \
  //devinfra/js/debundle/e2e:propose_commutations_budget_test
```

The final comparison uses control marker
`ci_source_unit_id_split_20261104_control_fresh` and candidate marker
`ci_source_unit_id_split_20261104_fresh`. Both reuse identity-generation output;
fresh compiler action counts and the comparison results are recorded below.

| Observation                                                         | [Control](https://app.buildbuddy.io/invocation/03490c83-017c-47bf-8218-f5b15b32f206) | [Candidate](https://app.buildbuddy.io/invocation/489fa392-75df-4f44-a8e8-d151200e35b5) |
| ------------------------------------------------------------------- | -----------------------------------------------------------------------------------: | -------------------------------------------------------------------------------------: |
| Bazel `buildTargets` phase                                          |                                                                             304.10 s |                                                                               266.62 s |
| Recorded action critical-path sum                                   |                                                                             301.48 s |                                                                               263.59 s |
| CLI ready from first first-party Rust action, client clock          |                                                                             227.73 s |                                                                               182.76 s |
| Fresh first-party Rust / metadata compilations                      |                                                                              58 / 54 |                                                                                58 / 54 |
| Actual E2E test process                                             |                                                                              72.41 s |                                                                                79.15 s |
| Selector IR metadata start relative to analysis metadata completion |                                                                              +0.66 s |                                                                               -47.24 s |

The observed phase difference is 37.48 s (12.3%), with the CLI ready 44.97 s
earlier. This does not isolate the split's causal saving: selector resolution's
metadata process took 39.65 s in the control and 10.42 s in the candidate,
despite much closer CPU times (7.28 and 5.98 CPU-s). Its reported downloaded
inputs also differed (15 files / 18.58 MB versus 1 file / 24 KB). Action-cache
freshness is matched; worker input-cache state and ambient execution conditions
are not controlled. Do not subtract that variance to invent an adjusted saving,
or treat the 12.3% as an expected fleet improvement.

The mechanism is directly observed: candidate selector IR metadata finishes at
14:50:55.944 UTC, before analysis metadata starts at 14:51:05.380 UTC. Solver
runtime metadata finishes at 14:51:21.904 UTC while analysis is still running.
The candidate's recorded action path now crosses analysis, artifact, program
analysis, vendor, staged analysis, lowering, pipeline, CLI compilation, final
linking and the test; the solver metadata chain is absent from that path.

Action critical-path sums, worker process times and Bazel
phase elapsed time have different clocks; none measures PR push-to-green.
This is one pair under ambient hosted load, with no randomized repeats or
fleet-level claim. The later test-import fix and unused-import removal do not
change the selected CLI workload; its measured candidate remains `d624882d92`.

## Excluded preliminary runs

The [initial control](https://app.buildbuddy.io/invocation/82f58ddb-4264-4499-84c4-5bc363a9d154)
took 285.89 s in `buildTargets`, with 58 new Rust compilations and 54 metadata
compilations. Its identity generation also executed, unlike the final warm-identity
pair. The [initial candidate](https://app.buildbuddy.io/invocation/23ebd14c-a236-40bd-b69f-2cd969529f11)
took 255.00 s, but reused some first-party compiler results (44 new compilations
and 40 metadata compilations). These elapsed values are excluded from the
comparison because their cache states differ. They establish no causal speedup.

## Validation

The selected E2E test passes in each recorded performance run. Ordinary PR CI
identified a missed direct import in `analysis_tests/mod.rs`, hidden by
`use analysis::*`; the [focused repair check](https://app.buildbuddy.io/invocation/3ee86e66-f585-4e07-beca-ca17f50a9175)
passes `//devinfra/js/debundle:gate_test` after adding the direct `source_identity`
import. The repair and stale serde-import cleanup are committed as `dc07bee536`.
Check the current [PR checks](https://github.com/agentydragon/ducktape/pull/14330/checks)
for compilation and lint of the full affected graph; the focused pass alone is
not full CI proof.
