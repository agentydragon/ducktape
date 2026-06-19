//! `reads_member` resolution: the production-lowering bridge from the
//! `spec::ReadsMemberTarget` surface to the `selector_solve` owner-graph kernel.
//!
//! A `reads_member` member pins its target by the **member it reads** off an
//! object — "the function that reads `.uniqueId` off the codegen context" —
//! rather than by the target's own re-minify-fragile minified name. This is the
//! stable identity of the ~72 TS codegen helpers, currently name-pinned.
//!
//! ## Where the fact comes from (the X2 deviation from `cross_ref`)
//!
//! The `cross_ref` primitive (P4 step 1) rides owner→binding reference edges that
//! already live in the owner graph. `reads_member` cannot: a property name like
//! `.X` read off an arbitrary object is **not** an owner→binding reference (the
//! object is rarely a top-level binding, and the member is never one). So the
//! member-read fact is derived from the chunk's AST
//! (`chunk_facts::member_reads_by_ordinal` — the same `Member`/`PropName`
//! projection the matcher uses) and **joined to the owner** by source-order
//! ordinal here, then carried into the kernel's lean EDB as `OwnerNode`'s
//! `member_reads`. The kernel stays decoupled from the `analysis` crate (it
//! deserializes the JSON wire shape), so this projection happens in the lowering
//! crate that depends on both, exactly as `cross_ref::build_resolution` does.
//!
//! ## The object handle (ordering decision, shared with `cross_ref`)
//!
//! A `reads_member` selector may constrain the object the member is read off
//! (`object: @Anchor`). `@Anchor` names another member by its readable `name:`;
//! the kernel needs the object's *minified* binding to match the AST member-read.
//! The owner graph's `export_name` is not populated at member-resolution time
//! (see `materialize::cross_ref`), so — as with a cross-ref anchor — the object's
//! minified binding comes from the already-resolved members of the same chunk.

use analysis::OwnerGraph as FactOwnerGraph;
use analysis::reports::owner_key;
use spec::ReadsMemberTarget;
use swc_ecma_ast::Module;

use super::kind_labels::{dep_kind_str, statement_kind_str, statement_kind_str_for_spec};

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST member-reads
/// onto the lean owner graph the `selector_solve` kernel consumes, then run the
/// phase-1 solve. Each owner node's `member_reads` come from
/// `chunk_facts::member_reads_by_ordinal` joined by statement ordinal — the
/// `reads_member` EDB rows the owner graph alone cannot supply.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
) -> selector_solve::Resolution {
    let reads_by_ordinal = chunk_facts::member_reads_by_ordinal(module);
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
            member_reads: reads_by_ordinal
                .get(&node.statement_ordinal.0)
                .into_iter()
                .flatten()
                .map(|read| selector_solve::MemberRead {
                    object: read.object.clone(),
                    member: read.member.clone(),
                })
                .collect(),
            // The `member_of_module` use-site EDB is unused by the `reads_member`
            // resolvers; leave it empty.
            module_member_uses: Vec::new(),
        })
        .collect();
    // Edges are unused by the `reads_member` resolvers, but the lean graph the
    // kernel solves is whole-owner-graph shaped; project them so the same
    // `Resolution` could serve other primitives and `declares` stays correct.
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

/// The minified binding the `reads_member` target resolves to, or `None`
/// (fail-closed) when the relation does not pick out exactly one declaring owner.
///
/// `object_binding` is the *minified* name the `object: @Anchor` constraint
/// resolved to (the anchor-first handle), `None` when the selector has no object
/// constraint. The kernel chain narrows, most-specific-first:
///
/// - object + kind: `reads_member_from_owner_of_kind`
/// - object only: `reads_member_from_owner`
/// - kind only: `reads_member_owner_of_kind`
/// - neither: `reads_member_owner`
///
/// then `binding_for_owner`. Every step is categorical: zero-or-several anywhere
/// collapses to `None`, so an unresolvable `reads_member` errors at the call site
/// rather than guessing.
pub(super) fn resolve_reads_member<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &ReadsMemberTarget,
    object_binding: Option<&str>,
) -> Option<&'a str> {
    let kind = target.kind.map(statement_kind_str_for_spec);
    let owner = match (object_binding, kind) {
        (Some(object), Some(kind)) => {
            resolution.reads_member_from_owner_of_kind(object, &target.member, kind)
        }
        (Some(object), None) => resolution.reads_member_from_owner(object, &target.member),
        (None, Some(kind)) => resolution.reads_member_owner_of_kind(&target.member, kind),
        (None, None) => resolution.reads_member_owner(&target.member),
    }?;
    resolution.binding_for_owner(owner)
}
