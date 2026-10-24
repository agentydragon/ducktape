//! Synchronous body reads preserve the source initialization timeline across modules.

use analysis::DepKind;
use debundle_e2e_support::*;

const INITIALIZED_SOURCE: &str = r#"const iA = 7;
const crossModBinding = 42;
const triggerInit = gR(iA);
function gR(x) { return crossModBinding + x; }
console.log(triggerInit);
export { iA, triggerInit, gR, crossModBinding };
"#;

#[test]
fn cross_module_at_init_call_preserves_initialized_body_read() {
    let fixture = run_fixture(FixtureOpts::new(
        INITIALIZED_SOURCE,
        vec![
            logical_module("mod_m", &[Member::new("gR")]),
            logical_module("mod_m2", &[Member::new("crossModBinding")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "49\n");

    let graph = fixture.owner_graph();
    let caller = owner_for_binding(&graph, "triggerInit");
    let initializer = owner_for_binding(&graph, "crossModBinding");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == caller
            && edge.target == initializer
            && edge.edge_kind == DepKind::Sequenced
            && edge.constrains_init_order
    }));
}

#[test]
fn cross_module_at_init_call_cannot_initialize_a_later_body_read_early() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const iA = 7;
const triggerInit = gR(iA);
function gR(x) { return crossModBinding + x; }
const crossModBinding = 42;
console.log(triggerInit);
export { iA, triggerInit, gR, crossModBinding };
"#,
            vec![
                logical_module("mod_m", &[Member::new("gR")]),
                logical_module("mod_m2", &[Member::new("crossModBinding")]),
            ],
        ),
        &["residual", "mod_m2"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn at_init_call_to_cross_module_setter_does_not_colocate_caller_with_state() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setState(value) { state = value; }
setState("updated");
console.log(state);
export { state, setState };
"#,
        vec![logical_module(
            "mod_state",
            &[Member::new("state"), Member::new("setState")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "updated\n");
}

#[test]
fn intra_module_at_init_call_preserves_initialized_body_read() {
    let fixture = run_fixture(FixtureOpts::new(
        INITIALIZED_SOURCE,
        vec![logical_module("mod_m2", &[Member::new("crossModBinding")])],
    ));
    assert_preserved_entry_output(&fixture, "49\n");
    let graph = fixture.owner_graph();
    let caller = owner_for_binding(&graph, "triggerInit");
    let initializer = owner_for_binding(&graph, "crossModBinding");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == caller
            && edge.target == initializer
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}
