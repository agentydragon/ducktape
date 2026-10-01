# Debundle Architecture Backlog

Current architecture-level follow-ups for `devinfra/js/debundle/`. This
file is an active backlog: resolved items are deleted, not struck through.

## What hurts most (priority order)

1. **Finding the right owner of a behavior is laborious.** `BUILD.bazel` has
   over 1,200 lines of fine-grained Rust targets; `pipeline.rs`, `cli/mod.rs`,
   `artifacts/artifact.rs`, `spec/spec.rs`, `selectors/resolution/selector_resolve.rs`,
   `selectors/matching/chunk_facts.rs`, and `peel/quotient.rs` are each
   over 1,000 lines. This is not a blanket request for smaller files: split
   interfaces where there is already a stable phase boundary, keep the
   public API and Bazel targets navigable, and avoid creating a maze of
   one-function modules.
2. **The same words name different layers.** See [Vocabulary / naming debt](#vocabulary--naming-debt)
   below. Fix the highest-friction local ambiguity first; avoid a project-wide
   rename that changes spec, report, or CLI wire formats.
3. **Graph and lowering boundaries are permissive.** The owner graph,
   incremental quotient, realizability index, and emitted schedule have
   related but separately maintained state. Broad `pub(crate)` and
   `lowering/`'s sibling `use super::*` make cross-phase dependencies hard
   to audit. Prefer explicit phase inputs and checked constructors over a
   bulk visibility or collection-type rewrite.

## Open backlog

Re-check file paths and line numbers against current `HEAD` before
acting; this file intentionally describes shapes rather than frozen
review line references.

### Emission internals (intentional low-level boundary)

`EmissionFiles` owns finalized lowered/pass-through files and the matching
output indexes. Post-strip validation, final export validation, the rename
queue, and tree/harness script writing now require `&EmissionFiles`, so their
callers cannot accidentally pass prepared or stale files/indexes. Body-only
transforms still use `rewrite_bodies`'s checked `(ChunkBundle,
ArtifactIndexes)` closure: those algorithms also operate on prepared/source
chunks in their tests and the closure enforces unchanged indexed layout.
Do not rebuild output indexes after body-only rewrites; lowering changes
entry paths and creates modules, so its one post-lowering index build is
necessary. Full-swap exclusions and the post-strip gate remain mandatory.

### Post-strip consumer scan retirement condition

`vendor/mod.rs::validate_partial_swap_consumers` was kept at the end
of the 2026-06 vendor collapse: lowering can synthesize consumer
directives inside materialized module bodies (`BindingKind::Imported`
re-export imports in `lowering/lower.rs`, `export … from` re-exports
in moved bodies) with no live rewrite at the construction site, and
the plan-time gate's input-space enumeration cannot see them. Retire
the scan only after (a) those construction paths consult the
`VendorResolutionPlan` (live rewrite or plan-time rejection) and (b)
e2e fixtures pin the synthesized-directive shapes failing without the
scan.

### `owner` → `node` rename (deferred)

Folding `OwnerId`/`OwnerIdx` into one `NodeId`, renaming `OwnerGraph*` →
`NodeGraph*`, and the wire ids `owner:N` → `node:N`. Deliberately not
done in the naming/identity sweep: "owner" is a coherent, pervasive
term (~1100+ uses) and an owner genuinely _is_ a graph node, so a
half-rename worsens consistency while a full rename breaks the wire
format and diverges the frozen `props/specimens` snapshot. Revisit only
as a deliberate wholesale rename.

## Vocabulary / naming debt

- **`ModuleId(pub LogicalModuleIndex)` wraps an index in another wrapper.**
  Clarify which indices are stable within a chunk before changing this identity
  model.
- **`chunk` and `artifact` do not tell you which phase owns a value.**
  `ChunkBundle` / `ChunkArtifact` / `JsChunk` describe stored files;
  `ChunkAnalysisReport` is a manifest/report; `ChunkFactorization` is a
  validated graph-and-assignment product; `MaterializedLogicalChunk` is a
  lowerer result. Use phase-qualified names for _new_ boundary types
  (`PreparedChunk`, `PlannedChunk`, `EmitFileSet` as appropriate), not a
  mass rename of existing report fields or published artifacts.
- **`materialise` / `materialize` coexist.** `pipeline.rs` calls its local
  selection `materialise_chunk_ids` while the API and spec use
  `materialize_logical_modules`. Standardize the local spelling when editing
  the pipeline. `peel`, `factor`, `atom`, `owner`, and `quotient` are distinct
  domain concepts in `docs/design.md`, not synonyms to normalize away.
- **Anonymous ownership is implicit.** `OwnerNode` with empty `declared`
  becomes an anonymous statement in some callers, while materialization
  carries `anonymous_statement_ordinals` and a sentinel `ModuleId`. Consider
  an explicit kind or destination type after first pinning its invariants;
  see the discussion below. Do **not** casually rename `OwnerId` to `NodeId`:
  that breaks wire IDs (see deferred item).

## Duplicated calculations

### `tarjan_scc` over the module quotient: residual walks

The module-quotient pipeline currently has two broad Tarjan consumers:

1. `check_realizability` runs Tarjan and exposes only the unrealizable (multi-module) SCCs on the verdict (`unrealizable_sccs`), which `validate_factorization` consumes.
2. `ChunkFactorization::build_with` caches `dep_graph_sccs`, which the materializer/emitter path and `report_builders::build_quotient_scc_reports` read.

Remaining legitimate walks (different graphs): `validation.rs::compute_realizability_cut` (FAS iteration, intrinsic), `graph/build.rs::promote_at_init_calls` (closure fixpoint), `atomic_units.rs::compute_atomic_units` (constraining-edge owner SCC).

**Open follow-up.** The verdict-time and factorization-build-time walks
compute the same partition for different consumers; structurally
consolidatable behind a wider API change, but not urgent and not on a hot
path.

## Encapsulation + module boundaries

### `pub(crate)` on internals is broad

`OwnerGraph` fields are private, but `RealizabilityIndex` holds
owner-edge references, `IncrementalQuotient` maintains bucket state
derived from the owner graph, and `OwnerGraph::from_report` reconstructs
it from JSON. The _crate-internal_ invariant surface is still large:
several consumers rely on conventions rather than a type boundary that
makes invalid operations impossible.

## Test-vs-spec drift

### Defensive comments should stay tied to a real invariant

`graph/linker_order.rs::chunk_source_import_order_from_adjacency`'s
`None`-after-`Some` clause is "kept for robustness against future filter
changes that might admit non-constraining members". If the filter shape
changes, either turn this into a tested invariant or delete the defensive
branch.

## Code refactor / dedup opportunities

Production-code dedup/cleanup options, calibrated by (LOC saved × safety).

**Structural findings (full-package review):**

1. `vendor/mod.rs` further split (~1.6k lines including tests after the
   emission/manifests/passthrough/plan/strip/validate/wrappers extraction):
   package/subpath resolution helpers, export-surface collection,
   `MaterializedOutputChunkIndex`, the shared import factories
   (`DeferredImport` / `IdentRewriteTarget` / `PartialSwapIdentRewriter` and
   the `make_*` constructors), and the post-strip consumer scan are each
   liftable.
2. Two top-level fact traversals remain: `program_analysis.rs::analyze_program_shallow`
   scans every chunk during prepare to build the manifest and determine AST
   retention; `facts/` performs the more expensive owner/graph analysis only
   for chunks that need it. Declaration shape classification is shared via
   `binding_targets::decl_shape`, but the traversals still have separate
   responsibilities. Before folding shallow extraction into facts, preserve
   the cheap prepare path for pass-through chunks and the manifest's
   source-order/unsplit-var-declaration semantics.
3. `lowering/lower.rs` — extract the remaining inline phases of `lower_chunk`
   (naturalization, disambiguation, import planning, the per-module loop);
   each needs substantial captured state from `LowerChunkInputs` (15–20
   fields). Related: `lowering/mod.rs` carries a ~95-line import block from
   wildcard `use super::*` in every sub-module.
4. `artifacts/output_layout.rs` has repetitive `self.root.join(CONSTANT)`
   accessors; a data-driven path helper is possible but low priority. Keep
   named accessors if they make call sites and output contracts clearer.
5. Encapsulation/type design: BTree collections in hot-path graph structures
   (`counted_digraph.rs`, `artifacts/artifact.rs`, `realizability/`) where hash-based
   would be measurably faster — document determinism where it is required;
   make `DepKind`'s constraining vs non-constraining axis
   (`constrains_init_order()`) a first-class type distinction; the three-layer
   edge representation (domain graph → counted graph → realizability index)
   has fragile bridging; `pub(super)` blankets `lowering/` field and function
   visibility.
6. Tests: `e2e/comma_list_owner_split_test.rs` asserts emitted shapes via
   whitespace OR-chains — parse or normalize instead.
7. `ChunkBundle` ownership ping-pong through every stage
   (`artifact = result.artifact`) — cosmetic now that each stage is a pure
   function.

SWC-reuse evaluations (what to adopt, what was rejected and why):
<docs/swc_reuse.md>.

**Real value but needs design work / behavior-risk:**

1. Parameterize the per-form AST holing visitors (`selectors/authoring/render.rs`
   `hole_expr` / `hole_stmt`, `selectors/authoring/minimize/class.rs`
   `hole_class_member`, etc.) behind a `Holer`
   trait or table to collapse repeated per-variant match clusters. ~150 LOC,
   medium risk (over-abstraction hazard; the per-form holing strategies differ
   for good reasons).
2. Migrate `e2e/vendor_swap_test.rs` off raw `serde_json::json!` vendor-mark
   literals onto the typed vendor-mark builders. The raw-`json!` form bypasses
   `FixtureOpts` and the typed `VendorResolutionPlan` constructors, so test
   fixtures can drift from real config shapes without a compile error
   (e.g. a renamed vendor-mark field stays green in tests while breaking real
   specs). Points: <e2e/vendor_swap_test.rs> (~lines 1680, 1821 and the
   `report_out_dir` literals), builder surface in `vendor/mod.rs`.

**Organization only (≈0 LOC removed, navigability win):** split giant
files at existing responsibility seams — <selectors/authoring/selector_codemod.rs>,
<selectors/resolution/selector_resolve.rs>, <selectors/matching/chunk_facts.rs>,
<peel/quotient.rs>, <lowering/rename_ledger.rs>, <cli/mod.rs>,
<artifacts/artifact.rs>. Before moving code, document the exported API of
that seam; Bazel already treats many files as separate crates, so file moves
can otherwise multiply dependency plumbing.

## Quick wins (≤30 min each)

1. **Carry chunk-top-level `Mark` on `ChunkContext`** so `top_level_id`
   lookups do not have to be threaded through every materialize-side
   function as a separate parameter. The `Mark` lives on `LowerChunkAst`
   but is still threaded through several helpers below `lower_chunk`.
   Folding the `top_level_id` helper onto a small `ChunkContext`
   accessor would let helpers take just that context instead.

## Concerns to discuss before deciding

### Entry-file universal-edge approximation

The simulator and emitter now share import-ordering through
`esm_import_order::EsmImportOrder`; keep it that way. The remaining
approximation is narrower: pass-2 candidate SCC enumeration still runs over
the real I-graph, without adding the entry-file residual module's universal
edges. A module that eager-reads an entry-file binding when residual's own
statements never reference that module can therefore avoid candidate SCC
checking. This is a pre-existing inline-mode-only under-restriction; catchall
chunks keep no TDZ-prone bindings in the entry file. Extending candidate
enumeration with the universal entry edges would close it at the cost of much
larger SCCs in the incremental planner path.

### A11 intrinsic integrity: from observed assumption to checked precondition

docs/design.md documents A11 (the chunk runs with unmodified built-in prototypes) as relied on by observation — prototype pollution defeats every purity-whitelist admission argument and is not detected. A `compute_shadowed_globals`-style top-level scan over the analyzed chunks for `<Builtin>.prototype.<x> = ...` assignment shapes would convert the in-corpus half of the assumption into a checked precondition; pollution originating outside the analyzed chunks (host code, other bundles) necessarily stays an assumption.

### Do anonymous statements deserve a first-class `OwnerKind`?

Today an "anonymous statement" is just an `OwnerNode` with empty `declared`. The materializer (`lowering/materialize/mod.rs`) special-cases them via `anonymous_statement_ordinals` + an explicit `anon_residual_sentinel` ModuleId. The realizability gate doesn't distinguish them. Several diagnostics use the placeholder `<anon stmt #ord>` in `validation.rs`. This is a coherent piece of vocabulary that should perhaps be an `OwnerNode::kind` variant rather than a sentinel "empty declared bindings". Worth thinking about at the next refactor — not blocking.
