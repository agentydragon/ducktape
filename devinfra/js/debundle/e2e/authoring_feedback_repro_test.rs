//! Manual characterization reproducers for debug/2026_10_02_authoring_feedback.md.
//! These assert the CURRENT limitations, not the desired future contract.
//! Replace the corresponding assertion when implementing each request.
//! All programs and selectors are independently invented synthetic examples.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_entry_output, assert_module_source, logical_module, run_fixture,
};

#[test]
fn readable_parameter_placeholder_does_not_name_emitted_local() {
    let fixture = run_fixture(FixtureOpts::new(
        "function a(b) { return b + 2; } console.log(a(5)); export { a };",
        vec![logical_module(
            "arithmetic",
            &[Member::source_alpha(
                "addTwo",
                "function selected(amount) { return amount + 2; }",
            )],
        )],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/arithmetic.js",
        &["function addTwo(b)", "return b + 2"],
        &["function addTwo(amount)"],
    );
    assert_entry_output(&fixture, "7\n");
    println!("R4: emitted addTwo(b), not the selector's addTwo(amount); Node printed 7");
}
