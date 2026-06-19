//! Owner-graph wire-spelling for the enum kinds the `selector_solve` kernel
//! matches on. The kernel reads the JSON owner graph, so these in-memory
//! projections must produce strings identical to the `#[serde(rename_all =
//! "snake_case")]` spelling of `analysis::{StatementKind, DepKind}`. Shared by
//! the `cross_ref`, `reads_member`, and `member_of_module` lowering bridges.

use spec::BindingSourceKind;

/// Owner-graph statement-kind spelling for an `analysis::StatementKind`.
pub(super) fn statement_kind_str(kind: analysis::StatementKind) -> &'static str {
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

/// The owner-graph statement kind a `kind:` selector constraint narrows to. Maps
/// the spec's source-declaration kind onto the owner-graph `statement_kind` the
/// kernel filters by. `ImportSpecifier` has no owner-graph statement-kind
/// counterpart, so it never matches a declaring owner — fail-closed.
pub(super) fn statement_kind_str_for_spec(kind: BindingSourceKind) -> &'static str {
    match kind {
        BindingSourceKind::VariableDeclarator => "var_decl",
        BindingSourceKind::FunctionDeclaration => "fn_decl",
        BindingSourceKind::ClassDeclaration => "class_decl",
        BindingSourceKind::ImportSpecifier => "import",
    }
}

/// Owner-graph edge-kind spelling for an `analysis::DepKind`.
pub(super) fn dep_kind_str(kind: analysis::DepKind) -> &'static str {
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
