//! Entry imports every emitted owner. Prioritizing the asymmetric cycle
//! before its mediator makes DFS finish the dependency and consumer before
//! the mediator invokes them. This tests the supplied global priority and
//! the emitter's universal entry imports against Node execution.

use debundle_e2e_support::*;

#[test]
fn three_module_mediator_into_asymmetric_cycle_runs_under_node() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const dep_value = "alpha";
const cross_value = dep_value + "-beta";
function lazy_reader() { return cross_value; }
function mediator_helper() { return dep_value + "-via-mediator-" + lazy_reader(); }
const mediator_init = mediator_helper();
console.log(mediator_init);
export { mediator_init };
"#,
            vec![
                logical_module(
                    "mod_dep",
                    &[Member::new("dep_value"), Member::new("lazy_reader")],
                ),
                logical_module("mod_dependent", &[Member::new("cross_value")]),
                logical_module(
                    "mod_mediator",
                    &[Member::new("mediator_helper"), Member::new("mediator_init")],
                ),
            ],
        )
        .with_import_order(&["mod_dependent", "mod_dep", "mod_mediator"]),
    );
    assert_entry_output(&fixture, "alpha-via-mediator-alpha-beta\n");
}
