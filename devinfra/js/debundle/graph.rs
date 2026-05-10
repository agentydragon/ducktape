use std::collections::{BTreeMap, BTreeSet};

use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};

use crate::purity::Purity;
use crate::{
    BindingId, BindingName, BindingTable, ModuleId, SourceLocation, StatementFacts, StatementKind,
    StatementOrdinal,
};

/// One reason an edge `(from, to)` exists, with the source
/// statement ordinal that produced it. This is the single source of
/// truth for edge semantics:
///
/// - `AtInitRead` constrains ESM evaluation order under TDZ
///   semantics (`R ⊆ I`).
/// - `LazyRead` contributes to the imports graph `I`, but does not
///   constrain realizability inside an SCC because the read fires
///   after module evaluation.
/// - `AtInitWrite` / `LazyWrite` describe rebinding writes. A
///   cross-destination write is rejected outright because ESM imports
///   are read-only in the importing module; same-destination writes
///   are represented only at owner level and don't become module
///   imports.
/// - `SideEffectOrder` contributes to `S` and constrains
///   realizability because source-order side effects require a
///   topological order.
#[derive(Debug, Clone)]
pub enum EdgeReason {
    AtInitRead {
        statement_ordinal: StatementOrdinal,
        binding: BindingId,
    },
    LazyRead {
        statement_ordinal: StatementOrdinal,
        binding: BindingId,
    },
    AtInitWrite {
        statement_ordinal: StatementOrdinal,
        binding: BindingId,
    },
    LazyWrite {
        statement_ordinal: StatementOrdinal,
        binding: BindingId,
    },
    SideEffectOrder {
        statement_ordinal: StatementOrdinal,
    },
}

impl EdgeReason {
    pub(crate) fn purity_order(statement_ordinal: StatementOrdinal) -> Self {
        Self::SideEffectOrder { statement_ordinal }
    }

    pub(crate) fn kind(&self) -> EdgeKind {
        match self {
            Self::AtInitRead { .. } => EdgeKind::AtInitRead,
            Self::LazyRead { .. } => EdgeKind::LazyRead,
            Self::AtInitWrite { .. } => EdgeKind::AtInitWrite,
            Self::LazyWrite { .. } => EdgeKind::LazyWrite,
            Self::SideEffectOrder { .. } => EdgeKind::SideEffectOrder,
        }
    }

    pub(crate) fn statement_ordinal(&self) -> StatementOrdinal {
        match self {
            Self::AtInitRead {
                statement_ordinal, ..
            }
            | Self::LazyRead {
                statement_ordinal, ..
            }
            | Self::AtInitWrite {
                statement_ordinal, ..
            }
            | Self::LazyWrite {
                statement_ordinal, ..
            }
            | Self::SideEffectOrder { statement_ordinal } => *statement_ordinal,
        }
    }

    pub(crate) fn binding(&self) -> Option<BindingId> {
        match self {
            Self::AtInitRead { binding, .. }
            | Self::LazyRead { binding, .. }
            | Self::AtInitWrite { binding, .. }
            | Self::LazyWrite { binding, .. } => Some(*binding),
            Self::SideEffectOrder { .. } => None,
        }
    }

    pub(crate) fn is_at_init_read(&self) -> bool {
        matches!(self, Self::AtInitRead { .. })
    }

    pub(crate) fn is_binding_write(&self) -> bool {
        matches!(self, Self::AtInitWrite { .. } | Self::LazyWrite { .. })
    }

    pub(crate) fn is_side_effect_order(&self) -> bool {
        matches!(self, Self::SideEffectOrder { .. })
    }

    pub(crate) fn constrains_realizability(&self) -> bool {
        // Whitelist: every variant except `LazyRead` constrains
        // realizability today. Stated positively so adding a new
        // `EdgeReason` variant forces an explicit decision here
        // instead of silently classifying it as constraining.
        matches!(
            self,
            Self::AtInitRead { .. }
                | Self::AtInitWrite { .. }
                | Self::LazyWrite { .. }
                | Self::SideEffectOrder { .. }
        )
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EdgeKind {
    AtInitRead,
    LazyRead,
    AtInitWrite,
    LazyWrite,
    SideEffectOrder,
}

/// Stable-in-run identity of an owner graph vertex. V1 owner
/// vertices are post-comma-list `StatementFacts` rows, so the id
/// is the row's source-order ordinal.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct OwnerId(pub usize);

/// Fine-grained graph before logical modules are formed. Nodes are
/// top-level owners/statements; edges are owner-level reads and
/// source-order side-effect constraints. The module dependency graph
/// is the quotient of this graph by `OwnerNode.destination`.
#[derive(Debug, Clone, Default)]
pub struct OwnerGraph {
    pub binding_table: BindingTable,
    pub nodes: Vec<OwnerNode>,
    pub graph: DiGraphMap<OwnerId, EdgeMetadata>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<BindingId>,
    pub kind: StatementKind,
    pub purity: Purity,
    pub destination: ModuleId,
}

impl OwnerGraph {
    fn record_reason(&mut self, from: OwnerId, to: OwnerId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.graph.contains_edge(from, to) {
            self.graph.add_edge(from, to, EdgeMetadata::default());
        }
        let weight = self
            .graph
            .edge_weight_mut(from, to)
            .expect("owner edge was just added");
        weight.reasons.push(reason);
    }

    pub fn iter_edges(&self) -> impl Iterator<Item = (OwnerId, OwnerId, &EdgeMetadata)> + '_ {
        self.graph.all_edges()
    }

    pub fn node(&self, id: OwnerId) -> Option<&OwnerNode> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &OwnerNode> {
        self.nodes.iter()
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
    pub fn has_at_init_read(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_at_init_read)
    }

    /// `true` if at least one reason is a side-effect ordering
    /// edge. `S` edges in an SCC make it unrealizable: the
    /// constraint is "predecessor must evaluate before
    /// successor", and a cycle has no topological emit order
    /// satisfying every such edge.
    pub fn has_purity_ordering(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_side_effect_order)
    }

    /// `true` if at least one reason is a rebinding write. These
    /// edges are rejected outright when they cross destination
    /// modules because imported ESM bindings are read-only.
    pub fn has_binding_write(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_binding_write)
    }

    /// `true` if this edge constrains realizability — at least one
    /// of its reasons is realizability-constraining (an at-init
    /// read `R`, a side-effect ordering `S` edge, or a rebinding
    /// write). Lazy read-only edges don't, because the reads they
    /// represent fire after every module in the cycle has finished
    /// evaluating.
    ///
    /// Delegates to `EdgeReason::constrains_realizability` to keep
    /// the per-edge and per-reason definitions in lockstep.
    pub fn constrains_realizability(&self) -> bool {
        self.reasons
            .iter()
            .any(EdgeReason::constrains_realizability)
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
pub struct ModuleDepGraph {
    pub binding_table: BindingTable,
    pub graph: DiGraphMap<ModuleId, EdgeMetadata>,
}

impl ModuleDepGraph {
    fn record_reason(&mut self, from: ModuleId, to: ModuleId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.graph.contains_edge(from, to) {
            self.graph.add_edge(from, to, EdgeMetadata::default());
        }
        // Safe: we just ensured the edge exists.
        let weight = self
            .graph
            .edge_weight_mut(from, to)
            .expect("edge was just added");
        weight.reasons.push(reason);
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
    pub fn has_at_init_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::has_at_init_read)
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_realizability_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_realizability)
    }
}

/// Build the fine owner graph. Module-level dependencies are not
/// created here; they are derived later by quotienting owners by
/// destination.
pub fn build_owner_graph(
    facts: &[StatementFacts],
    binding_assignment: &BTreeMap<BindingName, ModuleId>,
) -> OwnerGraph {
    let mut graph = OwnerGraph::default();
    let mut binding_table = BindingTable::default();
    let mut binding_owner = Vec::<Option<OwnerId>>::new();
    let mut declared_by_stmt = Vec::<BTreeSet<BindingId>>::with_capacity(facts.len());
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
        declared_by_stmt.push(declared);
    }

    let mut binding_assignment_by_id = vec![None; binding_table.len()];
    for (binding, destination) in binding_assignment {
        let Some(binding_id) = binding_table.get(binding) else {
            continue;
        };
        binding_assignment_by_id[binding_id.0] = Some(*destination);
    }

    for (stmt, declared) in facts.iter().zip(declared_by_stmt.iter()) {
        let id = OwnerId(stmt.ordinal.0);
        let destination = declared
            .iter()
            .filter_map(|binding_id| {
                binding_assignment_by_id
                    .get(binding_id.0)
                    .copied()
                    .flatten()
            })
            .next()
            .unwrap_or(ModuleId::ResidualEntry);
        graph.nodes.push(OwnerNode {
            id,
            statement_ordinal: stmt.ordinal,
            source_location: stmt.source_location.clone(),
            declared: declared.clone(),
            kind: stmt.kind,
            purity: stmt.purity.clone(),
            destination,
        });
        graph.graph.add_node(id);
    }

    let record_binding_edge = |graph: &mut OwnerGraph,
                               from: OwnerId,
                               binding: &BindingName,
                               make_reason: fn(StatementOrdinal, BindingId) -> EdgeReason,
                               statement_ordinal: StatementOrdinal| {
        let Some(binding_id) = binding_table.get(binding) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        let Some(Some(to)) = binding_owner.get(binding_id.0) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        graph.record_reason(from, *to, make_reason(statement_ordinal, binding_id));
    };
    for stmt in facts {
        let from = OwnerId(stmt.ordinal.0);
        for binding in &stmt.reads_at_init {
            record_binding_edge(
                &mut graph,
                from,
                binding,
                |statement_ordinal, binding| EdgeReason::AtInitRead {
                    statement_ordinal,
                    binding,
                },
                stmt.ordinal,
            );
        }
        for binding in &stmt.reads_lazy {
            record_binding_edge(
                &mut graph,
                from,
                binding,
                |statement_ordinal, binding| EdgeReason::LazyRead {
                    statement_ordinal,
                    binding,
                },
                stmt.ordinal,
            );
        }
        for binding in &stmt.writes_at_init {
            record_binding_edge(
                &mut graph,
                from,
                binding,
                |statement_ordinal, binding| EdgeReason::AtInitWrite {
                    statement_ordinal,
                    binding,
                },
                stmt.ordinal,
            );
        }
        for binding in &stmt.writes_lazy {
            record_binding_edge(
                &mut graph,
                from,
                binding,
                |statement_ordinal, binding| EdgeReason::LazyWrite {
                    statement_ordinal,
                    binding,
                },
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
    // `has_purity` is computed by `classify_expr_purity` so
    // pure literal initializers (`const X = 42`,
    // `const X = { a: 1 }`, function/class declarations without
    // observable static init) don't contribute to S. Without
    // that precision the cross-module S graph would be dense
    // enough to reject realistic specs for trivially pure const
    // sequences.
    //
    let mut previous_side_effect_owner: Option<OwnerId> = None;
    for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
        let from = OwnerId(stmt.ordinal.0);
        if let Some(to) = previous_side_effect_owner {
            graph.record_reason(from, to, EdgeReason::purity_order(stmt.ordinal));
        }
        previous_side_effect_owner = Some(from);
    }

    graph.binding_table = binding_table;
    graph
}

/// Quotient the owner graph by each owner node's destination module.
/// This is the only path that constructs the module dependency graph
/// used by validation and emit.
pub fn quotient_owner_graph(owner_graph: &OwnerGraph) -> ModuleDepGraph {
    let owner_edges = collect_owner_edge_entries(owner_graph);
    quotient_owner_graph_with_destinations(owner_graph, &owner_edges, |_, node| node.destination)
}

pub(crate) fn quotient_owner_graph_with_destinations<F>(
    owner_graph: &OwnerGraph,
    owner_edges: &[OwnerEdgeEntry],
    mut destination_for: F,
) -> ModuleDepGraph
where
    F: FnMut(OwnerId, &OwnerNode) -> ModuleId,
{
    let mut graph = ModuleDepGraph {
        binding_table: owner_graph.binding_table.clone(),
        graph: DiGraphMap::new(),
    };
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in owner_edges {
        let Some(from_node) = owner_graph.node(edge.from) else {
            continue;
        };
        let Some(to_node) = owner_graph.node(edge.to) else {
            continue;
        };
        let from = destination_for(edge.from, from_node);
        let to = destination_for(edge.to, to_node);
        if from == to {
            continue;
        }
        if edge.reason.is_side_effect_order() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        graph.record_reason(from, to, edge.reason.clone());
    }
    graph
}

#[derive(Debug, Clone)]
pub(crate) struct OwnerEdgeEntry {
    pub(crate) id: String,
    pub(crate) from: OwnerId,
    pub(crate) to: OwnerId,
    pub(crate) reason: EdgeReason,
}

pub(crate) fn collect_owner_edge_entries(owner_graph: &OwnerGraph) -> Vec<OwnerEdgeEntry> {
    let mut entries = Vec::new();
    for (from, to, weight) in owner_graph.iter_edges() {
        for reason in &weight.reasons {
            entries.push((from, to, reason.clone()));
        }
    }
    entries.sort_by(|a, b| {
        (
            a.0.0,
            a.1.0,
            a.2.kind(),
            a.2.statement_ordinal(),
            a.2.binding(),
        )
            .cmp(&(
                b.0.0,
                b.1.0,
                b.2.kind(),
                b.2.statement_ordinal(),
                b.2.binding(),
            ))
    });
    entries
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| OwnerEdgeEntry {
            id: format!("owner_edge:{idx}"),
            from,
            to,
            reason,
        })
        .collect()
}

/// Predicate shared by `materialize_logical_modules` and
/// `peelability.rs`: given a candidate set of moved owners and the
/// post-peel entry export set, report binding names that moved bodies
/// would reference free — bindings declared by an owner that stays in
/// residual but aren't on entry's export list.
///
/// SSOT for the materializer's "moved module references residual entry
/// binding(s) … not exported by entry" rejection: when this returns a
/// non-empty set, the materializer would reject and peelability marks
/// the candidate `BlockedEmitResolvability`. Mirrors the
/// `constrains_realizability` SSOT introduced in `f86e84b7e`.
///
/// Inputs:
/// - `owner_graph`: nodes carry each owner's destination module.
/// - `owner_edges`: per-reason owner-graph edges; the `binding` on
///   each edge tells us which top-level chunk binding the read
///   targets.
/// - `moved_owners`: the candidate's moved owner set (a peel of these
///   is the hypothetical change being evaluated).
/// - `entry_exported_names`: post-peel entry export set, i.e.
///   pre-existing source exports ∪ bindings of all owners that end up
///   in a logical module (the candidate's bindings are auto-exported
///   by `entry_exports_for_moved_bindings`).
pub(crate) fn peel_emit_blocked_residual_bindings(
    owner_graph: &OwnerGraph,
    owner_edges: &[OwnerEdgeEntry],
    moved_owners: &BTreeSet<OwnerId>,
    entry_exported_names: &BTreeSet<BindingName>,
) -> BTreeSet<BindingName> {
    let mut blocked = BTreeSet::new();
    for edge in owner_edges {
        if !moved_owners.contains(&edge.from) {
            continue;
        }
        if moved_owners.contains(&edge.to) {
            continue;
        }
        let Some(to_node) = owner_graph.node(edge.to) else {
            continue;
        };
        if !matches!(to_node.destination, ModuleId::ResidualEntry) {
            continue;
        }
        let Some(binding_id) = edge.reason.binding() else {
            continue;
        };
        let Some(name) = owner_graph.binding_table.name(binding_id) else {
            continue;
        };
        if entry_exported_names.contains(name) {
            continue;
        }
        blocked.insert(name.clone());
    }
    blocked
}
