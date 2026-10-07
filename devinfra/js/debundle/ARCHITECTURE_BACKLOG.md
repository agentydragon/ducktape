# Debundle Architecture Backlog

Current architecture-level follow-ups for `devinfra/js/debundle/`. This file holds design details; dispatch order lives
in <TODO.md>. Resolved items are deleted, not struck through.

## Architectural pressure points

1. **Finding the right owner of a behavior is laborious.** `BUILD.bazel` has over 1,200 lines of fine-grained Rust
   targets; `pipeline.rs`, `artifacts/artifact.rs`, `spec/spec.rs`, `selectors/resolution/selector_resolve.rs`,
   `selectors/matching/chunk_facts.rs`, and `peel/quotient.rs` are each over 1,000 lines. This is not a blanket request
   for smaller files: split interfaces where there is already a stable phase boundary, keep the public API and Bazel
   targets navigable, and avoid creating a maze of one-function modules.
2. **The same words name different layers.** See [Vocabulary / naming debt](#vocabulary--naming-debt) below. Fix the
   highest-friction local ambiguity first; avoid a project-wide rename that changes spec, report, or CLI wire formats.
3. **Graph and lowering boundaries are permissive.** The owner graph, incremental quotient, realizability index, and
   emitted schedule have related but separately maintained state. Broad crate-internal visibility makes cross-phase
   invariants hard to audit. Prefer explicit phase inputs and checked constructors over a bulk visibility or
   collection-type rewrite.

## Open backlog

Re-check file paths and line numbers against current `HEAD` before acting; this file intentionally describes shapes
rather than frozen review line references.

### Emission internals (intentional low-level boundary)

`EmissionFiles` owns finalized lowered/pass-through files and the matching output indexes. Post-strip validation, final
export validation, the rename queue, and tree/harness script writing now require `&EmissionFiles`, so their callers
cannot accidentally pass prepared or stale files/indexes. Body-only transforms still use `rewrite_bodies`'s checked
`(ChunkBundle, ArtifactIndexes)` closure: those algorithms also operate on prepared/source chunks in their tests and the
closure enforces unchanged indexed layout. Do not rebuild output indexes after body-only rewrites; lowering changes
entry paths and creates modules, so its one post-lowering index build is necessary. Full-swap exclusions and the
post-strip gate remain mandatory.

### Post-strip consumer scan retirement condition

`vendor/mod.rs::validate_partial_swap_consumers` was kept at the end of the 2026-06 vendor collapse: lowering can
synthesize consumer directives inside materialized module bodies (`BindingKind::Imported` re-export imports in
`lowering/lower.rs`, `export … from` re-exports in moved bodies) with no live rewrite at the construction site, and the
plan-time gate's input-space enumeration cannot see them. Retire the scan only after (a) those construction paths
consult the `VendorResolutionPlan` (live rewrite or plan-time rejection) and (b) e2e fixtures pin the
synthesized-directive shapes failing without the scan.

### `owner` → `node` rename (deferred)

Folding `OwnerId`/`OwnerIdx` into one `NodeId`, renaming `OwnerGraph*` → `NodeGraph*`, and the wire ids `owner:N` →
`node:N`. Deliberately not done in the naming/identity sweep: "owner" is a coherent, pervasive term (~1100+ uses) and an
owner genuinely _is_ a graph node, so a half-rename worsens consistency while a full rename breaks the wire format and
diverges the frozen `props/specimens` snapshot. Revisit only as a deliberate wholesale rename.

## Vocabulary / naming debt

- **`ModuleId(pub LogicalModuleIndex)` wraps an index in another wrapper.** Clarify which indices are stable within a
  chunk before changing this identity model.
- **`chunk` and `artifact` do not tell you which phase owns a value.** `ChunkBundle` / `ChunkArtifact` / `JsChunk`
  describe stored files; `ChunkAnalysisReport` is a manifest/report; `ChunkFactorization` is a validated
  graph-and-assignment product; `MaterializedLogicalChunk` is a lowerer result. Use phase-qualified names for _new_
  boundary types (`PreparedChunk`, `PlannedChunk`, `EmitFileSet` as appropriate), not a mass rename of existing report
  fields or published artifacts.
- **`materialise` / `materialize` coexist.** `pipeline.rs` calls its local selection `materialise_chunk_ids` while the
  API and spec use `materialize_logical_modules`. Standardize the local spelling when editing the pipeline. `peel`,
  `factor`, `atom`, `owner`, and `quotient` are distinct domain concepts in `docs/design.md`, not synonyms to normalize
  away.
- **Anonymous ownership is implicit.** `OwnerNode` with empty `declared` becomes an anonymous statement in some callers,
  while materialization carries `anonymous_statement_ordinals` and a sentinel `ModuleId`. Consider an explicit kind or
  destination type after first pinning its invariants; see the discussion below. Do **not** casually rename `OwnerId` to
  `NodeId`: that breaks wire IDs (see deferred item).

## Duplicated calculations

### `tarjan_scc` over the module quotient: residual walks

The module-quotient pipeline currently has two broad Tarjan consumers:

1. `check_realizability` runs Tarjan and exposes only the unrealizable (multi-module) SCCs on the verdict
   (`unrealizable_sccs`), which `validate_factorization` consumes.
2. `ChunkFactorization::build_with` caches `dep_graph_sccs`, which the materializer/emitter path and
   `report_builders::build_quotient_scc_reports` read.

Remaining legitimate walks (different graphs): `validation.rs::compute_realizability_cut` (FAS iteration, intrinsic),
`graph/build.rs::promote_at_init_calls` (closure fixpoint), `atomic_units.rs::compute_atomic_units` (constraining-edge
owner SCC).

Do not consolidate these walks solely because they all compute SCCs. The verdict distinguishes constraining edges from
the full import graph (including lazy back-edges); the reported module quotient has its own edge/node contract. Any
reuse must first establish identical edge sets and isolated-node handling, and preserve the gate's two-pass semantics.

## Encapsulation + module boundaries

### `pub(crate)` on internals is broad

`OwnerGraph` fields are private, but `RealizabilityIndex` holds owner-edge references, `IncrementalQuotient` maintains
bucket state derived from the owner graph, and `OwnerGraph::from_report` reconstructs it from JSON. The _crate-internal_
invariant surface is still large: several consumers rely on conventions rather than a type boundary that makes invalid
operations impossible.

## Test-vs-spec drift

### Defensive comments should stay tied to a real invariant

`graph/linker_order.rs::chunk_source_import_order_from_adjacency`'s `None`-after-`Some` clause is "kept for robustness
against future filter changes that might admit non-constraining members". If the filter shape changes, either turn this
into a tested invariant or delete the defensive branch.

## Remaining refactor opportunities

- **Shallow versus full fact extraction.** `program_analysis.rs` builds manifests and rewrite facts for every chunk;
  `facts/` performs owner/graph analysis only where needed. Share classifications, not necessarily traversals: preserve
  the cheap pass-through path and source-order/unsplit-declaration semantics.
- **Entry lowering orchestration.** `lowering/lower.rs` still combines entry import disambiguation, rename sealing and
  entry export planning. Extract only where a narrow interface simplifies callers; per-module output already has its own
  boundary. Production lowering imports are explicit; wildcard imports remain only inside test modules, not as
  cross-phase dependency plumbing.
- **Graph representation boundaries.** Clarify the domain graph → counted graph → realizability index contract and
  constraining versus non-constraining edges. Collection changes need profiles and an explicit determinism contract, not
  a blanket BTree-to-hash rewrite.

Large-file moves alone save no code. Stable seams worth evaluating remain in `selectors/authoring/selector_codemod.rs`,
`selectors/resolution/selector_resolve.rs`, `selectors/matching/chunk_facts.rs`, `peel/quotient.rs`,
`lowering/rename_ledger.rs`, and `artifacts/artifact.rs`. Define the seam's API before moving files: Bazel target
plumbing can otherwise outweigh the benefit.

Keep semantic test cases and assertions; share setup rather than introducing a new test framework. Invalid-input tests
must still be able to construct invalid inputs. Selector renderers share omitted-run collapsing, but their AST
retention, hole kinds and empty-list policies intentionally remain form-specific.

SWC-reuse decisions and rejected replacements: <docs/swc_reuse.md>. Measured performance work and profiling
prerequisites: <TODO.md> § Pipeline performance and architecture cleanup and `perf/`.

## Concerns to discuss before deciding

### A11 intrinsic integrity: from observed assumption to checked precondition

docs/design.md documents A11 (the chunk runs with unmodified built-in prototypes) as relied on by observation —
prototype pollution defeats every purity-whitelist admission argument and is not detected. A
`compute_shadowed_globals`-style top-level scan over the analyzed chunks for `<Builtin>.prototype.<x> = ...` assignment
shapes would convert the in-corpus half of the assumption into a checked precondition; pollution originating outside the
analyzed chunks (host code, other bundles) necessarily stays an assumption.

### Do anonymous statements deserve a first-class `OwnerKind`?

Today an "anonymous statement" is just an `OwnerNode` with empty `declared`. The materializer
(`lowering/materialize/mod.rs`) special-cases them via `anonymous_statement_ordinals` + an explicit
`anon_residual_sentinel` ModuleId. The realizability gate doesn't distinguish them. Several diagnostics use the
placeholder `<anon stmt #ord>` in `validation.rs`. This is a coherent piece of vocabulary that should perhaps be an
`OwnerNode::kind` variant rather than a sentinel "empty declared bindings". Worth thinking about at the next refactor —
not blocking.
