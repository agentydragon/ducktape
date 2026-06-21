//! Owner-graph wire-spelling for the `kind:` selector constraint: map the spec's
//! `BindingSourceKind` onto the `analysis::StatementKind` the `selector_solve`
//! kernel filters by, then spell it via that enum's `strum::IntoStaticStr` derive
//! (`#[strum(serialize_all = "snake_case")]`, identical to its serde `rename_all`,
//! so the kernel's JSON owner graph and this in-memory projection stay in lockstep
//! by construction). Used by the `cross_ref`, `reads_member`, `member_of_module`,
//! `makes_decorate_call`, and `intrinsic_alias` lowering bridges.
//!
//! `StatementKind` / `DepKind` themselves stringify directly via their
//! `strum::Display` derive at the projection sites — no passthrough wrapper here.

use analysis::StatementKind;
use spec::BindingSourceKind;

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
