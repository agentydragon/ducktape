# Debundler IR Cleanup

Refactor of the debundler's internal IR toward standard compiler-IR shape.
Behaviour-preserving — the realizability theorem, validator gate, and emit
shape don't change. Only how the IR is represented and named changes.

In compiler-theory terms the debundler is a tiny compiler whose IR is a
**program dependence graph** restricted to top-level statements, with use-edges
tagged by binding-time (eager / lazy) and an additional sequenced-execution
relation between side-effecting nodes. The spec is a **partition** of IR
vertices into output modules. The output module dep graph is the **quotient**
of the IR by that partition. The validator's realizability gate is exactly
"no SCC contains an init-order-constraining edge." The peelability search is
exactly "enumerate partition refinements that keep the quotient realizable."

## Status

| Step | Status          | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A    | DONE            | `OwnerGraph` stores flat `edges: Vec<OwnerEdge>` + CSR `out_edges`/`in_edges`; `Schedule.owner_edges` field gone; `collect_owner_edge_entries` deleted; `OwnerEdgeEntry` renamed to `OwnerEdge`.                                                                                                                                                                                                                                                                                                                                      |
| B    | DONE            | `ModuleDepGraph` is now `ModuleQuotient` built from `(owner_graph, partition)` via `build_module_quotient`. Kept materialised because petgraph algos (`tarjan_scc`, `toposort`, `greedy_feedback_arc_set`) want a real graph; the lazy `Quotient<'a>` view didn't pay its keep.                                                                                                                                                                                                                                                       |
| C    | DONE            | `Partition { of: Vec<ModuleId> }` (`devinfra/js/debundle/partition.rs`); `OwnerNode::destination` removed; `build_owner_graph(facts)` builds pure IR; `Schedule.partition` holds the spec's assignment; the anonymous-statement override applies to the partition, not the IR.                                                                                                                                                                                                                                                        |
| F    | DONE            | Wire-affecting renames landed: `EdgeKind` → `DepKind`; variants `AtInitRead` / `LazyRead` / `AtInitWrite` / `LazyWrite` / `SideEffectOrder` → `EagerUse` / `LazyUse` / `EagerRebind` / `LazyRebind` / `Sequenced`; `constrains_realizability` → `constrains_init_order`; `StatementFacts.{reads_at_init, reads_lazy, writes_at_init, writes_lazy}` → `{eager_reads, lazy_reads, eager_rebinds, lazy_rebinds}`; `ModuleQuotient` method names aligned. JSON wire shape changed (e.g. `"edge_kind": "eager_use"` was `"at_init_read"`). |
| D    | DEFERRED        | After A, `Schedule` no longer carries `owner_edges`. The remaining fields cohere as "everything about chunk K under partition P"; splitting them into `ChunkIR` + `Schedule` is mostly mechanical churn. Revisit when a downstream consumer actually wants one half without the other. See "D — deferred details" below.                                                                                                                                                                                                              |
| E    | DECIDED AGAINST | The four-way split (`eager_reads` / `lazy_reads` / `eager_rebinds` / `lazy_rebinds`) reads more cleanly at use sites than a unified `BTreeSet<(BindingName, DepKind)>`, and the collectors are separate by design (lazy descends into function bodies, eager doesn't).                                                                                                                                                                                                                                                                |
| G    | OBVIATED        | Was on the critical path only to free F's wire-affecting renames; with JSON wire-shape changes accepted directly, the report-schema split is no longer needed. Could still happen if the IR moves more — defer until then.                                                                                                                                                                                                                                                                                                            |

## D — deferred details

Should D ever come off the bench, the target shape:

```rust
pub struct ChunkIR {
    pub chunk_id: ChunkId,
    pub facts: Vec<StatementFacts>,
    pub bindings: HashMap<BindingName, BindingKind>,
    pub graph: OwnerGraph,
    /// Pre-computed binding-name → exported-name for binding_reports.
    pub export_name_by_binding: HashMap<BindingName, BindingName>,
}

pub struct Schedule {
    pub linker_order: Vec<ModuleId>,
    pub linker_position_by_module: HashMap<ModuleId, usize>,
    /// Pre-Owned-resolution entry export set (cached for peel emit-blocked
    /// projection); `None` when AST analysis was skipped.
    pub entry_exported_names: Option<HashSet<BindingName>>,
}
```

The `owner_report_ids_by_binding` cache moves to `reports.rs` (its only
consumer). Peelability builds its own per-evaluation context off
`(ir, partition)` — which it largely already does, just routed through
`Schedule` today.

## Out of scope

- The realizability theorem and gate (DESIGN.md "The realizability theorem").
  Untouched.
- The lowering / emit pipeline for spec materialisation. Untouched.
- The CLI surface (`--spec`, `--package-root`, etc.). Untouched.
