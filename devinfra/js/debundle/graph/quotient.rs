use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::ModuleId;
use crate::partition::Partition;

use super::edge::{EdgeRole, OwnerEdge};
use super::owner_graph::{OwnerGraph, OwnerId};

/// The I∪S module dependency graph: the quotient of an [`OwnerGraph`]
/// by a [`Partition`], one edge per directed `(from, to)` module pair.
///
/// Newtype around `petgraph::DiGraphMap<ModuleId, ()>`. Mutation
/// happens only inside [`build_module_quotient`]; callers go through
/// the read-only accessors `all_edges`, `contains_edge` and `sccs`.
#[derive(Debug, Clone, Default)]
pub struct ModuleQuotient(DiGraphMap<ModuleId, ()>);

impl ModuleQuotient {
    /// Iterate over every `(from, to)` pair in the quotient.
    pub fn all_edges(&self) -> impl Iterator<Item = (ModuleId, ModuleId)> + '_ {
        self.0.all_edges().map(|(from, to, _)| (from, to))
    }

    /// `true` iff the directed edge `(from, to)` is present.
    pub fn contains_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.0.contains_edge(from, to)
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
///   the gate crate's quotient edge report builder. Drops same-module edges
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
/// MUST route through this function (or [`project_endpoints`], its form
/// for an owner assignment that is not a materialised [`Partition`]) so
/// the lenient-vs-gate decision stays welded to the edge's [`EdgeRole`]
/// at one source-level point.
pub fn partition_endpoints(
    edge: &OwnerEdge,
    partition: &Partition,
    view: EndpointView,
) -> Option<(ModuleId, ModuleId)> {
    project_endpoints(edge, partition.residual(), view, |owner| {
        partition.of(owner)
    })
}

/// [`partition_endpoints`] under an arbitrary owner → module
/// assignment `module_of`, with `residual` the assignment's ESM DFS
/// root. Lets the gate's speculative move overlay project edges under
/// the post-move assignment without cloning the partition.
pub fn project_endpoints(
    edge: &OwnerEdge,
    residual: ModuleId,
    view: EndpointView,
    module_of: impl Fn(OwnerId) -> ModuleId,
) -> Option<(ModuleId, ModuleId)> {
    let from = module_of(edge.from);
    let to = module_of(edge.to);
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
        && from == residual
    {
        return None;
    }
    if view == EndpointView::Lenient
        && edge
            .reason
            .role
            .is_cross_module_promotion(edge.from, module_of)
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
    let mut graph = DiGraphMap::new();
    for edge in owner_graph.iter_edges() {
        if let Some((from, to)) = partition_endpoints(edge, partition, EndpointView::Lenient) {
            graph.add_edge(from, to, ());
        }
    }
    ModuleQuotient(graph)
}
