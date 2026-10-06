//! Nested-closure rebinds and reads vs. the owner graph.
//!
//! Two distinct contracts meet here:
//!
//! 1. **Init-order**: a read or rebind inside a *nested* closure does
//!    not fire when the enclosing function is invoked synchronously
//!    at module init, so at-init call promotion and the constraining
//!    `LazyRebind` edge must not manufacture init-order constraints
//!    for it (the original purpose of this test — see the production
//!    observation below).
//! 2. **ESM read-only imports**: a rebind of a binding owned by a
//!    *different* destination module is invalid at ANY time, not just
//!    at init — the emitted module imports the binding, and an
//!    assignment to an imported binding throws `TypeError` whenever
//!    it fires. Non-first-order (nested) lazy rebinds therefore emit a
//!    `DeferredRebind` edge: it participates in cross-destination-rebind
//!    rejection and forced co-location (bidirectional `G_atomic`) but does
//!    NOT constrain init order (it is excluded from the constraining-edge
//!    subgraph and the I-graph).

use debundle_e2e_support::*;

/// Contract 1: a nested-closure *read* manufactures no init-order
/// constraint. The split spec is realizable, the bundle prints the
/// init-time value, and the stored closure keeps working post-init
/// (ESM imports are readable at any time).
#[test]
fn nested_closure_read_does_not_constrain_init_order() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setupHandler() {
    globalThis.__readState = () => state;
}
setupHandler();
console.log(state);
export { state, setupHandler };
"#,
        vec![
            logical_module("mod_state", &[Member::new("state")]),
            logical_module("mod_handler", &[Member::new("setupHandler")]),
        ],
    ));
    assert_entry_output(&fixture, "initial\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "console.log(globalThis.__readState());\n",
        "initial\n",
    );
}

/// Contract 2: a nested-closure *rebind* of a binding assigned to a
/// different destination module is rejected (the emitted bundle would throw
/// `TypeError: Assignment to constant variable.` when
/// `globalThis.__updateState()` ran).
#[test]
fn nested_closure_rebind_across_destinations_is_rejected() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"let state = "initial";
function setupHandler() {
    globalThis.__updateState = () => { state = "updated"; };
}
setupHandler();
console.log(state);
export { state, setupHandler };
"#,
            vec![
                logical_module("mod_state", &[Member::new("state")]),
                logical_module("mod_handler", &[Member::new("setupHandler")]),
            ],
        ),
        &["mod_state", "mod_handler"],
        &["deferred_rebind"],
    );
}

/// Contract 2, green side: co-locating the rebinder with the binding
/// declarer is accepted, and the post-init probe observes the update
/// through the module's live export binding.
#[test]
fn nested_closure_rebind_colocated_updates_after_init() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setupHandler() {
    globalThis.__updateState = () => { state = "updated"; };
}
setupHandler();
console.log(state);
export { state, setupHandler };
"#,
        vec![logical_module(
            "mod_state",
            &[Member::new("state"), Member::new("setupHandler")],
        )],
    ));
    assert_entry_output(&fixture, "initial\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "globalThis.__updateState();\n\
         const mod = await import(\"./static/app/modules/mod_state.js\");\n\
         console.log(mod.state);\n",
        "updated\n",
    );
}
