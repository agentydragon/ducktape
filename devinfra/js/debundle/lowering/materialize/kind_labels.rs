//! Owner-graph wire-spelling for the enum kinds the `selector_solve` kernel
//! matches on. The kernel reads the JSON owner graph, so these in-memory
//! projections must produce strings identical to the `#[serde(rename_all =
//! "snake_case")]` spelling of `analysis::{StatementKind, DepKind}`. The
//! single-enum cases delegate to each enum's `strum::IntoStaticStr` derive
//! (`#[strum(serialize_all = "snake_case")]`), which is the same `heck`
//! snake_case as serde, so the two spellings stay in lockstep by construction.
//! Shared by the `cross_ref`, `reads_member`, and `member_of_module` lowering
//! bridges.

use analysis::StatementKind;
use spec::BindingSourceKind;

/// Owner-graph statement-kind spelling for an `analysis::StatementKind`.
pub(super) fn statement_kind_str(kind: analysis::StatementKind) -> &'static str {
    kind.into()
}

/// The owner-graph statement kind a `kind:` selector constraint narrows to. Maps
/// the spec's source-declaration kind onto the owner-graph `StatementKind` the
/// kernel filters by, then spells it via that enum's strum derive (so no
/// snake_case is hand-typed here). `ImportSpecifier` projects onto
/// `StatementKind::Import`, whose owners declare no chunk-top binding, so the
/// constraint never matches a declaring owner — fail-closed.
pub(super) fn statement_kind_str_for_spec(kind: BindingSourceKind) -> &'static str {
    let statement_kind = match kind {
        BindingSourceKind::VariableDeclarator => StatementKind::VarDecl,
        BindingSourceKind::FunctionDeclaration => StatementKind::FnDecl,
        BindingSourceKind::ClassDeclaration => StatementKind::ClassDecl,
        BindingSourceKind::ImportSpecifier => StatementKind::Import,
    };
    statement_kind.into()
}

/// Owner-graph edge-kind spelling for an `analysis::DepKind`.
pub(super) fn dep_kind_str(kind: analysis::DepKind) -> &'static str {
    kind.into()
}
