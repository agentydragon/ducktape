//! A direct top-level eager read of a binding declared by a hoisted
//! function declaration must NOT emit an `EagerUse` owner edge that
//! `constrains_init_order`.
//!
//! ECMAScript Phase 1 of module linking (`ModuleDeclarationInstantiation`)
//! binds every `FunctionDeclaration` to its hoisted closure before any
//! module body runs, so `const x = f()` at top level cannot observe a TDZ
//! whichever module owns `f`; an `EagerUse` edge for it would manufacture a
//! cross-module init-order constraint no realizable trace demands. `const` /
//! `let` / class declarations are TDZ-locked until their statement runs and
//! keep their edges. The direct-read path in `graph/` applies the same
//! `target_is_hoisted` filter as the promoted-read path.
//!
//! Fixture: `f` lives in `mod_f`; residual reads it eagerly at top level.

use analysis::DepKind;
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

    let graph = fixture.owner_graph();

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
