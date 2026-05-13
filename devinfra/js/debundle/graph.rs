use std::collections::{BTreeSet, HashSet};

use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};

use crate::partition::Partition;
use crate::purity::Purity;
use crate::{
    BindingId, BindingName, BindingTable, ModuleId, SourceLocation, StatementFacts, StatementKind,
    StatementOrdinal,
};

/// One reason an edge `(from, to)` exists, with the source
/// statement ordinal that produced it. This is the single source of
/// truth for edge semantics:
///
/// - `EagerUse` constrains ESM evaluation order under TDZ
///   semantics (`R ⊆ I`).
/// - `LazyUse` contributes to the imports graph `I`, but does not
///   constrain realizability inside an SCC because the read fires
///   after module evaluation.
/// - `EagerRebind` / `LazyRebind` describe rebinding writes. A
///   cross-destination write is rejected outright because ESM imports
///   are read-only in the importing module; same-destination writes
///   are represented only at owner level and don't become module
///   imports.
/// - `Sequenced` contributes to `S` and constrains
///   realizability because source-order side effects require a
///   topological order.
#[derive(Debug, Clone)]
pub struct EdgeReason {
    pub(crate) kind: DepKind,
    pub(crate) statement_ordinal: StatementOrdinal,
    pub(crate) binding: Option<BindingId>,
}

impl EdgeReason {
    pub(crate) fn eager_use(so: StatementOrdinal, b: BindingId) -> Self {
        Self {
            kind: DepKind::EagerUse,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn lazy_use(so: StatementOrdinal, b: BindingId) -> Self {
        Self {
            kind: DepKind::LazyUse,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn eager_rebind(so: StatementOrdinal, b: BindingId) -> Self {
        Self {
            kind: DepKind::EagerRebind,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn lazy_rebind(so: StatementOrdinal, b: BindingId) -> Self {
        Self {
            kind: DepKind::LazyRebind,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn sequenced(so: StatementOrdinal) -> Self {
        Self {
            kind: DepKind::Sequenced,
            statement_ordinal: so,
            binding: None,
        }
    }

    pub(crate) fn is_eager_use(&self) -> bool {
        self.kind == DepKind::EagerUse
    }
    pub(crate) fn is_rebind(&self) -> bool {
        matches!(self.kind, DepKind::EagerRebind | DepKind::LazyRebind)
    }
    pub(crate) fn is_sequenced(&self) -> bool {
        self.kind == DepKind::Sequenced
    }
    /// Every kind except `LazyUse` constrains realizability.
    /// Stated as exclusion so adding a new `DepKind` variant
    /// forces an explicit decision here.
    pub(crate) fn constrains_init_order(&self) -> bool {
        self.kind != DepKind::LazyUse
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DepKind {
    EagerUse,
    LazyUse,
    EagerRebind,
    LazyRebind,
    Sequenced,
}

/// Stable-in-run identity of an owner graph vertex. V1 owner
/// vertices are post-comma-list `StatementFacts` rows, so the id
/// is the row's source-order ordinal.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct OwnerId(pub usize);

/// Fine-grained graph before logical modules are formed. Nodes are
/// top-level owners/statements; edges are owner-level reads and
/// source-order side-effect constraints. The module dependency graph
/// is the quotient of this graph by a [`Partition`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`OwnerEdge`] per reason, indexed by [`OwnerEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal. The previous representation
/// kept two parallel views — a `petgraph::DiGraphMap` for random
/// access by `(from, to)` and a separate `Vec<OwnerEdge>` for
/// stable indices into edges; this collapses them.
#[derive(Debug, Clone, Default)]
pub struct OwnerGraph {
    pub binding_table: BindingTable,
    pub nodes: Vec<OwnerNode>,
    pub edges: Vec<OwnerEdge>,
    /// CSR adjacency by source owner. `out_edges[owner.0]` is a list
    /// of `OwnerEdgeId` indices into `edges`.
    pub out_edges: Vec<Vec<OwnerEdgeId>>,
    /// CSR adjacency by target owner.
    pub in_edges: Vec<Vec<OwnerEdgeId>>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<BindingId>,
    pub kind: StatementKind,
    pub purity: Purity,
}

impl OwnerGraph {
    /// Iterate `&OwnerEdge` in `OwnerEdgeId` order. Each row is one
    /// reason — multiple reasons between the same `(from, to)` pair
    /// appear as separate entries.
    pub fn iter_edges(&self) -> impl Iterator<Item = &OwnerEdge> + '_ {
        self.edges.iter()
    }

    pub fn node(&self, id: OwnerId) -> Option<&OwnerNode> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &OwnerNode> {
        self.nodes.iter()
    }

    /// Edges originating at `owner`.
    pub fn out_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.out_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Edges terminating at `owner`.
    pub fn in_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.in_edges.get(owner.0).map(Vec::as_slice).unwrap_or(&[])
    }
}

/// Per-edge metadata. One physical `(from, to)` ESM `import`
/// directive can be backed by multiple reasons (e.g. several
/// at-init reads of bindings owned by the same target module);
/// they're all kept here so cycle reports can show every
/// triggering statement.
#[derive(Debug, Clone, Default)]
pub struct EdgeMetadata {
    pub reasons: Vec<EdgeReason>,
}

impl EdgeMetadata {
    /// `true` if at least one reason is an at-init read. The
    /// realizability gate uses this to decide whether an
    /// `I ∪ S` SCC contains an `R` cross-module edge.
    pub fn has_eager_use(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_eager_use)
    }

    /// `true` if at least one reason is a side-effect ordering
    /// edge. `S` edges in an SCC make it unrealizable: the
    /// constraint is "predecessor must evaluate before
    /// successor", and a cycle has no topological emit order
    /// satisfying every such edge.
    pub fn has_sequenced(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_sequenced)
    }

    /// `true` if at least one reason is a rebinding write. These
    /// edges are rejected outright when they cross destination
    /// modules because imported ESM bindings are read-only.
    pub fn has_rebind(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_rebind)
    }

    /// `true` if this edge constrains realizability — at least one
    /// of its reasons is realizability-constraining (an at-init
    /// read `R`, a side-effect ordering `S` edge, or a rebinding
    /// write). Lazy read-only edges don't, because the reads they
    /// represent fire after every module in the cycle has finished
    /// evaluating.
    ///
    /// Delegates to `EdgeReason::constrains_init_order` to keep
    /// the per-edge and per-reason definitions in lockstep.
    pub fn constrains_init_order(&self) -> bool {
        self.reasons.iter().any(EdgeReason::constrains_init_order)
    }
}

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
///
/// Backed by `petgraph::DiGraphMap`: one edge per directed
/// `(from, to)` pair, weight = `EdgeMetadata`. Multiple reasons
/// for the same physical edge (e.g. several at-init reads of
/// bindings owned by the same target module) accumulate into the
/// edge's reason list. Cycle detection runs through petgraph's
/// `tarjan_scc`.
#[derive(Debug, Clone, Default)]
pub struct ModuleQuotient {
    pub binding_table: BindingTable,
    pub graph: DiGraphMap<ModuleId, EdgeMetadata>,
}

impl ModuleQuotient {
    fn record_reason(&mut self, from: ModuleId, to: ModuleId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.graph.contains_edge(from, to) {
            self.graph.add_edge(from, to, EdgeMetadata::default());
        }
        self.graph
            .edge_weight_mut(from, to)
            .unwrap()
            .reasons
            .push(reason);
    }

    /// Iterate edges as `(from, to, &EdgeMetadata)`.
    pub fn iter_edges(&self) -> impl Iterator<Item = (ModuleId, ModuleId, &EdgeMetadata)> + '_ {
        self.graph.all_edges()
    }

    /// Edge metadata, if the edge exists.
    pub fn edge(&self, from: ModuleId, to: ModuleId) -> Option<&EdgeMetadata> {
        self.graph.edge_weight(from, to)
    }

    /// `true` if the directed edge `(from, to)` is present and at
    /// least one of its reasons is an at-init read.
    pub fn has_eager_use_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::has_eager_use)
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_init_order_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_init_order)
    }
}

/// Build the fine owner graph from per-statement facts. Pure IR
/// construction: no module assignment, no quotient. Module-level
/// dependencies are derived later by [`build_module_quotient`]
/// given a [`Partition`] mapping owners to destination modules.
pub fn build_owner_graph(facts: &[StatementFacts]) -> OwnerGraph {
    let mut binding_table = BindingTable::default();
    let mut binding_owner = Vec::<Option<OwnerId>>::new();
    let mut nodes = Vec::<OwnerNode>::with_capacity(facts.len());
    for stmt in facts {
        let mut declared = BTreeSet::new();
        for binding in &stmt.declared {
            let binding_id = binding_table.intern(binding.clone());
            if binding_owner.len() <= binding_id.0 {
                binding_owner.resize(binding_id.0 + 1, None);
            }
            binding_owner[binding_id.0] = Some(OwnerId(stmt.ordinal.0));
            declared.insert(binding_id);
        }
        let id = OwnerId(stmt.ordinal.0);
        nodes.push(OwnerNode {
            id,
            statement_ordinal: stmt.ordinal,
            source_location: stmt.source_location.clone(),
            declared,
            kind: stmt.kind,
            purity: stmt.purity.clone(),
        });
    }

    // Collect (from, to, reason) triples; the final `edges` Vec is
    // sorted at the end so `OwnerEdgeId` indices are stable.
    let mut raw_edges = Vec::<(OwnerId, OwnerId, EdgeReason)>::new();
    let push_binding_edge = |raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
                             from: OwnerId,
                             binding: &BindingName,
                             make_reason: fn(StatementOrdinal, BindingId) -> EdgeReason,
                             statement_ordinal: StatementOrdinal| {
        let Some(binding_id) = binding_table.get(binding) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        let Some(Some(to)) = binding_owner.get(binding_id.0) else {
            return;
        };
        if from == *to {
            return;
        }
        raw_edges.push((from, *to, make_reason(statement_ordinal, binding_id)));
    };
    for stmt in facts {
        let from = OwnerId(stmt.ordinal.0);
        for binding in &stmt.eager_reads {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.lazy_reads {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.eager_rebinds {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_rebind,
                stmt.ordinal,
            );
        }
        for binding in &stmt.lazy_rebinds {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_rebind,
                stmt.ordinal,
            );
        }
    }

    // Side-effect ordering edges (`S` per DESIGN.md "Module dep
    // graphs"). At owner level, record the source-order chain over
    // side-effecting owners: every later side-effecting owner
    // depends on the immediately previous side-effecting owner.
    // This is the transitive reduction of the total order. It
    // preserves reachability and SCCs while avoiding an O(n^2)
    // owner-edge explosion in Tana-scale chunks.
    //
    // `purity` is computed by `classify_expr_purity` so
    // pure literal initializers (`const X = 42`,
    // `const X = { a: 1 }`, function/class declarations without
    // observable static init) don't contribute to S. Without
    // that precision the cross-module S graph would be dense
    // enough to reject realistic specs for trivially pure const
    // sequences.
    let mut previous_side_effect_owner: Option<OwnerId> = None;
    for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
        let from = OwnerId(stmt.ordinal.0);
        if let Some(to) = previous_side_effect_owner
            && from != to
        {
            raw_edges.push((from, to, EdgeReason::sequenced(stmt.ordinal)));
        }
        previous_side_effect_owner = Some(from);
    }

    // Sort + assign stable `OwnerEdgeId` indices, then build CSR
    // adjacency in one pass.
    raw_edges.sort_by_key(|(from, to, reason)| {
        (
            *from,
            *to,
            reason.kind,
            reason.statement_ordinal,
            reason.binding,
        )
    });
    let edges: Vec<OwnerEdge> = raw_edges
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| OwnerEdge {
            id: OwnerEdgeId(idx),
            from,
            to,
            reason,
        })
        .collect();
    let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    for edge in &edges {
        if let Some(slot) = out_edges.get_mut(edge.from.0) {
            slot.push(edge.id);
        }
        if let Some(slot) = in_edges.get_mut(edge.to.0) {
            slot.push(edge.id);
        }
    }

    OwnerGraph {
        binding_table,
        nodes,
        edges,
        out_edges,
        in_edges,
    }
}

/// Quotient the owner graph by `partition` to build the module
/// dependency graph consumed by validation and emit. The single
/// public construction path; peelability and reports both go through
/// this for any non-hypothetical quotient.
pub fn build_module_quotient(owner_graph: &OwnerGraph, partition: &Partition) -> ModuleQuotient {
    let mut graph = ModuleQuotient {
        binding_table: owner_graph.binding_table.clone(),
        graph: DiGraphMap::new(),
    };
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in &owner_graph.edges {
        let from = partition.of(edge.from);
        let to = partition.of(edge.to);
        if from == to {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        graph.record_reason(from, to, edge.reason.clone());
    }
    graph
}

/// Stable per-chunk identity of an owner-graph edge. Equal to the
/// edge's position in [`OwnerGraph::edges`]. The previous
/// representation stored the report-shape spelling
/// (`format!("owner_edge:{idx}")`) on every entry; that spelling is
/// `O(n_edges)` strings allocated per chunk and
/// `O(n_blockers × n_candidates)` clones inside the peelability hot
/// loop. Carry the typed index instead and let the report layer do
/// the formatting at its single serialization boundary via
/// [`OwnerEdgeId::report_key`].
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct OwnerEdgeId(pub usize);

impl OwnerEdgeId {
    pub(crate) fn report_key(self) -> String {
        format!("owner_edge:{}", self.0)
    }
}

#[derive(Debug, Clone)]
pub struct OwnerEdge {
    pub id: OwnerEdgeId,
    pub from: OwnerId,
    pub to: OwnerId,
    pub reason: EdgeReason,
}

/// Predicate shared by `materialize_logical_modules` and
/// `peelability.rs`: given a candidate set of moved owners and the
/// post-peel entry export set, report binding names that moved bodies
/// would reference free — bindings declared by an owner that stays in
/// the source destination but aren't on entry's export list.
///
/// SSOT for the materializer's "moved module references source
/// destination binding(s) … not exported by entry" rejection: when
/// this returns a non-empty set, the materializer would reject and
/// peelability marks the candidate `BlockedEmitResolvability`.
/// Mirrors the `constrains_init_order` SSOT introduced in
/// `f86e84b7e`.
///
/// Inputs:
/// - `owner_graph`: source of the per-reason edge list (`.edges`)
///   walked here, the binding-name table, and `node()` lookups for
///   each edge's target.
/// - `partition`: per-owner module assignment. The destination module
///   of each edge's target is read via `partition.of(edge.to)`; only
///   targets sharing the moved set's source destination are
///   considered. The source destination is derived from
///   `moved_owners` (all moved owners share one destination by
///   precondition).
/// - `moved_owners`: the candidate's moved owner set (a peel of these
///   is the hypothetical change being evaluated).
/// - `base_entry_exports`: the schedule's cached pre-peel entry
///   export set — pre-existing source exports plus bindings of any
///   owner already living in a logical module. Stable across all
///   candidates evaluated for the same chunk; passed by reference to
///   avoid the per-candidate clone the previous BTreeSet API forced.
/// - `candidate_members`: bindings the candidate would auto-export
///   from entry on emit (via `entry_exports_for_moved_bindings`),
///   i.e. the per-candidate addition on top of `base_entry_exports`.
pub(crate) fn peel_emit_blocked_source_destination_bindings(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    moved_owners: &BTreeSet<OwnerId>,
    base_entry_exports: &HashSet<BindingName>,
    candidate_members: &[BindingName],
) -> BTreeSet<BindingName> {
    let Some(&first) = moved_owners.iter().next() else {
        return BTreeSet::new();
    };
    let source_destination = partition.of(first);
    // Hoist the candidate-members membership test out of the per-edge
    // loop; this function is called once per peelability candidate
    // (≥1500/chunk on Tana), so the inner test wants O(1).
    let candidate_members_set: HashSet<&BindingName> = candidate_members.iter().collect();
    let mut blocked = BTreeSet::new();
    for edge in &owner_graph.edges {
        if !moved_owners.contains(&edge.from) {
            continue;
        }
        if moved_owners.contains(&edge.to) {
            continue;
        }
        if owner_graph.node(edge.to).is_none() {
            continue;
        }
        if partition.of(edge.to) != source_destination {
            continue;
        }
        let Some(binding_id) = edge.reason.binding else {
            continue;
        };
        let Some(name) = owner_graph.binding_table.name(binding_id) else {
            continue;
        };
        if base_entry_exports.contains(name) || candidate_members_set.contains(name) {
            continue;
        }
        blocked.insert(name.clone());
    }
    blocked
}
