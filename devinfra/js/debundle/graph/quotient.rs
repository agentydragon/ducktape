use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::ModuleId;
use crate::partition::Partition;

use super::edge::{EdgeMetadata, EdgeReason, EdgeRole, OwnerEdge};
use super::owner_graph::OwnerGraph;

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
///
/// Thin newtype around `petgraph::DiGraphMap<ModuleId,
/// EdgeMetadata>`: one edge per directed `(from, to)` pair, weight =
/// `EdgeMetadata`. Multiple reasons for the same physical edge (e.g.
/// several at-init reads of bindings owned by the same target
/// module) accumulate into the edge's reason list. Cycle detection
/// runs through petgraph's `tarjan_scc`.
///
/// The inner `DiGraphMap` is private. Mutation happens only inside
/// [`build_module_quotient`] (and the constructor-private
/// `record_reason` helper); callers go through the read-only
/// accessors `all_edges`, `contains_edge`, `edge_weight`,
/// `has_init_order_constraining_edge`, and the convenience
/// `sccs` wrapper around `petgraph::algo::tarjan_scc`. The
/// newtype keeps the semantic name "the I∪S module-dep quotient"
/// distinct from arbitrary `DiGraphMap<ModuleId, EdgeMetadata>`
/// instances.
#[derive(Debug, Clone, Default)]
pub struct ModuleQuotient(DiGraphMap<ModuleId, EdgeMetadata>);

impl ModuleQuotient {
    fn record_reason(&mut self, from: ModuleId, to: ModuleId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.0.contains_edge(from, to) {
            self.0.add_edge(from, to, EdgeMetadata::default());
        }
        self.0
            .edge_weight_mut(from, to)
            .unwrap()
            .reasons
            .push(reason);
    }

    /// Iterate over every `(from, to, weight)` tuple in the quotient.
    /// Forwards to `petgraph::DiGraphMap::all_edges`.
    pub fn all_edges(&self) -> impl Iterator<Item = (ModuleId, ModuleId, &EdgeMetadata)> + '_ {
        self.0.all_edges()
    }

    /// `true` iff the directed edge `(from, to)` is present.
    pub fn contains_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.0.contains_edge(from, to)
    }

    /// The metadata for `(from, to)` if the edge exists, else `None`.
    pub fn edge_weight(&self, from: ModuleId, to: ModuleId) -> Option<&EdgeMetadata> {
        self.0.edge_weight(from, to)
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_init_order_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_init_order)
    }

    /// Strongly-connected components of the quotient, via
    /// `petgraph::algo::tarjan_scc`. Each inner `Vec` is one SCC.
    pub fn sccs(&self) -> Vec<Vec<ModuleId>> {
        tarjan_scc(&self.0)
    }
}

/// Selects between the gate and lenient views in
/// [`partition_endpoints`].
///
/// `Lenient` drops cross-module `PromotedAtInit` edges (the quotient
/// builder and reports view); `Gate` keeps them (the realizability
/// gate, incremental simulator, and canonical chunk-edge set). See
/// [`EdgeRole`] for the ESM-semantics justification.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum EndpointView {
    Lenient,
    Gate,
}

/// Partition-projected endpoints of `edge` when it participates in
/// the module quotient view; `None` means "skip this edge."
///
/// `view` selects which projection rule the caller wants:
///
/// - [`EndpointView::Lenient`] — used by `build_module_quotient` and
///   `report_builders::build_quotient_edge_reports` (gate crate). Drops same-module edges
///   AND drops cross-module [`EdgeRole::PromotedAtInit`] edges when
///   the callee module differs from the caller module. ESM
///   justification: the body read fires inside a call into a
///   *different* module, so by ESM DFS post-order the callee module
///   (and its transitive imports) are fully evaluated before the
///   call returns; the manufactured `R -> target-module` constraint
///   is redundant with the already-recorded `R -> callee-module`
///   edge.
/// - [`EndpointView::Gate`] — used by `check_realizability`,
///   `IncrementalQuotient::{add,remove}_current_edge`, and
///   `chunk_constraining_module_edges`. Drops same-module edges but
///   KEEPS cross-module `PromotedAtInit` edges. The emitter's
///   `collect_phantom_side_effect_providers` adds phantom
///   side-effect imports for these edges, which can reorder ESM's
///   link DFS so the target module evaluates while the caller module
///   is still on the stack — closing a TDZ cycle the lenient view
///   would hide. See
///   `realizability::tests::promoted_edge_in_aggregator_cycle_is_unrealizable`
///   for the regression fixture.
///
/// Invariant: every quotient-projecting consumer of the owner graph
/// MUST route through this function so the lenient-vs-gate decision
/// stays welded to the edge's [`EdgeRole`] at one source-level point.
pub fn partition_endpoints(
    edge: &OwnerEdge,
    partition: &Partition,
    view: EndpointView,
) -> Option<(ModuleId, ModuleId)> {
    let from = partition.of(edge.from);
    let to = partition.of(edge.to);
    if from == to {
        return None;
    }
    // Fallback-promoted edges (marked by `callee_owner == edge.from`,
    // see `UnresolvedCallFallback`) record "this statement's
    // unresolvable at-init call may invoke chunk functions reading
    // `to`'s bindings". When the caller lands in residual, the
    // constraint is vacuous: residual is the ESM DFS root and its
    // body runs only after every transitively-imported module has
    // fully evaluated, so no at-init call from residual code can
    // observe a TDZ. Dropping the edge in BOTH views also keeps the
    // gate's assumed I-topology in sync with the emitter, which
    // emits phantom side-effect imports for moved modules but not
    // for entry.
    if let EdgeRole::PromotedAtInit { callee_owner } = edge.reason.role
        && callee_owner == edge.from
        && from == partition.residual()
    {
        return None;
    }
    if view == EndpointView::Lenient
        && edge
            .reason
            .role
            .is_cross_module_promotion(edge.from, partition)
    {
        return None;
    }
    Some((from, to))
}

/// Quotient the owner graph by `partition` to build the module
/// dependency graph consumed by validation and emit. The single
/// public construction path; validation and reports both go through
/// this for any non-hypothetical quotient.
pub fn build_module_quotient(owner_graph: &OwnerGraph, partition: &Partition) -> ModuleQuotient {
    let mut graph = ModuleQuotient(DiGraphMap::new());
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in owner_graph.iter_edges() {
        let Some((from, to)) = partition_endpoints(edge, partition, EndpointView::Lenient) else {
            continue;
        };
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        graph.record_reason(from, to, edge.reason.clone());
    }
    graph
}
