//! `@Name` cross-reference resolution: the production-lowering bridge from the
//! `spec::CrossRefTarget` surface to the `selector_solve` owner-graph kernel.
//!
//! A `cross_ref` member pins its target by an invariant *relational edge* to a
//! separately-identified anchor member — "the function that references
//! `@Anchor`", "the var-decl that aliases `@Anchor`" — rather than by the
//! target's own re-minify-fragile minified name. The kernel
//! (`selector_solve::Resolution`) resolves at owner granularity over the chunk's
//! owner graph; this module adapts the in-memory `analysis::OwnerGraph` into the
//! kernel's lean EDB and composes the kernel chain into a single binding name.
//!
//! ## The anchor handle (ordering decision)
//!
//! The kernel offers two readable→binding handles for the `@Anchor`:
//!
//! - `owner_for_export(readable)` — uses the owner graph's `export_name`, the
//!   spec's readable member name. This requires `export_name` to be populated on
//!   the owner graph *at member-resolution time*.
//! - the **anchor-first** handle — the anchor's binding looked up from the
//!   already-resolved members of the same chunk.
//!
//! In the production pipeline the owner graph's `export_name` is **not** yet
//! populated when members resolve: `BindingReport::export_name` is filled by
//! `ChunkFactorization::export_name_for` (see `report_builders::binding_reports`),
//! and the factorization is built *after* the per-chunk plan (which is what
//! member resolution feeds). So the lean owner graph this module hands the kernel
//! carries no `export_name`, and we take the **anchor-first** path: the anchor's
//! minified binding comes from the resolved members, and the kernel resolves the
//! target through the owner graph's reference/alias edges.
//! `cross_ref_anchor_ordering_uses_resolved_member_binding` in
//! `e2e/cross_ref_lowering_test.rs` pins this down.

use analysis::OwnerGraph as FactOwnerGraph;
use analysis::reports::owner_key;
use spec::{CrossRefRelation, CrossRefTarget};

use super::kind_labels::{dep_kind_str, statement_kind_str, statement_kind_str_for_spec};

/// Project the in-memory `analysis::OwnerGraph` onto the lean owner graph the
/// `selector_solve` kernel consumes, then run the phase-1 solve. The kernel is
/// deliberately decoupled from the `analysis` crate's rich types (it deserializes
/// the JSON wire shape), so the projection happens here, in the lowering crate
/// that depends on both.
///
/// The lean graph carries no `export_name`: the spec's readable names are not on
/// the owner graph at member-resolution time (see the module doc), so the kernel
/// is used through its anchor-first handles only.
pub(super) fn build_resolution(graph: &FactOwnerGraph) -> selector_solve::Resolution {
    let nodes = graph
        .iter_nodes()
        .map(|node| selector_solve::OwnerNode {
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
            // Cross-ref resolution rides reference/alias edges, not member-read
            // or module-member-use facts, so its lean graph carries neither the
            // `reads_member` nor the `member_of_module` EDB.
            member_reads: Vec::new(),
            module_member_uses: Vec::new(),
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

/// The minified binding the `cross_ref` target resolves to, or `None` (fail-closed)
/// when the relational edge does not pick out exactly one declaring owner.
///
/// `anchor_binding` is the *minified* name the anchor member resolved to (the
/// anchor-first handle). The kernel chain is:
///
/// - `references`: `referencer_for(anchor)` / `referencer_of_kind(anchor, kind)`
///   when a `kind:` constraint disambiguates several declaring referencers →
///   `binding_for_owner`.
/// - `aliases`: `alias_owner_for(anchor)` → `binding_for_owner`.
///
/// Every step is categorical: zero-or-several anywhere collapses to `None`, so an
/// unresolvable cross-ref errors at the call site rather than guessing.
pub(super) fn resolve_cross_ref<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &CrossRefTarget,
    anchor_binding: &str,
) -> Option<&'a str> {
    let owner = match target.relation {
        CrossRefRelation::References => match target.kind {
            Some(kind) => {
                resolution.referencer_of_kind(anchor_binding, statement_kind_str_for_spec(kind))
            }
            None => resolution.referencer_for(anchor_binding),
        },
        CrossRefRelation::Aliases => resolution.alias_owner_for(anchor_binding),
    }?;
    resolution.binding_for_owner(owner)
}
