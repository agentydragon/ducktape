# Debundle Architecture Backlog

Current architecture-level follow-ups for `devinfra/js/debundle/`. This
file is an active backlog: resolved items are deleted, not struck through.

## Open backlog

Re-check file paths and line numbers against current `HEAD` before
acting; this file intentionally describes shapes rather than frozen
review line references.

### Materialize-into-emit (next pipeline-trajectory step)

The 2026-06 vendor collapse removed every vendor mutation wave; the
one remaining artifact mutation is `materialize_logical_modules`,
which writes lowered module files back into the chunk bundle for
`write_js_tree` / `emit_browser_harness` to re-read. The recorded next
step, also reflected in docs/design.md "Pipeline trajectory": lowered
outputs feed tree / harness emission directly, dropping the bundle
round-trip and the post-materialize index rebuild. The emission
rewrites (`apply_emission_rewrites`) would become per-file emit steps
in the same pass. No timetable; the e2e suite is the safety net.

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

`graph/linker_order.rs::chunk_source_import_order`'s `None`-after-`Some`
clause is "kept for robustness against future filter changes that might admit
non-constraining members". If the filter shape changes, either turn this
into a tested invariant or delete the defensive branch.

## Code refactor / dedup opportunities

Production-code dedup/cleanup options, calibrated by (LOC saved × safety).

**Structural findings (full-package review):**

1. `realizability/mod.rs` — extract `gate_perf_counters` (~490-line `pub mod`).
   Entangled with index internals (`use super::*`, `pub(super)` recording APIs
   called from `RealizabilityIndex` / `IncrementalQuotient` query methods, and
   the timing-only `IncrementalQuotient::base_snapshot_stale` shadow state); a
   clean move needs a narrow recording trait first, not just a file move.
2. `vendor/mod.rs` further split (~1.3k lines + tests remain after the
   emission/manifests/passthrough/plan/strip/validate/wrappers extraction):
   package/subpath resolution helpers, export-surface collection,
   `MaterializedOutputChunkIndex`, the shared import factories
   (`DeferredImport` / `IdentRewriteTarget` / `PartialSwapIdentRewriter` and
   the `make_*` constructors), and the post-strip consumer scan are each
   liftable.
3. Two parallel top-level fact extractors:
   `program_analysis.rs::analyze_program_shallow` keeps its own traversal and
   `classify_top_level_decl` alongside the `facts/` walk; the two rule sets
   can drift independently. Fold the shallow extractor into the facts
   traversal or derive its records from `StatementFacts`.
4. `lowering/lower.rs` — extract the remaining inline phases of `lower_chunk`
   (naturalization, disambiguation, import planning, the per-module loop);
   each needs substantial captured state from `LowerChunkInputs` (15–20
   fields). Related: `lowering/mod.rs` carries a ~95-line import block from
   wildcard `use super::*` in every sub-module.
5. `output_layout.rs` — replace the 10 identical `self.root.join(CONSTANT)`
   accessors with a data-driven `report_path(name)` plus constants.
6. Encapsulation/type design: BTree collections in hot-path graph structures
   (`rollback_graph.rs`, `artifact.rs`, `realizability/`) where hash-based
   would be measurably faster — document determinism where it is required;
   make `DepKind`'s constraining vs non-constraining axis
   (`constrains_init_order()`) a first-class type distinction; the three-layer
   edge representation (domain graph → rollback graph → realizability index)
   has fragile bridging; `pub(super)` blankets `lowering/` field and function
   visibility; `SourceImportResolution = Option<(String, String, String)>`
   (`plan_references.rs`) needs a named struct.
7. Tests: `e2e/comma_list_owner_split_test.rs` asserts emitted shapes via
   whitespace OR-chains — parse or normalize instead;
   `peel/quotient_integration_test.rs` references share too much code with the
   system under test (most verdicts compare against the kernel's own
   `project_partition`; only `replay_partition` rebuilds independently, and
   compares only `cycle_set()`), and randomized merge/partition sequences and
   gate-residual promotion transitions are uncovered.
8. `ChunkBundle` ownership ping-pong through every stage
   (`artifact = result.artifact`) — cosmetic now that each stage is a pure
   function.

SWC-reuse evaluations (what to adopt, what was rejected and why):
<docs/swc_reuse.md>.

**Real value but needs design work / behavior-risk:**

1. Parameterize the per-form AST holing visitors (`render.rs` `hole_expr` /
   `hole_stmt`, `minimize/class.rs` `hole_class_member`, etc.) behind a `Holer`
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
3. Consolidate the two `*BindingProjection` enums
   (`TargetBindingProjection` in `selector_constraint_backend.rs`,
   `SourceBindingProjection` in `selector_constraint_model_builder.rs`) into one
   shared projection type.
   They are structurally identical views of the same binding-namespace
   partition, duplicated per solver stage; the copies drift silently when a
   new binding kind is added. Unify behind one enum (plus any stage-specific
   extension) and re-point the three stages at it.

**Organization only (≈0 LOC removed, navigability win):** split the giant
files by responsibility — <selector_codemod.rs>, <peel/quotient.rs>,
<lowering/rename_ledger.rs>.

**Defer (high risk):** unifying the union-find / Tarjan-SCC / incremental
cycle-detection between <peel/quotient.rs> and
<realizability/condensation_order.rs>. They look parallel but encode different
correctness invariants for the realizability gate; a shared impl risks hiding
drift. Audit before attempting.

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

### CLI common args via clap `#[command(flatten)]` (DECIDED: declined)

The
recurring flags (`--modules` / `--source-root` / `--format`) occur in
incompatible combinations across the `Args` structs with inconsistent attrs
(`source-root` carries `env` on some structs, not others; `MatchSelector` /
`Describe` / `ShowSource` have no `--modules`), so there is no cohesive group to
extract. Flattening `{modules, format}` would add `args.common.*` indirection
for a semantically-incohesive bundle (input locator + output format) without a
real clarity or LOC win. `peel`'s `CommonArgs` (`{graph, modules}`) stays as the
one cohesive case.

### `BindingId`/`BindingTable` interning (DECIDED 2026-06: defer, perf-triggered)

Implement only if corpus profiling (`perf/proposer.md`) shows the binding-keyed graph paths as a material cost; docs/design.md marks the sketch as hypothetical with the same trigger. Until then it stays unimplemented — do not treat the design.md sketch as a description of the code.

### A11 intrinsic integrity: from observed assumption to checked precondition

docs/design.md documents A11 (the chunk runs with unmodified built-in prototypes) as relied on by observation — prototype pollution defeats every purity-whitelist admission argument and is not detected. A `compute_shadowed_globals`-style top-level scan over the analyzed chunks for `<Builtin>.prototype.<x> = ...` assignment shapes would convert the in-corpus half of the assumption into a checked precondition; pollution originating outside the analyzed chunks (host code, other bundles) necessarily stays an assumption.

### Do anonymous statements deserve a first-class `OwnerKind`?

Today an "anonymous statement" is just an `OwnerNode` with empty `declared`. The materializer (`lowering/materialize/mod.rs`) special-cases them via `anonymous_statement_ordinals` + an explicit `anon_residual_sentinel` ModuleId. The realizability gate doesn't distinguish them. Several diagnostics use the placeholder `<anon stmt #ord>` in `validation.rs`. This is a coherent piece of vocabulary that should perhaps be an `OwnerNode::kind` variant rather than a sentinel "empty declared bindings". Worth thinking about at the next refactor — not blocking.

### Two distinct `LogicalModule` types share a name

`spec::LogicalModule` (the authoring-spec module: `members` / `source_matches` / `annotations` / `anonymous_statements` / `comment`) and `ids.rs::LogicalModule` (the IR materialization record: `id` / `target_file` / `residual` / `rename_map` / `anonymous_statement_ordinals`) are unrelated structs with the same name, forcing qualified-path imports wherever both are visible. "Find a LogicalModule literal" is therefore ambiguous, and it bit an atomic field addition to the spec-side type. Rename the IR one (e.g. `LogicalModuleIr`, `PlannedModule` or `OutputModule`); the rename is mechanical but ripples through `lowering/`, `pipeline.rs` and the e2e fixtures. Not blocking.
