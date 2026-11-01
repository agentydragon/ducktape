# Composed static global-property may-accesses

## Boundary

This producer was first checked against the merged provider/JSONL source at
`92b786cc3a8e0664fc2583fc51f8781436a07484`, whose debundle content matches the
green combined head `1815cc7d75825b325210ff558a30a6d7c9aa67fa`.
The single patch was then rebased onto devel
`9798a20d91b1e19fed766af33ee6952a3593dde6`, including the conservative getter
model (#13444), strict expression transfers (#13460), and prefix binding
expansion deduplication (#13456). Their behavior stays intact. The integration
adapts three new getter member matches to the retained site field and keeps
the strict expression variants and conditional traversal.

The existing canonical execution index retains member spans during ValueExpr
lowering and assignment spans in typed member-write records. A physical
operation is identified by its containing chunk, lexical source-unit ordinal,
full span, read/write kind and static property. Every chunk has its own SWC
SourceMap, so span coordinates survive the existing analysis codec. Synthetic
destructuring projections have no physical member site and provide no new
access or absence certificate.

RuntimeResult carries a report-only ledger beside every payload. The executor
joins it before returning Values, HeapResult, callbacks, models or Outcome.
Literal evaluation still uses the shared transfer kernel. An invocation memo
stores only that invocation's ledger, including its children; replay joins
that ledger idempotently. The caller's conservative assumption tags the
incoming result, independently of which payload fields it consumes. Reaching
one site through flattened calls, final assignments, exported values and memo
replay therefore retains one joined record per physical operation/cell.

Proof dependencies are conservative unions of prefix/query conditions, not a
minimal dependency proof. Explicit runtime assumptions and property proofs,
plus dependencies of consulted preparatory purity facts and their pure
callees, survive Values consumption and prefix publication. This does not
change purity classification or any completed-purity publication boundary.

## Consumer and retained gaps

The terminal ledger stays with SynchronousCalls in the staged facts.
StatementFacts::initialization_effects projects its GlobalProp reads/writes
into the existing report-only TV consumer. Legacy direct `global_reads` /
`global_writes`, graph construction, source/emitted order, acceptance and
pair-premise classification are unchanged.

Every new operation is a may-access. Prejoined assignment/property lanes,
conditional source and flattened calls establish no definite execution.
Cells supplied only by this producer have `indexed_may_access` evidence and
cannot certify an observed contradiction. Existing observed cell evidence
keeps its status while retaining the additional may-sites separately.

All existing unknown global/general-heap/host coverage and Havoc remain.
Simple static-key member reads/writes are this tranche's useful producer
slice. Dynamic keys, unresolved receivers, update/delete and native writes,
ordinary heap/host/throw coverage and source-order completion remain gaps.
Node's `global` is currently an alias in `census_globals`, but not in execution
`global_object_names`; even under `predefined_globals: standard` its execution
receiver remains opaque. The negative CLI control preserves this boundary.
It would require a separately reviewed value-domain extension to produce
positive GlobalProp facts for that alias.

Default samples retain twenty source sites per cell access, with exact counts
and explicit truncation. Full JSONL serializes borrowed evidence and keeps all
sites without constructing a second full provenance list per pair. The
existing output-sensitive sweep and its per-pair cell aggregation are shared.

## Checks

The named-libraries check runs normal RBE and lint aspects for `analysis`,
`gate`, `lowering`, `debundle_e2e` and `e2e:support`, as well as their named
analysis/gate/lowering unit suites and three public CLI suites:

```sh
bazelisk test \
  //devinfra/js/debundle:analysis \
  //devinfra/js/debundle:analysis_test \
  //devinfra/js/debundle:gate \
  //devinfra/js/debundle:gate_test \
  //devinfra/js/debundle:lowering \
  //devinfra/js/debundle:lowering_test \
  //devinfra/js/debundle:debundle_e2e \
  //devinfra/js/debundle/e2e:support \
  //devinfra/js/debundle/e2e:effect_translation_validation_test \
  //devinfra/js/debundle/e2e:staged_analysis_test \
  //devinfra/js/debundle/e2e:translation_validation_differential_test \
  --test_output=errors
```

[Original-base final focused invocation](https://app.buildbuddy.io/invocation/0bafb8bc-76c8-451d-9c2e-4dd69fe871b1):
all eleven named targets and six suites pass. CLI coverage has 24 cases:
12 effect controls, 7 staged controls and 5 legacy differential controls.
The final run executes five suites and reuses the passing analysis suite.
The [preceding run](https://app.buildbuddy.io/invocation/7290e6c5-efaf-4521-87eb-41d27003a96f)
also passes; the final source additionally preserves an existing provenance
truncation flag when sampling an already sampled value.

Controls retain exact lexical owners and byte spans, nonempty native proof
dependencies, idempotent sites across nested returns and repeated evaluation,
default-export evaluation, alias unions, ordinary receivers whose parameter
is named globalThis, opaque-callback assumption isolation, unchanged Havoc,
and Node output. A 24-write control retains all 24 sites in JSONL and exactly
20 in the default sample. Direct/staged runs round-trip the same sites,
dependencies, full streams and reports. The target-architecture sweep
paragraph was corrected to describe output-sensitive reporting and the
pair-specific premise boundary.

Early controls exposed two fixture assumptions: an unused native premise
contributed no dependency, and the census's Node alias was not an execution
alias. The native control now uses the existing prototype-write census shape
that actually consumes its truthful assertion; the Node case stays negative.
Neither required broadening a semantic model. The analysis unit build also
caught two synthetic member constructions needing qualified dummy spans.

### Rebased integration checks

After rebasing onto `9798a20d91b1e19fed766af33ee6952a3593dde6`, source commit
`41ec650751f5a76a4d63002bad265582758a9e9e` passes the same eleven named targets
plus `//devinfra/js/debundle/e2e:foreign_call_summary_test` in
[BuildBuddy](https://app.buildbuddy.io/invocation/8666fb7e-f579-4059-a509-8081176a4d67).
All seven suites execute and pass: the 24 effect/staged/differential controls,
21 foreign-call controls, and the analysis/gate/lowering unit suites. Normal
named library Clippy aspects pass. The following update records these results
only; production and test source match that checked commit.

The range-diff against original-base commit
`fdad9b84c6ffb35482a47cff2855d674c8a59778` contains only the three new getter
member-match arity updates, context shifts from the preserved strict
expression implementation, and this evidence note. There is no dependency on
the separately developed execution-purity foundation.

## Original-base old-producer control

The identical neutral source/spec live under
`/tmp/debundle-cutover-20261101/tv-effects/ledger-control`; `comparison.json`
retains input/engine hashes and the complete new cell evidence. Source SHA-256:
`2ca652c5506ddc62d2147744bf013f3eb0cc913ce9eb03de3784ef89ef37bf6a`.
Spec SHA-256:
`99cdf9dbc2895e4366110e23486b2ec1213bff0f5b74f1e25e018f7b28649e82`.

The optimized baseline engine is source `1815cc7d75825b325210ff558a30a6d7c9aa67fa`,
SHA-256 `201dccb8ed07783f577a821edd4cd89cf223ad637788b93fed3b8bcc70f8c35a`,
built in [BuildBuddy](https://app.buildbuddy.io/invocation/30232643-1651-4e6c-b7dd-e78787f4fc39).
The original-base candidate CLI is the checked local Bazel source, run in
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/4b4f1b35-ec69-40c1-8036-78315ca9cb95).
Both execute `run --spec <same-spec> --effect-conflicts-jsonl` and exit 1 for
the same existing constraining cycle.

The baseline reports one Havoc pair with zero cells. The candidate reports
that same pair plus the expected marker GlobalProp cell: a callee read/write
and another callee write, each with its original source site. The read keeps
the consumed `intrinsics_never_written` condition. The new cell is not
observed. Thus the new-cell expectation fails on the old producer and passes
on the new one; this is a report-production regression, not an acceptance fix.
All pair/disposition/Havoc counts and the full coverage report match; only
cell incidence increases from zero to one. After removing command provenance,
dependency graph, legacy TV, cycle, export-reentry and full program/census
graph JSON match exactly. No canonical corpus precision or full TV2 completion
is claimed.
