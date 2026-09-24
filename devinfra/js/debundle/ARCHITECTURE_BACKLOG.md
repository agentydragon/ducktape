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

1. `check_realizability` materialises one SCC partition and exposes it on the verdict; `validate_factorization` and `reports::build_quotient_scc_reports` consume it instead of re-walking.
2. `ChunkFactorization::build_with` caches a `dep_graph_sccs` field used by the materializer/emitter path.

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

### `BindingId`/`BindingTable` interning (DECIDED 2026-06: defer, perf-triggered)

Implement only if corpus profiling (`perf/proposer.md`) shows the binding-keyed graph paths as a material cost; docs/design.md marks the sketch as hypothetical with the same trigger. Until then it stays unimplemented — do not treat the design.md sketch as a description of the code.

### A11 intrinsic integrity: from observed assumption to checked precondition

docs/design.md documents A11 (the chunk runs with unmodified built-in prototypes) as relied on by observation — prototype pollution defeats every purity-whitelist admission argument and is not detected. A `compute_shadowed_globals`-style top-level scan over the analyzed chunks for `<Builtin>.prototype.<x> = ...` assignment shapes would convert the in-corpus half of the assumption into a checked precondition; pollution originating outside the analyzed chunks (host code, other bundles) necessarily stays an assumption.

### Do anonymous statements deserve a first-class `OwnerKind`?

Today an "anonymous statement" is just an `OwnerNode` with empty `declared`. The materializer (`lowering/materialize/mod.rs`) special-cases them via `anonymous_statement_ordinals` + an explicit `anon_residual_sentinel` ModuleId. The realizability gate doesn't distinguish them. Several diagnostics use the placeholder `<anon stmt #ord>` in `validation.rs`. This is a coherent piece of vocabulary that should perhaps be an `OwnerNode::kind` variant rather than a sentinel "empty declared bindings". Worth thinking about at the next refactor — not blocking.

### Two distinct `LogicalModule` types share a name

`spec::LogicalModule` (the authoring-spec module: `members` / `source_matches` / `annotations` / `anonymous_statements` / `comment`) and `ids.rs::LogicalModule` (the IR materialization record: `id` / `target_file` / `residual` / `rename_map` / `anonymous_statement_ordinals`) are unrelated structs with the same name, forcing qualified-path imports wherever both are visible. "Find a LogicalModule literal" is therefore ambiguous, and it bit an atomic field addition to the spec-side type. Rename the IR one (e.g. `LogicalModuleIr`, `PlannedModule` or `OutputModule`); the rename is mechanical but ripples through `lowering/`, `pipeline.rs` and the e2e fixtures. Not blocking.
