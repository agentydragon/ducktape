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
use analysis::reports::owner_key;
use spec::{BindingSourceKind, MemberOfModuleTarget};
use swc_ecma_ast::Module;

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST module-member
/// uses onto the lean owner graph the `selector_solve` kernel consumes, then run
/// the phase-1 solve. Each owner node's `module_member_uses` come from
/// `chunk_facts::module_member_uses_by_ordinal` (member accesses joined to
/// `import_sources`, the chunk's local→module-source map) keyed by statement
/// ordinal — the `member_of_module` EDB rows the owner graph alone cannot supply.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
    import_sources: &HashMap<String, String>,
) -> selector_solve::Resolution {
    let uses_by_ordinal = chunk_facts::module_member_uses_by_ordinal(module, import_sources);
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
            // `member_reads` are unused by the `member_of_module` resolvers (they
            // ride the import-joined `module_member_uses` instead); leave empty.
            member_reads: Vec::new(),
            module_member_uses: uses_by_ordinal
                .get(&node.statement_ordinal.0)
                .into_iter()
                .flatten()
                .map(|use_site| selector_solve::ModuleMemberUse {
                    module: use_site.module.clone(),
                    member: use_site.member.clone(),
                })
                .collect(),
        })
        .collect();
    // Edges are unused by the `member_of_module` resolvers, but the lean graph the
    // kernel solves is whole-owner-graph shaped; project them so `declares` stays
    // correct (the categoricity-preserving conjunct) and the same `Resolution`
    // could serve other primitives.
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

/// Owner-graph statement-kind spelling the kernel matches on. Mirrors the
/// `#[serde(rename_all = "snake_case")]` wire spelling of
/// `analysis::StatementKind` — the kernel reads the JSON owner graph, so the
/// in-memory projection must produce the identical strings.
fn statement_kind_str(kind: analysis::StatementKind) -> &'static str {
    use analysis::StatementKind::*;
    match kind {
        VarDecl => "var_decl",
        FnDecl => "fn_decl",
        ClassDecl => "class_decl",
        Export => "export",
        Import => "import",
        SideEffect => "side_effect",
    }
}

/// The owner-graph statement kind a `kind:` selector constraint narrows to. The
/// spec carries the source-declaration kind (`BindingSourceKind`); map it onto
/// the owner-graph `statement_kind` the kernel filters by. `ImportSpecifier` has
/// no owner-graph statement-kind counterpart, so it never matches a declaring
/// consumer — fail-closed.
fn statement_kind_str_for_spec(kind: BindingSourceKind) -> &'static str {
    match kind {
        BindingSourceKind::VariableDeclarator => "var_decl",
        BindingSourceKind::FunctionDeclaration => "fn_decl",
        BindingSourceKind::ClassDeclaration => "class_decl",
        BindingSourceKind::ImportSpecifier => "import",
    }
}

/// Owner-graph edge-kind spelling the kernel matches on. Mirrors
/// `analysis::DepKind`'s snake_case wire spelling.
fn dep_kind_str(kind: analysis::DepKind) -> &'static str {
    use analysis::DepKind::*;
    match kind {
        EagerUse => "eager_use",
        LazyUse => "lazy_use",
        EagerRebind => "eager_rebind",
        LazyRebind => "lazy_rebind",
        DeferredRebind => "deferred_rebind",
        Sequenced => "sequenced",
        LocalEffect => "local_effect",
    }
}
