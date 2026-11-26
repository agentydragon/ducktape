use source_identity::SourceUnitId;

use crate::module_assignment::ModuleAssignment;
use analysis::ModuleId;

use super::edge::{DependencyEdge, EdgeRole};

/// Selects between the gate and lenient views in
/// [`assignment_endpoints`].
///
/// `Lenient` drops cross-module `PromotedAtInit` edges (the quotient report).
/// `Gate` keeps them for the canonical chunk-edge set and realizability checks. See
/// [`EdgeRole`] for the ESM-semantics justification.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum EndpointView {
    Lenient,
    Gate,
}

/// Assignment-projected endpoints of `edge` when it participates in
/// the module quotient view; `None` means "skip this edge."
///
/// `view` selects which projection rule the caller wants:
///
/// - [`EndpointView::Lenient`] — used by the gate crate's quotient report.
///   Drops same-module edges and cross-module [`EdgeRole::PromotedAtInit`] edges when
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
/// Invariant: every quotient-projecting consumer of the dependency graph
/// MUST route through this function (or [`project_endpoints`], its form
/// for a source unit assignment that is not a materialised [`ModuleAssignment`]) so
/// the lenient-vs-gate decision stays welded to the edge's [`EdgeRole`]
/// at one source-level point.
pub fn assignment_endpoints(
    edge: &DependencyEdge,
    assignment: &ModuleAssignment,
    view: EndpointView,
) -> Option<(ModuleId, ModuleId)> {
    project_endpoints(
        edge,
        assignment.entry_destination().module_id(),
        view,
        |source_unit| assignment.of(source_unit),
    )
}

/// [`assignment_endpoints`] under an arbitrary source unit → module
/// assignment `module_of`, with `residual` the assignment's ESM DFS
/// root. Lets the gate's speculative move overlay project edges under
/// the post-move assignment without cloning the assignment.
pub fn project_endpoints(
    edge: &DependencyEdge,
    residual: ModuleId,
    view: EndpointView,
    module_of: impl Fn(SourceUnitId) -> ModuleId,
) -> Option<(ModuleId, ModuleId)> {
    let from = module_of(edge.from);
    let to = module_of(edge.to);
    if from == to {
        return None;
    }
    // Fallback-promoted edges (marked by `callee_source_unit == edge.from`,
    // see `promote_at_init_calls`) record "this statement's
    // unresolvable at-init call may invoke chunk functions reading
    // `to`'s bindings". When the caller lands in residual, the
    // constraint is vacuous: residual is the ESM DFS root and its
    // body runs only after every transitively-imported module has
    // fully evaluated, so no at-init call from residual code can
    // observe a TDZ. Dropping the edge in BOTH views also keeps the
    // gate's assumed I-topology in sync with the emitter, which
    // emits phantom side-effect imports for moved modules but not
    // for entry.
    if let EdgeRole::PromotedAtInit { callee_source_unit } = edge.reason.role
        && callee_source_unit == edge.from
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
