# Debundle structure and consistency review

Static review of merged Ducktape `0ff6ef0332b272e3add65c0dc7f703a2287e932d`.
The scope is the compiler, its authoring services, saved analysis, and output
reconstruction. This is not a new soundness proof or an executed test result.

The broad architecture is understandable: source preparation and elaboration,
assignment-independent analysis, assignment and realizability checking,
source-order ESM lowering, final reconstruction checks, and output writing.
The largest remaining issues concern what the semantic model can express and
which layer owns a fact. File moves alone will not resolve them.

## Strategic limit: the supported lowering defines which partitions are possible

The design deliberately emits assigned source statements in their original
module-local order, with ordinary ESM imports/exports and no initialization
wrappers or implicit binding pulls (`docs/design.md:1`, `:33`). Cross-destination
rebinding is rejected because imported bindings are read-only; repeated `var`
sites are co-located (`graph/edge.rs:65`). These are properties of the chosen
reconstruction strategy, not merely missing analysis precision.

This supports complete runnable output with conservative ownership, but does
not promise every useful requested partition. For example, separating two
families that both assign a shared lexical binding requires either keeping the
writers together or a representation change that preserves the shared cell.
A different lowering could be valid, but its TDZ, ordering, live-binding and
abrupt-completion obligations need a stated contract and proof.

The current structural-versus-semantic-order classifications are useful
(`assignment_analysis.rs:228`). Use them to distinguish an actual strategy
limit from an imprecise inferred constraint and from a poorly chosen partition.
Do not make a large atomic group the application's architecture by default,
and do not weaken a gate to simulate a lowering the emitter cannot perform.

## Boundaries to preserve

- `AssignmentAnalysis` owns an immutable assignment, quotient, cached verdict,
  and `EsmImportOrder`. The gate and emitter share import order rather than
  implementing independent scheduling semantics (`assignment_analysis.rs:45`,
  `esm_import_order.rs`, `lowering/placement.rs`).
- `CandidateEvaluation` evaluates the authoritative gate and diagnostic
  translation/effect assessments for the same candidate. The effect assessment
  explicitly remains report-only. There is no justification here for bypassing
  the legacy gate (`assignment_analysis.rs:127`,
  `realizability/effect_translation_validation.rs:1`).
- Source and emitted file ownership use different phase types;
  `ReconstructionPlan::validate` produces the immutable `ValidatedPlan` consumed
  by writers. Rejected reading-only output is typed separately
  (`artifacts/emission_files.rs`, `reconstruction.rs:114`).
- Saved analysis now selects explicit saved DTOs and restores SWC identities
  through `ContextOrdinals`. The codec has ordinary named MessagePack/zstd
  semantics; it no longer guesses identity from tuples or changes Serde
  behavior through an artifact scope (`analysis_codec.rs:1`,
  `staged_analysis/analysis_artifact.rs:43`).
- The local execution and strict provider paths already share
  `local_transfer`, allocation transfers, and own-data transfers. Further work
  should extend that kernel rather than inventing another evaluator
  (`facts/sync_effects/local_transfer.rs:1`,
  `facts/sync_effects/strict_provider.rs`).

## A. High: operation execution and complete effect authority remain different models

`SynchronousCalls` contains observed reads/rebinds, fallback provenance, timing
completeness, heap summaries, a separate evaluation ledger, and several proof
receipts (`facts/statement_facts.rs:205`). `EvaluationEffects` currently records
only static global-property may-accesses (`facts/evaluation_effects.rs:1`).
`initialization_effects` conservatively marks global properties, general heap,
and host effects unknown unless an exact completed-owner proof is available
(`facts/initialization_effects.rs:173`). Prepared/classifier purity and completed
purity are also separate judgments with a narrowly scoped discharge operation
(`facts/purity_classification.rs:74`).

These distinctions are necessary today: a completed timing walk or an empty
access set does not prove effect absence. The architectural cost is that an
operation's support must be understood across the AST classifier, ordered local
operations, runtime transfer, owner coverage, and graph fallback. Supporting the
operation in one path can leave it opaque to the authority that decides whether
reordering is safe.

Make the shared ordered operation transfer the producer of composable domain
coverage and proof dependencies. Retain conservative fallback for uncovered
operations and preserve the distinction between observed accesses and proof of
absence. Start with one real extraction blocker and carry its operation receipts
through enclosing calls and owner finalization. Do not introduce a global prefix
fixpoint or replace the gate merely to make the models look uniform.

Validation: real CLI fixtures for local and imported uses of the same operation,
unknown/getter/reentry negative controls, exact owner coverage and dependency
checks, and a same-input downstream constraint/output comparison.

## B. High: exported callables lack a shared initialization-availability judgment

The strict provider kernel can derive summaries for both a function declaration
and a const arrow (`facts/sync_effects/strict_provider.rs:876`). However, general
foreign-call/returned-record summaries are exported only after
`filter(|call| call.hoisted)` in `cross_module_purity.rs:1395`. The code correctly
notes that an initializer may not have executed when an importer calls it.

This is a precision boundary, not evidence of an unsafe acceptance. It explains
why equivalent-looking callable bodies can have different extraction outcomes
depending on declaration form, exported alias, or initialization wrapper.
Recognizing the callable's body is insufficient: body semantics and availability
of the exported binding are separate facts.

Introduce a checked availability judgment for an export at a particular import
use. Keep it separate from invocation/return/effect summaries. Begin with an
immutable initialized provider on a proved acyclic initialization path; refuse
cyclic, early-use, reassigned, or unresolved cases. Trace reexports, namespace
members and initialization-produced aliases through the same identities.

Validation: hoisted and initialized-arrow positive controls, early cyclic use,
provider mutation, namespace/reexport aliases, and provider capture retention.
Removing the hoisted check without an availability proof would be unsound.

## C. High for whole-app output: runtime asset closure has no general contract

`RuntimeSourceRewriter` rebases literal dynamic imports, worker paths, and
`new URL(..., import.meta.url)` using source/target directories
(`lowering/rewrite_runtime.rs:17`). Browser harness preparation validates HTML
entry scripts and preloads and collects existing non-JavaScript files
(`artifacts/browser_harness_plan.rs:37`). Final reconstruction checks replacement
consumers, public exports and unresolved claims (`reconstruction.rs:145`).

These components do not together establish that all required dynamic modules,
stylesheets, worker resources, and external URLs have a represented runtime
destination. A correctly split and accepted captured chunk can therefore still
be insufficient to run the application. Missing capture input and wrong output
URL mapping must remain distinguishable; this review does not establish an
emitter bug for an uncaptured target.

The dynamic-import rebasing test asserts the rewritten specifier and prints the
type of the importing function; it does not call that function and load its
target (`e2e/url_rebase_test.rs:49`). This is useful rewrite coverage, but an
executed lazy-load control is still needed for the broader runtime contract.

Add a runtime-resource model with explicit captured, emitted, replacement,
external, and unresolved destinations. Feed URL rewriting and the harness from
the same model. Keep semantic acceptance and runtime-input closure separate in
the result, including resources that cannot be enumerated statically.

Validation: original and emitted controls that load a real lazy module,
stylesheet and worker/resource URL, plus an explicitly missing-input control.

## D. Medium: frontend and analysis responsibilities still overlap

The analysis projection is not uniformly source-resolved.
`NativeOperationSourceSelector` stores either a raw `SourceMatchClaim` or a
resolved declaration (`staged_analysis/analysis_projection.rs:67`).
`program_summary.rs` reconstructs modules, collects structural source facts,
creates `SelectorChunk`, and resolves source-match premises in Analyze
(`staged_analysis/program_summary.rs:63`, `:979`, `:1105`). Frontend elaboration
separately collects structural facts and solves authored ownership claims
(`frontend_elaboration/elaboration.rs:312`).

The Rust crate graph remains acyclic, but the conceptual phase direction is
mixed: frontend depends on staged analysis for its projection types, while
staged analysis depends on selector resolution and spec compilation
(`BUILD.bazel:1025`). Selector implementation is consequently part of the
compiled analysis closure, including its invalidation identity.

Have elaboration resolve source-site identity for all premise forms across the
required provider closure. Analyze should consume canonical sites, hashes and
contracts, and retain semantic contradiction checks and native-census validation.
Separate projection/contract types from analysis execution so frontend need not
depend on an executable later-stage service. This is a phase extraction, not a
new selector solver or another parse.

Validation: identical resolved sites and premise dependencies for direct
source-match and resolved-member forms, repeated-var endpoints, outside-provider
refusals, and unchanged staged replay under matching input identities.

## E. Medium: ownership selection and residual placement are coupled

`OwnershipSubset::validate` rejects every materialized chunk using catchall-file
placement (`lowering/ownership_subset.rs:76`). `ChunkPlanBuilder::retain_modules`
also requires no residual plan to exist (`lowering/materialize/plan_builder.rs:180`).
This makes two otherwise separate choices interact: which authored claims to
retain, and where the remaining source belongs. A normal app can support a
residual policy while being unusable for the standard passing-subset workflow.
That complicates small runnable controls and pushes authors toward duplicate
configs.

Apply ownership filtering after the complete selector/premise solve but before
residual routing, then use the normal routing policy for every omitted unit.
Preserve anchors, anonymous statements, imported claims, duplicate detection and
full-spec errors. Continue to validate the resulting ordinary assignment; subset
acceptance is not monotone and a catchall can introduce a real cycle.

Validation: full-context subsets under each supported residual policy, including
empty and retained selections, cross-claim anchors, imports and anonymous units.

## F. Medium: source index, execution engine, and semantic evidence need clearer owners

`execution_index.rs` describes AST collection but also owns runtime preparation
and capability inference and declares the interpreter, provider evaluator,
runtime heap, callback and inference modules as its children with `#[path]`
(`facts/sync_effects/execution_index.rs:14070`). `interpreter.rs` consequently
imports its execution data structures from its parent collector and shared
kernel types through `super::super`. Both files are about fourteen thousand
lines; the problem is their mixed roles, not their size alone.

Semantic vocabulary is also owned by `reports/schema.rs`: for example,
`AssumedEffectReason`, execution operations and precision-loss reasons are used
by runtime effects, graph construction and proof coverage. The report schema is
therefore an analysis dependency rather than merely a projection of domain facts
(`facts/sync_effects/interpreter.rs:85`, `graph/edge.rs:13`).

Names also understate responsibilities: `cross_module_purity` produces native
integrity, values, callable capabilities and provider summaries, while `lowering`
also composes analysis, assignment and diagnostic output. Name these services by
their actual contract as their responsibilities are separated; renaming files
alone would leave the dependency direction unchanged.

Make the immutable execution program/index, runtime store, transfer kernel and
provider/application adapters explicit peers. Move semantic evidence to its
domain owner; report DTOs should project it. Do this by extracting one stable
interface at a time, preserving the existing shared transfers.

Promoted dependency edges particularly need an accessible derivation reference:
`EdgeRole::PromotedAtInit` names a callee source unit, while physical source,
fallback reason and proof information live in other records
(`graph/edge.rs:21`, `graph/build.rs:2111`). Existing evidence is richer than the
edge alone; expose its connection rather than treating a promoted owner as the
literal source operation. Keep complete provenance separable from bounded report
presentation.

## G. Lower: identity and serialization boundaries have small remaining inconsistencies

`LogicalRequest` and `ModulePlan` still use qualified strings, while graph
destinations use `ModuleId`, wire destinations use `ModuleKey`, and callers
normalize through `ModulePath`. Two separate `logical_module_path` helpers split
`chunk::path` strings (`spec_resolution.rs:17`,
`selectors/resolution/selector_resolve.rs:2625`,
`lowering/materialize/plan_builder.rs:38`). `ModulePath::parse` also folds case
and accepts qualified historical spellings, so it is a canonical authoring
identity rather than a physical output-path type (`spec/spec.rs:2034`).
Use a typed chunk-qualified module reference between services and format it only
at external boundaries. Keep report entry labels and filesystem locations out of
that identity. The distinct source-unit ordinal, graph ID and raw statement ID
types already demonstrate the right idiom.

The explicit saved/runtime split is substantially complete. A small cleanup
remains: runtime binding-effect identities/coverage still derive Serde even
though the artifact uses their saved counterparts; a runtime receipt's JSON is
also used in an internal before/after invariant test
(`facts/sync_effects/effect_coverage.rs:39`,
`facts/statement_facts.rs:907`). Remove unnecessary identity-bearing runtime
Serde capabilities after moving that assertion to a typed comparison or report
view. Continue reusing genuinely identical identity-free data in saved DTOs.
This does not warrant a macro or another format migration.

## Suggested slices

1. Add checked export availability for one bounded initialized-provider class
   (B), with the existing local kernel and cyclic negative controls. Require
   an ordinary-gate extraction and executed original/emitted behavior control.
2. Independently resolve source-site premises at the frontend boundary and
   separate projection contracts from analysis execution (D). This is the
   most bounded structural cleanup and makes the intended phase contract concrete.
3. Compose one operation's domain coverage through real owner execution (A),
   measuring the downstream extraction payoff before broadening the work.
4. Make ownership subsets independent of catchall routing (E), enabling cheap
   ordinary-gate runtime fixtures without parallel app specs.
5. Extend runtime-resource modeling and original/emitted lazy-load checks (C)
   as a separate end-to-end capability, then perform the organizational and
   identity cleanup in F/G around the interfaces those slices establish.

Flat/tree specs have a clear authoring-to-canonical relationship, the current
graph codec has a strict format, and legitimate compact/detailed YAML forms are
not by themselves legacy support debt. Preserve the existing soundness checks
and exact saved-artifact identities throughout these changes.
