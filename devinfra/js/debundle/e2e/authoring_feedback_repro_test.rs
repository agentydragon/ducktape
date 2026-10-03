//! Manual characterization reproducers for debug/2026_10_02_authoring_feedback.md.
//! These assert the CURRENT limitations, not the desired future contract.
//! Replace the corresponding assertion when implementing each request.
//! All programs and selectors are independently invented synthetic examples.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, Member, assert_entry_output, assert_module_source, find_outcome,
    logical_module, logical_module_with_binding_groups, outcomes, run_fixture, validate_json,
};

#[test]
fn multi_statement_mismatch_has_no_near_miss() {
    let make_spec = || {
        vec![logical_module_with_binding_groups(
            "limits",
            &[],
            &[BindingGroup::source_alpha(
                "const lower = 7; const upper = 8;",
                &[("lower", "Lower"), ("upper", "Upper")],
            )],
        )]
    };
    // Control proves that this is a supported selector form.
    let control = validate_json(FixtureOpts::new("const a = 7; const b = 8;", make_spec()));
    assert!(outcomes(&control).is_empty(), "{control:#}");

    let report = validate_json(FixtureOpts::new("const a = 7; const b = 9;", make_spec()));
    println!("R2: {report:#}");
    for name in ["Lower", "Upper"] {
        let record = find_outcome(outcomes(&report), "no_match", name);
        assert!(record["outcome"].get("nearest_unclaimed").is_none());
        assert!(record["outcome"].get("reason").is_none());
    }
}

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
