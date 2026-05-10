# Debundler IR Cleanup

Forward-looking refactor of the debundler's internal data structures and naming
toward standard compiler-IR shape. Behaviour-preserving — the realizability
theorem, validator gate, and emit shape don't change. Only how the IR is
represented and named changes.

## Problem

Re-reading <devinfra/js/debundle/DESIGN.md> against the current
<devinfra/js/debundle/graph.rs>, <devinfra/js/debundle/schedule.rs>,
<devinfra/js/debundle/peelability.rs>, <devinfra/js/debundle/facts.rs>, and
<devinfra/js/debundle/logical_modules.rs> surfaces five structural smells. Each
is independent of the others; they're listed in the order I'd land them.

In compiler-theory terms the debundler is a tiny compiler whose IR is a
**program dependence graph** restricted to top-level statements, with use-edges
tagged by binding-time (eager / lazy) and an additional sequenced-execution
relation between side-effecting nodes. The spec is a **partition** of IR
vertices into output modules. The output module dep graph is the
**quotient** of the IR by that partition. The validator's realizability gate
is exactly "no SCC contains an init-order-constraining edge." The peelability
search is exactly "enumerate partition refinements that keep the quotient
realizable."

Most of the smells come from places where the current code drifted off that
framing.

## A. Collapse the dual edge representation

`OwnerGraph` carries the same edge set in two shapes:

- `OwnerGraph.graph: DiGraphMap<OwnerId, EdgeMetadata>` — petgraph
  hashmap-of-hashmaps, used for random access by `(from, to)` and for
  `tarjan_scc` over the fine graph. Pays ~2× memory and indirection for
  random access we mostly don't need.
- `owner_edges: Vec<OwnerEdgeEntry>` — flattened one-row-per-reason, sorted by
  `(from, to, kind, ordinal, binding)`, with stable indices. Used by the
  peelability hot loop and by every consumer that wants "edge at index N".
- `PeelabilityContext::{owner_out_edges, owner_in_edges}: Vec<Vec<usize>>` —
  CSR adjacency built **from** `owner_edges` at peelability-context-build
  time.

We pay storage and bookkeeping for the dual `DiGraphMap` + `Vec<OwnerEdgeEntry>`
because some passes want random access by pair and others want stable indices.
The CSR adjacency is then rebuilt on the fly. The standard compiler-IR shape
gives both with one storage:

```rust
pub struct DepGraph {
    pub nodes: Vec<DepNode>,                  // indexed by NodeId(usize)
    pub edges: Vec<DepEdge>,                  // indexed by EdgeId(usize)
    pub out_edges: Vec<Vec<EdgeId>>,          // CSR by source NodeId
    pub in_edges:  Vec<Vec<EdgeId>>,          // CSR by target NodeId
    pub binding_table: BindingTable,
}

pub struct DepEdge {
    pub from: NodeId,
    pub to: NodeId,
    pub binding: Option<BindingId>,           // None for `Sequenced`
    pub kind: DepKind,
    pub statement_ordinal: StatementOrdinal,
}
```

This drops `petgraph::DiGraphMap` for `OwnerGraph`'s edges, kills the
`OwnerEdgeId` type that currently exists only as a bridge between the two
representations, and lets peelability skip the CSR-rebuild step (the CSR is
the canonical storage).

The petgraph dependency stays only where graph algorithms genuinely want it
(`toposort` for the linker order, `tarjan_scc` for cycle reports,
`greedy_feedback_arc_set` for cut computation). For those, build a
`DiGraphMap` on demand from the flat edge list — those graphs are small
(quotient-sized, ~tens of nodes per chunk) so the conversion cost is
negligible.

**Affected files:** `graph.rs` (rewrite), `schedule.rs` (drop `owner_edges`
field; expose graph adjacency directly), `peelability.rs` (drop
`PeelabilityContext::owner_out_edges` / `owner_in_edges` — read from the
graph), `validation.rs` (operate on flat edges + adjacency), `reports.rs`
(swap `owner_edges` indexing for `graph.edges` indexing).

## B. Demote `ModuleDepGraph` to a derived view

Today `Schedule` builds and stores both `OwnerGraph` and `ModuleDepGraph`,
where the latter is a quotient of the former under the spec's destination
function. Every query on `ModuleDepGraph` (`has_at_init_edge`,
`has_realizability_constraining_edge`, `tarjan_scc`) is answerable from
`OwnerGraph + dest: Fn(OwnerId) -> ModuleId`. The materialized type
duplicates ~70 lines of edge-recording bookkeeping that runs in parallel
with `OwnerGraph`'s.

Cleaner shape: a thin view object.

```rust
pub struct Quotient<'a> {
    ir: &'a DepGraph,
    partition: &'a Partition,
}

impl<'a> Quotient<'a> {
    pub fn modules(&self) -> impl Iterator<Item = ModuleId> + '_ { … }
    pub fn cross_edges(&self) -> impl Iterator<Item = &'a DepEdge> { … }
    pub fn edge_constrains_init(&self, m1: ModuleId, m2: ModuleId) -> bool { … }
    pub fn module_sccs(&self) -> Vec<Vec<ModuleId>> { … }
    /// Memoised quotient adjacency for hot consumers.
    pub fn materialise(self) -> ModuleAdjacency { … }
}
```

Validation, the realizability cut, the linker order, and the report layer all
become `Quotient` consumers. `ModuleDepGraph` deletes. The validator's
public API takes `Quotient<'_>` instead of `&ModuleDepGraph`.

If a downstream pass really wants memoised module adjacency for repeated
queries, it asks for `quotient.materialise()` once.

**Affected files:** delete `ModuleDepGraph` from `graph.rs`,
`quotient_owner_graph` and `quotient_owner_graph_with_destinations`. New
`Quotient<'a>` view in `graph.rs`. `validation.rs` switches to the view.
`schedule.rs` drops `dep_graph: ModuleDepGraph`.

## C. Move the partition off `OwnerNode`

`OwnerNode.destination: ModuleId` couples the IR vertex with the spec's
partition decision. The anonymous-statement override then **mutates
`OwnerNode.destination` after construction** in `Schedule::build` to fix up
owners with no declared binding:

```rust
for (idx, module) in logical_modules.iter().enumerate() {
    let module_id = ModuleId::Logical(LogicalModuleIndex(idx));
    for ordinal in &module.anonymous_statement_ordinals {
        if let Some(node) = owner_graph.nodes.iter_mut().find(…) {
            node.destination = module_id;          // IR mutation post-build
        }
    }
}
```

That's the IR-leaks-into-partition pattern. Cleaner separation:

```rust
pub struct Partition {
    /// Module assignment per IR vertex.
    of: Vec<ModuleId>,                       // indexed by NodeId
}

impl Partition {
    pub fn build(ir: &DepGraph, spec: &SpecAssignment) -> Self {
        let mut of = vec![ModuleId::ResidualEntry; ir.nodes.len()];
        // 1. By-declared-name assignment from spec.
        // 2. Anonymous-statement override from `LogicalModule::anonymous_statement_ordinals`.
        // 3. Done — IR untouched.
        of
    }
    pub fn of(&self, n: NodeId) -> ModuleId { self.of[n.0] }
}
```

IR is immutable. Quotient passes take both the IR and the partition by
reference. The two-pass anonymous override becomes obvious because both
phases operate on the same fresh `Partition` vector, in order.

**Affected files:** `graph.rs` (drop `destination` from `OwnerNode`),
`schedule.rs` (build a `Partition` instead of mutating `OwnerNode`),
`peelability.rs` (the candidate-evaluation code already conceptually
operates on a hypothetical refined partition; this makes that explicit).

## D. Split the god-struct `Schedule`

`Schedule` currently carries:

- chunk facts (input)
- binding catalogue (input)
- spec's logical modules + chunk renames (input/spec)
- owner graph (analysis)
- owner_edges (analysis, redundant after A)
- dep graph (derived analysis = quotient — gone after B)
- linker order + position cache (derived from quotient)
- export-name-by-binding cache (perf)
- entry-exported-binding-names cache (perf)
- owner-report-ids-by-binding cache (perf)

A `Schedule` in the textbook sense is a topo-sort of a constrained DAG; that's
`linker_order: Vec<ModuleId>`. Everything else has a more natural home:

```rust
pub struct ChunkIR {
    pub chunk_id: ChunkId,
    pub facts: Vec<StatementFacts>,
    pub bindings: HashMap<BindingName, BindingKind>,
    pub graph: DepGraph,
    /// Pre-computed binding-name → exported-name for binding_reports.
    pub export_name_by_binding: HashMap<BindingName, BindingName>,
}

pub struct Partition { /* see C */ }

pub struct Schedule {
    pub linker_order: Vec<ModuleId>,
    pub linker_position_by_module: HashMap<ModuleId, usize>,
    /// Pre-Owned-resolution entry export set (cached for peel emit-blocked
    /// projection); `None` when AST analysis was skipped.
    pub entry_exported_names: Option<HashSet<BindingName>>,
}
```

Peelability builds its own per-evaluation context off `(ir, partition)` —
which it largely already does, just routed through `Schedule`. The
`owner_report_ids_by_binding` cache moves to `reports.rs` (its only
consumer).

**Affected files:** `schedule.rs` becomes thin — mostly the `Schedule` type
plus `compute_linker_order`. `lib.rs` re-exports the new triple.
`logical_modules.rs` and tests construct `(ChunkIR, Partition, Schedule)`
instead of one `Schedule`.

## E. Unify `StatementFacts` use-edges into one tagged list

```rust
// Today
pub struct StatementFacts {
    pub reads_at_init: BTreeSet<BindingName>,
    pub reads_lazy:    BTreeSet<BindingName>,
    pub writes_at_init: BTreeSet<BindingName>,
    pub writes_lazy:   BTreeSet<BindingName>,
    …
}
```

The four parallel sets encode a manual histogram of `Vec<(BindingName,
DepKind)>`. The split-sets shape leaves an implicit "what if a name is in
both eager and lazy?" question (the comment on `reads_lazy` actually flags
this as legal). Storing references once with a tag, then filtering at the
consumer, keeps the invariant in the type.

```rust
// Proposed
pub struct StatementFacts {
    pub references: Vec<Reference>,           // both reads and rebind-writes
    pub purity: Purity,
    …
}

pub struct Reference {
    pub binding: BindingName,                 // → BindingId after interning
    pub kind: DepKind,                        // EagerUse | LazyUse | EagerRebind | LazyRebind
}
```

Owner-graph edge construction becomes "for ref in stmt.references → record
edge `(stmt_owner, declaring_owner, ref.kind)`" — already roughly the loop
shape in `build_owner_graph`, just with one inner loop instead of four.

**Affected files:** `facts.rs` (collectors emit `Vec<Reference>` directly),
`graph.rs` (single edge-recording loop), `analysis_tests.rs` (test fixtures
construct fewer sets).

## F. Compiler-theory naming pass

Bulk renames once the structure stabilises (after A–E land). Mechanical;
do as a single PR with `git grep -l <old> | xargs sed`.

| Current                                         | Proposed                                             | Why                                                                                                                                                |
| ----------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OwnerGraph` / `OwnerNode`                      | `DepGraph` / `DepNode` (or `ProgramDependenceGraph`) | What the type actually models.                                                                                                                     |
| `OwnerId`                                       | `NodeId` (or keep `OwnerId`, internally consistent)  | Identity of an IR vertex.                                                                                                                          |
| `EdgeKind::AtInitRead` / `LazyRead`             | `DepKind::EagerUse` / `LazyUse`                      | Standard binding-time-analysis vocab.                                                                                                              |
| `EdgeKind::AtInitWrite` / `LazyWrite`           | `DepKind::EagerRebind` / `LazyRebind`                | These aren't writes in the dataflow sense — member writes are excluded by design. They're rebinding writes (anti-deps).                            |
| `EdgeKind::SideEffectOrder`                     | `DepKind::Sequenced` (or `ProgramOrder`)             | Standard term for "preserve source-order between two effects."                                                                                     |
| `EdgeReason.constrains_realizability`           | `.constrains_init_order`                             | Tighter — that's exactly what the predicate tests.                                                                                                 |
| `ModuleDepGraph` (if it survives B)             | `ModuleQuotient`                                     | It's a quotient, not a separate graph.                                                                                                             |
| `ResidualEntry`                                 | `EntryModule` (or `Default` partition)               | "Residual" is debundler-internal jargon for "the default partition that catches everything unassigned." Keep the synthetic-module concept; rename. |
| `Schedule` (post-D)                             | already correct after the split                      | Schedule = topo-sort, matches usage.                                                                                                               |
| `peelability::evaluate_residual_peel_candidate` | `evaluate_partition_refinement`                      | A peel is a partition refinement.                                                                                                                  |
| `PeelabilityContext`                            | `IncidenceCache` (or `RefinementContext`)            | Names what it actually is: per-owner CSR adjacency + per-module-pair edge totals.                                                                  |
| `OwnerEdgeEntry` (gone after A)                 | `DepEdge`                                            | One representation, one name.                                                                                                                      |
| `OwnerEdgeId` (gone after A)                    | `EdgeId`                                             | Same.                                                                                                                                              |

## G. Sever the JSON report schema from the IR (stretch)

`OwnerGraphNodeReport`, `OwnerGraphEdgeReport`, `QuotientEdgeReport` etc. in
`report_schema.rs` are the **external wire format**. Today the report layer
in `reports.rs` builds them by reading directly off `Schedule` /
`OwnerGraph`. The two stay loosely coupled but a structural change to the
IR (renaming a variant, splitting a field) automatically requires a wire-
shape change.

Cleaner: a dedicated mapping layer
`reports.rs::owner_graph_report(ir, partition, peelability) ->
OwnerGraphReport` keeps the IR free to evolve and makes the report
fields the only place that owns the public contract.

This is mostly about new test discipline — every IR change should compile
the report layer in isolation and pin the wire shape with a snapshot test.
Today both targets sit in the same crate so changes propagate silently.

## Order of landing

1. **B + C together** — `Quotient<'a>` view; partition off `OwnerNode`.
   Biggest conceptual simplification, well-bounded blast radius.
2. **D** — split `Schedule`. Lots of mechanical updates in tests but no
   semantic change.
3. **A** — collapse the dual edge representation. Rewrite `OwnerGraph` as
   flat edges + CSR adjacency.
4. **E** — unified `Reference` list on `StatementFacts`.
5. **F** — bulk rename to compiler-theory vocabulary. Mechanical, single PR.
6. **G** — split report schema from IR (only after the IR has stopped
   moving).

Each step compiles and passes the full `//devinfra/js/debundle/...` test
matrix on its own. None changes validator behaviour or emit shape — the
realizability theorem and the per-fixture output are invariants.

## Out of scope

- The realizability theorem and gate (DESIGN.md "The realizability
  theorem"). Untouched.
- The lowering / emit pipeline for spec materialisation. Untouched.
- The CLI surface (`--spec`, `--package-root`, etc.). Untouched.
- The wire shape of `*.json` reports (until G; even then, the type names
  stay so existing JSON consumers keep parsing).
