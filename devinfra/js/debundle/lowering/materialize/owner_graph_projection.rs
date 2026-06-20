//! Shared owner-graph → `selector_solve` projection for the three
//! lowering bridges (`cross_ref`, `reads_member`, `member_of_module`).
//!
//! All three project the in-memory `analysis::OwnerGraph` onto the lean owner
//! graph the `selector_solve` kernel consumes and run the phase-1 solve. The
//! node skeleton (id, statement kind, declared bindings), the edge projection,
//! and the final `solve` are identical; the primitives differ only in each
//! node's `(member_reads, module_member_uses)` EDB rows. The kernel is
//! deliberately decoupled from the `analysis` crate's rich types (it
//! deserializes the JSON wire shape), so the projection happens here, in the
//! lowering crate that depends on both.

use analysis::OwnerGraph as FactOwnerGraph;
use analysis::OwnerNode as FactOwnerNode;
use analysis::reports::owner_key;

use super::kind_labels::{dep_kind_str, statement_kind_str};

/// Project `graph` onto the kernel's lean owner graph and run the phase-1 solve.
/// `node_extras` supplies each owner node's primitive-specific EDB rows — the
/// `(member_reads, module_member_uses)` pair the owner graph alone cannot carry;
/// every other field of the lean node, the edge projection, and the `solve` are
/// shared.
///
/// The lean graph carries no `export_name`: the spec's readable names are not on
/// the owner graph at member-resolution time (see `materialize::cross_ref`), so
/// the kernel is used through its anchor-first handles only.
pub(super) fn solve_projected(
    graph: &FactOwnerGraph,
    mut node_extras: impl FnMut(
        &FactOwnerNode,
    ) -> (
        Vec<selector_solve::MemberRead>,
        Vec<selector_solve::ModuleMemberUse>,
    ),
) -> selector_solve::Resolution {
    let nodes = graph
        .iter_nodes()
        .map(|node| {
            let (member_reads, module_member_uses) = node_extras(node);
            selector_solve::OwnerNode {
                id: owner_key(node.id),
                statement_kind: statement_kind_str(node.kind).to_string(),
                declared_bindings: node
                    .declared
                    .iter()
                    .map(|id| selector_solve::DeclaredBinding {
                        binding: id.0.as_str().to_string(),
                        export_name: None,
                    })
                    .collect(),
                member_reads,
                module_member_uses,
            }
        })
        .collect();
    let edges = graph
        .iter_edges()
        .map(|edge| selector_solve::OwnerEdge {
            source: owner_key(edge.from),
            binding: edge.reason.binding().map(|id| id.0.as_str().to_string()),
            edge_kind: dep_kind_str(edge.reason.kind()).to_string(),
        })
        .collect();
    selector_solve::solve(&selector_solve::OwnerGraph { nodes, edges })
}
