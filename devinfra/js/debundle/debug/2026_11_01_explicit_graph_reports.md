# Explicit full graphs and report-only output

## Cause and boundary

At base `b5515358a71c1720c9f9d8c45da3dc3d12357a16`,
`split_dependency_graphs=True` made a second output directory of the SAME
`DebundlePipeline` action. It moved full graphs after generating them, so the
default build still serialized and uploaded them. Output grouping alone did
not make the default computation small.

Full graph selection is now explicit (`run --graphs`) and independent of
report timing, effect JSONL and diagnostic evidence detail. The canonical
graph/facts, assignment validation, legacy/effect TV, premise checks and final
program validator still run. Only graph report projection/writing is omitted.
`graph_summary.json` retains exact counts and gate/legacy-TV verdicts without
node/edge tables. It is not a substitute input for graph-backed queries.

`RunArtifacts::ReportsOnly` requires an Analyze artifact and a fresh output
root. It reaches the same accepted `ReconstructionPlan::validate`, or the same
`RejectedPlan`/typed exit-3 boundary for a readable rejection, and writes no
program/browser/vendor outputs. Explicit inconsistent roots and dry-run/report
mode combinations fail. The rejection marker describes the actual output.
Unresolved claims remain final-validation errors, not successful exports.

## Separate on-demand Bazel action

DefaultInfo depends only on the ordinary .out. With split_dependency_graphs,
the dependency_graphs group selects a separate `DebundleExportGraphs` action.
Both actions consume the exact SAME Analyze output File, source/spec inputs
and package roots. Export loads the artifact, runs canonical materialization
and validation, and writes full graphs plus run diagnostics. It does not call
Analyze, copy an alternate source census or force full effect JSONL.

Real public fixture controls use
`//devinfra/js/debundle/pipeline_rule_test:jsonl_readable_default`:

```sh
bazelisk build <fixture> --output_groups=default,analysis --remote_download_outputs=toplevel
bazelisk build <fixture> --output_groups=dependency_graphs,analysis --remote_download_outputs=toplevel
```

The [default build](https://app.buildbuddy.io/invocation/02907cd6-fa2d-47ed-89b0-ac3f5163946d)
executes ProjectAnalysis, Analyze and Pipeline. Its tree has graph_summary and
emitted modules, with no dependency/program graph. The
[explicit export](https://app.buildbuddy.io/invocation/2fc1501e-a0e5-45bb-ad78-7815b2f1d4b5)
executes only ExportGraphs among Debundle actions (standard workspace status
also runs), reusing Analyze SHA-256
`5d223b6e45029d9a19cf883b0023777611136a313a82243139fb576089d28769`.
The exported graph's relative program_graph reference resolves, and no app
tree is emitted. BEP records and the input/output manifest are preserved under
`/tmp/debundle-cutover-20261101/graph-selection/action-proof.json` and sibling
`*-build.bep.jsonl` files. This neutral fixture is not a corpus benchmark.

One initial build failed with an upload digest mismatch because a source
comment changed while its tool compilation was uploading inputs. The retry
above used frozen source and passed; no infrastructure workaround was used.

## Public consumer controls

Graph-reading fixtures and snapshot regeneration request graphs explicitly.
The raw run helpers remain graph-free so defaults can be tested. Graph-query,
edit-gate, snapshot and provenance consumers retain their existing graph
identity checks. The shipped isolated-edit snapshot example opts in; vendor
and runtime-only examples do not need full graphs. Adapters must distinguish
normal regeneration from explicit graph refresh.

The real CLI compares small/default and full output, including Node output,
counts and verdicts. Direct/staged full graphs match. A separate report-only
export from the same artifact preserves those reports while omitting per-file
emission metrics. Controls retain typed rejection/markers, absent graph-path
claims when no graph exists, analysis requirements, conflicting-root refusal,
dry-run suppression and final unresolved-claim rejection.

Compressed full-graph encoding/readers are inherited from the merged exporter
change. Other diagnostic detail budgets remain independently selected. This
selection patch does not claim all reports are bounded or that it removes
internal graph computation.

## Original-base focused checks

[Final original-base checks](https://app.buildbuddy.io/invocation/30f92598-56a1-4524-9f28-60377acc11be)
pass all 27 named targets/aspects and 20 suites: pipeline/lowering/CLI units,
four pipeline action controls, graph/staged/provenance/snapshot/gate/edit and
JSONL CLI controls, raw vendor/standalone defaults and both shipped Python
examples. Normal pinned hooks pass. The owning evidence note is the only
source addition after those checks; production and test inputs are unchanged.

## Rebased integration checks

The first rebase onto `dcec89735235e6632550ad29f8e297b08257e5a0`
inherits the compressed graph exporter and plain/gzip readers from
`9b890485c79e45da4b63bd02992e6e91ba319ba2`.
[Combined gzip checks](https://app.buildbuddy.io/invocation/5dc9295b-a9f4-468b-bd6e-e29461d07a3d)
pass all 30 named targets/aspects and 22 suites, including compressed query and
snapshot controls. The actual Bazel action executions above remain original-base
evidence; the action and final-validation policy is preserved through this rebase.

The final rebase onto `a887d7c90df5f53231de4564306408d58007f6bd`
also inherits bounded translation-validation reports from
`2f3c8e0c734c2c42d0cca93cb82aed77bf1ee3f1`. Its exact pair set, evidence
visitor and sink flush remain unchanged; graph-summary acceptance reads the
existing result report. Normal Pipeline retains full-report and chunk attrs.
ExportGraphs excludes both full-report/chunk and effect-conflict JSONL flags,
so a graph-only output-group request does not duplicate full diagnostic streams.
Six action controls include this independence, shell quoting and SAME Analyze
artifact identity.

[Final combined checks](https://app.buildbuddy.io/invocation/7fb7a950-4073-4894-9ff3-07b3587c610a)
pass all 33 named targets/aspects and 25 suites, including the six action
controls and translation-validation differential controls. Normal pinned hooks
pass. The subsequent rebase onto pin-only
`f684931c6e` preserves the exact debundle subtree of the tested base. Only this
evidence note changes after the combined checks; production and test inputs
remain identical.

## Export-reentry landing race

C2 merged as `28628668de` immediately before PR creation, so the initial PR
head was conflicted. The rebase preserves its real `ExportReentry` full-report
kind, allowed-kind validation, per-chunk full sink and borrowed bounded summary.
The graph action continues to exclude all full-report kinds/chunks and JSONL;
the ordinary Pipeline retains them. No source fact or gate rule changes.

[Reconciled checks](https://app.buildbuddy.io/invocation/cc8a7912-6b61-4842-b4c5-12b471b1cbf5)
pass all 36 named targets/aspects and 28 suites, including seven action controls,
export-reentry/closed-loader controls and the prior graph/staged/query/snapshot
and final-validation matrix. Production/test source is `832beb653a`; this
checkpoint is a separate docs-only follow-up. Normal pinned hooks pass.
