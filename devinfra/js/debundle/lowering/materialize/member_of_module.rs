//! `member_of_module` resolution: the production-lowering bridge from the
//! `spec::MemberOfModuleTarget` surface to the `selector_solve` owner-graph
//! kernel. This is the **first use-site selector** (P4 step 3): it pins an
//! entity by *how it is consumed at a use site* — "the export consumed as
//! `mod.X`" — rather than by the target's own body (X2 `reads_member`) or its
//! own minified name. It unlocks the empty-class/superclass cluster, where
//! several byte-identical empty subclasses (`class T extends Base {}`) have no
//! internal anchor and are distinguished only by how each is consumed.
//!
//! ## Where the fact comes from (the X3 deviation from `reads_member`)
//!
//! Like `reads_member`, the raw evidence is an `obj.X` member access derived from
//! the chunk AST — but the use-site edge constrains `obj` to be a chunk-top
//! **imported** binding and replaces it with the import's **source module**
//! (resolved through the import table here). So the EDB row carries two
//! re-minify-invariant labels — the import specifier `module` and the export
//! `member` — neither of which a bundle rebuild rewrites, which is exactly what a
//! fragile minified-name pin lacks. `chunk_facts::module_member_uses_by_ordinal`
//! does the AST projection + import join; this bridge supplies the import map
//! (from `RuntimeImportFacts`) and carries the rows into the kernel's lean EDB as
//! `OwnerNode`'s `module_member_uses`, joined to owners by statement ordinal —
//! exactly as `reads_member::build_resolution` carries `member_reads`.

use std::collections::HashMap;

use analysis::OwnerGraph as FactOwnerGraph;
use spec::MemberOfModuleTarget;
use swc_ecma_ast::Module;

use super::kind_labels::statement_kind_str_for_spec;
use super::owner_graph_projection::solve_projected;

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST module-member
/// uses onto the lean owner graph the `selector_solve` kernel consumes, then run
/// the phase-1 solve. Each owner node's `module_member_uses` come from
/// `chunk_facts::module_member_uses_by_ordinal` (member accesses joined to
/// `import_sources`, the chunk's local→module-source map) keyed by statement
/// ordinal — the `member_of_module` EDB rows the owner graph alone cannot supply.
/// `member_reads` are unused by the `member_of_module` resolvers (they ride the
/// import-joined `module_member_uses` instead), so they stay empty. The shared
/// skeleton lives in `owner_graph_projection::solve_projected`.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
    import_sources: &HashMap<String, String>,
) -> selector_solve::Resolution {
    let uses_by_ordinal = chunk_facts::module_member_uses_by_ordinal(module, import_sources);
    solve_projected(graph, |node| {
        let module_member_uses = uses_by_ordinal
            .get(&node.statement_ordinal.0)
            .into_iter()
            .flatten()
            .map(|use_site| selector_solve::ModuleMemberUse {
                module: use_site.module.clone(),
                member: use_site.member.clone(),
            })
            .collect();
        (Vec::new(), module_member_uses)
    })
}

/// The minified binding the `member_of_module` target resolves to, or `None`
/// (fail-closed) when the relation does not pick out exactly one declaring owner.
///
/// The kernel chain narrows most-specific-first:
///
/// - module + member + kind: `consumes_module_member_owner_of_kind`
/// - module + member: `consumes_module_member_owner`
///
/// then `binding_for_owner`. Every step is categorical: zero-or-several anywhere
/// collapses to `None`, so an unresolvable `member_of_module` errors at the call
/// site rather than guessing.
pub(super) fn resolve_member_of_module<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &MemberOfModuleTarget,
) -> Option<&'a str> {
    let owner = match target.kind.map(statement_kind_str_for_spec) {
        Some(kind) => {
            resolution.consumes_module_member_owner_of_kind(&target.module, &target.member, kind)
        }
        None => resolution.consumes_module_member_owner(&target.module, &target.member),
    }?;
    resolution.binding_for_owner(owner)
}
