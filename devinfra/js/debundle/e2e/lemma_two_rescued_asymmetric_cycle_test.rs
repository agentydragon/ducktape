//! An explicit dependent-first discovery priority enters the asymmetric import
//! cycle through its eager reader. DFS descends to the dependency, skips the
//! gray back-edge, and initializes the read target before its consumer.
//! The verifier and emitted Node program must agree on this witness.

use debundle_e2e_support::*;

#[test]
fn two_module_asymmetric_cycle_with_only_residual_entrant_runs_under_node() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() { return cross_value; }
console.log(dep_value, cross_value, lazy_reader());
export { dep_value, cross_value, lazy_reader };
"#,
            vec![
                logical_module(
                    "mod_dep",
                    &[Member::new("dep_value"), Member::new("lazy_reader")],
                ),
                logical_module("mod_dependent", &[Member::new("cross_value")]),
            ],
        )
        .with_import_order(&["mod_dependent", "mod_dep"]),
    );
    assert_entry_output(&fixture, "alpha alpha-beta alpha-beta\n");
}
