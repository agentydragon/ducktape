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

Remaining legitimate walks (different graphs): `validation.rs::compute_realizability_cut` (FAS iteration, intrinsic), `graph.rs::promote_at_init_calls` (closure fixpoint), `atomic_units.rs::compute_atomic_units` (constraining-edge owner SCC).

**Open follow-up.** The verdict-time and factorization-build-time walks
compute the same partition for different consumers; structurally
consolidatable behind a wider API change, but not urgent and not on a hot
path.

## Encapsulation + module boundaries

### `ChunkFactorization` is yet another per-chunk IR/report layer

`chunk_factorization.rs::ChunkFactorization` holds `analysis: Arc<ChunkAnalysis>` plus partition + dep_graph + linker_order + maps. Then `validate()` returns a `FactorizationReport` which is yet a third "report" type alongside `ChunkAnalysisReport` and the IR `ChunkAnalysis`. The naming hierarchy is:

```
ChunkAnalysis (IR)     // chunk_analysis.rs
  ↓ wrapped in
ChunkFactorization     // chunk_factorization.rs (IR + partition + dep_graph)
  ↓ validate() →
FactorizationReport    // validation.rs (cycles + atomic_unit_conflicts + linker_order)

ChunkAnalysisReport    // artifact.rs (the JSON per-chunk report stub)
  ↓ from_analysis() →
ChunkManifest          // artifact.rs (analysis report + decomposition + metrics)

OwnerGraphReport       // reports/schema.rs (the JSON view of the typed OwnerGraph)
```

Six distinct types in the orbit of "stuff a chunk analysis produced" (after the `ChunkAnalysis`/`ChunkAnalysisReport` split). A reader still can't tell from the name alone which one carries which data without grepping. Some of this is unavoidable (the JSON-wire / typed-IR split is real), but the layering of `ChunkAnalysis` → `ChunkFactorization` → `FactorizationReport` could plausibly collapse to two: an IR with optional partition state + a derive-to-report adapter.

### `pub(crate)` on internals is broad

`OwnerGraph` fields are private, but `RealizabilityIndex` holds
owner-edge references, `IncrementalQuotient` maintains bucket state
derived from the owner graph, and `OwnerGraph::from_report` reconstructs
it from JSON. The _crate-internal_ invariant surface is still large:
several consumers rely on conventions rather than a type boundary that
makes invalid operations impossible.

## Name overloading

Watch out for:

- **`ChunkFactorization` vs `ChunkAnalysis`**: both are per-chunk IR; the difference is whether the partition is applied. Could be `ChunkAnalysis` (no partition) vs `FactorizedChunk` (partition applied) and the meaning would be more obvious.
- **`SccDiagnosis` (`realizability/mod.rs`, renamed from `UnrealizableScc` in `3dbaf1037`)** vs **`CycleReport` (`validation.rs:38`)** vs **`QuotientSccReport` (`reports/schema.rs:174`)** vs **`AtomicUnitConflict` (`factor_assembly.rs:42`)** — four representations of "the spec is unrealizable, here's why" with subtly different fields. `SccDiagnosis` carries `constraining_owner_edges`; `CycleReport` carries `cut` (a minimum cut) + `evidence`; `QuotientSccReport` carries `module_edge_ids` + `constraining_module_edge_ids`. Two of these contain the same data ("the modules in the SCC + the edges in the SCC"), with the cut/evidence/min decoration added by the validator. The right shape is one core type with optional decorations, not four parallel structs.

## Algorithmic clarity (realizability gate, atom detection)

### The gate is _more_ coherent than the maintainer fears, but its docs make it look like a stack of patches

The realizability gate's actual algorithm, read carefully, is:

> Build the canonical constraining-edge view of the I-graph; the gate accepts iff (a) Tarjan on the constraining-edge view has no multi-module SCC, and (b) for every multi-module SCC in the full I-graph that has at least one constraining edge, the ECMA-262 Phase-2 simulator (rooted at residual, with residual's imports sorted by `source_import_position` and every other module's by `linker_position`) yields a post-order with `post_order[target] < post_order[source]` for every constraining edge.

That's one algorithm with two passes. Pass 1 is a cheap necessary condition (mutual at-init cycles can never be rescued by reordering); Pass 2 is the precise condition (the runtime DFS-simulator decides asymmetric cycles). The 2× Tarjan is structural to the algorithm, not patchy. **This is fine.** The docs/design.md theorem reads cleanly.

### Atomic-units classification has two paths but only one is wired

`atomic_units.rs::compute_atomic_units` is the structural-atom detector (SCCs of the constraining-edge owner graph). `factor_assembly::detect_unit_conflict` is the "did the spec split a unit?" detector. The structural atoms are computed once per chunk (in `compute_owner_graph_and_units_with`), passed through `OwnerGraphAndUnits` to the materializer and into `ChunkFactorization`. Clean — this is the right shape.

Spec-induced atoms (the SCCs of `I ∪ S` under the quotient) are NOT
precomputed; they emerge from the realizability primitive. docs/design.md
§"Two classes of atom" labels them as a distinct concept. The verdict
exposes the SCC partition and `validate_factorization` consumes it. The
residual walk lives on `ChunkFactorization::dep_graph_sccs` for the
materializer/emitter path (see "Duplicated calculations" for the open
consolidation).

## Test-vs-spec drift

### `#[ignore]`d tests should name the future work

`e2e/purity_test.rs` names explicit "Step D"/"Step E" reasons for its
ignored tests. Keep that standard for any new ignored test: the reason
must point to current future work, not an unexplained skip.

### Defensive comments should stay tied to a real invariant

`graph.rs::chunk_source_import_order`'s `None`-after-`Some` clause is
"kept for robustness against future filter changes that might admit
non-constraining members". If the filter shape changes, either turn this
into a tested invariant or delete the defensive branch.

### Keep the doc split crisp

These files document the same project from multiple perspectives. Skimming them, I find:

- docs/design.md is the canonical theorem + algorithm document.
- AGENTS.md is the canonical "how to work on this crate" document.
- docs/cli.md is the command reference; docs/guide.md is the worked
  step-by-step workflow document (at 644 lines it is now larger than
  README.md, not "shorter intro material").
- docs/wire_format.md is the JSON sidecar reference.
- CODE_REVIEW.md is the active code-quality backlog.
- CLI_DOGFOOD.md is the open CLI usability/scripting-safety backlog.
- README.md is a marketing-shaped pitch with usage.
- TODO.md is the broad active work backlog.
- perf/proposer.md is the performance work log.
- plans/ holds future-work design notes (including
  plans/factor_vocabulary_rename.md, the terminology-rename plan
  removing "factor" vocabulary in favor of precise graph-theoretic
  names); x/ holds experimental/in-flux notes.
- docs/lessons_learned/cross_process_stage_b.md is the historical
  exception: it records an abandoned design to prevent repeating it.

## Quick wins (≤30 min each)

1. **Carry chunk-top-level `Mark` on `ChunkContext`** so `top_level_id`
   lookups do not have to be threaded through every materialize-side
   function as a separate parameter. The `Mark` lives on `LowerChunkAst`
   but is still threaded through several helpers below `lower_chunk`.
   Folding the `top_level_id` helper onto a small `ChunkContext`
   accessor would let helpers take just that context instead.

## Concerns to discuss before deciding

### Gate simulator ↔ materializer import-order sharing (RESOLVED)

The historical drift surface — the emitter placed phantom side-effect imports first in each emitted module while the simulator sorted ALL of a module's I-successors in one `linker_position` list, residual's missing universal entry imports, and the `usize::MAX` tie-break mismatch — is resolved: both sides now consume one shared ordering implementation, `esm_import_order::EsmImportOrder` (`sort_entry_imports` / `sort_module_imports`), built from the canonical `ChunkConstrainingEdgeSet`. The emitter renders the entry's per-plan import list (named imports for binding-owning plans, side-effect-only imports for binding-less plans) and each module's merged intra-chunk import list (binding + phantom + residual-entry, one sort) from it; the simulator (`realizability::EsmIGraph`) uses the same two sorts as DFS neighbor order, with residual fanning out to every I-graph module exactly as the emitted entry does. Do not reintroduce per-side ordering rules — encode any ordering requirement in `EsmImportOrder` so both sides inherit it.

Remaining known approximation: the simulator roots at `partition.residual()` (the `anon_residual_sentinel` ≈ the entry file) and models its body as evaluating last. That is exact for the entry file in both `unassigned_mode`s; the `catchall_file` plan itself is an ordinary module and is modeled as one. Pass-2 candidate SCC enumeration still runs over the _real_ I-graph (no universal residual edges), so a module that eager-reads an entry-file binding when residual's own statements never reference that module forms no candidate SCC and is not checked — a narrow, pre-existing under-restriction (inline-mode-only: catchall chunks keep no TDZ-prone bindings in the entry file). Extending candidate enumeration with the universal entry edges would close it at the cost of much larger SCCs in the incremental planner path.

### `BindingId`/`BindingTable` interning (DECIDED 2026-06: defer, perf-triggered)

Implement only if corpus profiling (`perf/proposer.md`) shows the binding-keyed graph paths as a material cost; docs/design.md marks the sketch as hypothetical with the same trigger. Until then it stays unimplemented — do not treat the design.md sketch as a description of the code.

### A11 intrinsic integrity: from observed assumption to checked precondition

docs/design.md documents A11 (the chunk runs with unmodified built-in prototypes) as relied on by observation — prototype pollution defeats every purity-whitelist admission argument and is not detected. A `compute_shadowed_globals`-style top-level scan over the analyzed chunks for `<Builtin>.prototype.<x> = ...` assignment shapes would convert the in-corpus half of the assumption into a checked precondition; pollution originating outside the analyzed chunks (host code, other bundles) necessarily stays an assumption.

### Do anonymous statements deserve a first-class `OwnerKind`?

Today an "anonymous statement" is just an `OwnerNode` with empty `declared`. The materializer (`lowering/materialize/mod.rs`) special-cases them via `anonymous_statement_ordinals` + an explicit `anon_residual_sentinel` ModuleId. The realizability gate doesn't distinguish them. Several diagnostics use the placeholder `<anon stmt #ord>` in `validation.rs`. This is a coherent piece of vocabulary that should perhaps be an `OwnerNode::kind` variant rather than a sentinel "empty declared bindings". Worth thinking about at the next refactor — not blocking.
