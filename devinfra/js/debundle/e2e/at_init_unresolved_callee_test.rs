//! Soundness: at-init calls whose callee can't be resolved to a
//! chunk-declared function must not be skipped by at-init call promotion
//! (`graph.rs::promote_at_init_calls`). An at-init call routed through a
//! single-assignment alias (`const g = readB; g();`) or an object-literal
//! method (`api.read()`) fires its body's TDZ-locked cross-module reads at
//! runtime, so such a statement conservatively depends eagerly on the
//! transitive lazy closures of every chunk binding it reads at-init
//! (following initializer read chains); the gate-accepted-but-TDZ shapes
//! below close a constraining cycle and are rejected.

use analysis::{DepKind, EdgeRoleReport};
use debundle_e2e_support::*;

/// `const g = readB; const r = g();` — the alias `g` is a VarDecl, not a
/// function declaration. `readB`'s body reads `B` (mod_b), and mod_b
/// eagerly reads `A` (mod_a): an asymmetric cycle that TDZs on `g()` at
/// runtime.
#[test]
fn aliased_at_init_call_closing_cross_module_cycle_is_rejected() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
function readB() { return B; }
const g = readB;
const r = g();
console.log(r);
export { A, B, g, r, readB };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("A"),
                        Member::new("readB"),
                        Member::new("g"),
                        Member::new("r"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

/// Method-call variant: `const api = { read: () => B }; api.read();`
/// — the callee is a member expression, not a bare identifier.
#[test]
fn object_literal_method_at_init_call_closing_cycle_is_rejected() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
const api = { read: () => B };
const r = api.read();
console.log(r);
export { A, B, api, r };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[Member::new("A"), Member::new("api"), Member::new("r")],
                ),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

/// Green companion: the fallback emits a *correct* constraint (not a
/// blanket rejection) when the aliased call's transitive reads are
/// acyclic — the emitted bundle runs and the cross-module read is
/// ordered.
#[test]
fn aliased_at_init_call_with_acyclic_closure_is_accepted_and_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const VALUE = "v";
const get = () => VALUE;
const indirect = get;
const r = indirect();
console.log(r);
export { VALUE, get, indirect, r };
"#,
        vec![
            logical_module("mod_val", &[Member::new("VALUE")]),
            logical_module(
                "mod_use",
                &[
                    Member::new("get"),
                    Member::new("indirect"),
                    Member::new("r"),
                ],
            ),
        ],
    ));
    assert_entry_output(&fixture, "v\n");
}

#[test]
fn plain_array_collection_methods_do_not_promote_element_function_bodies() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const later = "ready";
const tools = [{ name: "tool", systemNodeId: "tool-id", run: () => later }];
const toolCopies = tools.map((tool) => ({ ...tool }));
const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));
const ids = [...tools.filter((tool) => tool.systemNodeId).map((tool) => tool.systemNodeId)];
const copiedIds = [...toolCopies.map((tool) => tool.systemNodeId)];
const byId = {};
tools.forEach((tool) => {
  tool.systemNodeId && (byId[tool.systemNodeId] = tool.name);
});
export { later, tools, toolCopies, byName, ids, copiedIds, byId };
"#,
        vec![logical_module(
            "collections",
            &[
                Member::new("byName"),
                Member::new("ids"),
                Member::new("copiedIds"),
                Member::new("byId"),
            ],
        )],
    ));

    let graph = fixture.owner_graph();
    let later_owner = owner_for_binding(&graph, "later");
    let promoted_to_later: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == later_owner
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();
    assert!(
        promoted_to_later.is_empty(),
        "static Array collection methods must not promote lazy reads \
         from function-valued elements; got {promoted_to_later:#?}\n\n\
         Full owner graph: {graph:#?}",
    );
    assert_entry_output(&fixture, "");
}

#[test]
fn global_event_listener_callback_is_not_promoted_as_at_init() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const later = "ready";
typeof window < "u" &&
  window.addEventListener("beforeunload", () => {
    console.log(later);
  });
export { later };
"#,
        vec![logical_module("later_mod", &[Member::new("later")])],
    ));

    let graph = fixture.owner_graph();
    let later_owner = owner_for_binding(&graph, "later");
    let promoted_to_later: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == later_owner
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();
    assert!(
        promoted_to_later.is_empty(),
        "global addEventListener registration must not promote lazy callback reads; \
         got {promoted_to_later:#?}\n\nFull owner graph: {graph:#?}",
    );
    assert_entry_output(&fixture, "");
}

#[test]
fn dom_event_listener_callback_is_not_promoted_as_at_init() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const later = "ready";
const document = {
  querySelector: () => ({ addEventListener() {} }),
};
const button = document.querySelector("button");
button?.addEventListener("click", () => {
  console.log(later);
});
export { later, document, button };
"#,
        vec![logical_module("later_mod", &[Member::new("later")])],
    ));

    let graph = fixture.owner_graph();
    let later_owner = owner_for_binding(&graph, "later");
    let promoted_to_later: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == later_owner
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();
    assert!(
        promoted_to_later.is_empty(),
        "DOM addEventListener registration must not promote lazy callback reads; \
         got {promoted_to_later:#?}\n\nFull owner graph: {graph:#?}",
    );
    assert_entry_output(&fixture, "");
}
