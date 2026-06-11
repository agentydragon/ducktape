//! RED test: a direct top-level eager read of a binding declared
//! by a hoisted function declaration must NOT emit an `EagerUse`
//! owner edge that `constrains_init_order`.
//!
//! Background: `graph.rs::target_is_hoisted` (lines 576-584)
//! recognizes `StatementKind::FnDecl` as ESM-Phase-1 hoisted and
//! excludes promoted at-init reads of FnDecl bindings from the
//! call-graph closure that feeds the promoted-edge emission. But
//! the *direct* top-level eager-read path (the loop over
//! `stmt.eager_reads` around `graph.rs:342`) doesn't apply that
//! filter: any top-level `const x = f()` where `f` is a
//! hoisted `function f() {}` emits a direct `EagerUse` edge
//! `caller → f_owner` with `constrains_init_order: true`.
//!
//! ## Why this is over-conservative
//!
//! ECMAScript Phase 1 of module linking
//! (`ModuleDeclarationInstantiation`) binds every
//! `FunctionDeclaration` to its hoisted closure before any module
//! body runs. So when a residual or peeled module evaluates
//! `const x = f()` at top level, the binding `f` is guaranteed
//! initialized — the read cannot observe a TDZ, regardless of
//! which module owns `f`. Recording an `EagerUse` edge for this
//! read manufactures a cross-module init-order constraint that
//! no realizable trace actually demands.
//!
//! Compare with `target_is_hoisted` in `graph.rs:576-584`:
//!
//! > Other declared kinds (`VarDecl`, `ClassDecl`) are kept:
//! > const / let / class are TDZ-locked until their statement
//! > runs, so a cross-module read inside an at-init-called
//! > function does fire the realizability hazard.
//!
//! The same logic that justifies the FnDecl exclusion in
//! promoted reads (no TDZ on hoisted bindings) applies to direct
//! reads. The fix family: hoist the predicate to module scope
//! (or reuse it as a free function) and gate the direct
//! `stmt.eager_reads` loop with it.
//!
//! ## Fixture
//!
//! `f` lives in `mod_f`; residual reads it eagerly at top level.
//! There is no real cross-module init-order hazard: ESM Phase 1
//! hoists `f`'s binding before any module body evaluates, so the
//! `const x = f()` in residual sees a fully-bound `f` no matter
//! what order modules run in.
//!
//! ## Expected outcomes
//!
//! - **Today (RED)**: the owner-graph report emits an
//!   `EagerUse` edge for binding `f` from the residual `const x`
//!   statement to the FnDecl owner, with
//!   `constrains_init_order: true`. The assertion in
//!   `eager_use_to_fndecl_is_not_emitted` fails.
//! - **After the proposed tightening**: the direct-read path in
//!   `graph.rs` applies the same `target_is_hoisted` filter the
//!   promoted-read path uses, so no `EagerUse` edge to a FnDecl
//!   owner is emitted. The assertion passes.

use analysis::{DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

#[test]
fn eager_use_to_fndecl_is_not_emitted() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function f() { return "from-f"; }
const x = f();
console.log(x);
export { f };
"#,
        vec![logical_module("mod_f", &[Member::new("f")])],
    ));

    assert_entry_output(&fixture, "from-f\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));

    let fndecl_owner = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding_report| binding_report.binding == "f")
        })
        .expect("FnDecl owner for `f` should exist in owner graph");

    let offending_edges: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == fndecl_owner.id
                && edge.edge_kind == DepKind::EagerUse
                && edge.binding.as_deref() == Some("f")
                && edge.constrains_init_order
        })
        .collect();

    assert!(
        offending_edges.is_empty(),
        "no EagerUse edge to a FnDecl owner should constrain init order — \
         ESM Phase 1 hoists FunctionDeclaration bindings before any \
         module body runs, so a direct top-level read of `f` cannot \
         observe a TDZ. Offending edges: {offending_edges:#?}\n\nFull \
         owner graph: {graph:#?}",
    );
}
