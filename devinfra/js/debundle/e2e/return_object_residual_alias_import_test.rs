//! Return-object aliases must retain the original binding identity across
//! residual, sibling, and local destinations.

use debundle_e2e_support::*;

#[test]
fn peeled_return_object_imports_renamed_residual_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "foo";
function makeResult() { return { label: a }; }
console.log(makeResult().label);
export { makeResult };
"#,
        vec![logical_module("result", &[Member::new("makeResult")])],
    ));

    assert_entry_output(&fixture, "foo\n");
}

#[test]
fn peeled_return_object_keeps_distinct_existing_reference() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "foo", label = "bar";
function makeResult() { return { label: a, other: label.toUpperCase() }; }
console.log(makeResult().label + ":" + makeResult().other);
export { makeResult };
"#,
        vec![logical_module("result", &[Member::new("makeResult")])],
    ));

    assert_entry_output(&fixture, "foo:BAR\n");
}

#[test]
fn peeled_return_object_imports_renamed_sibling_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "foo";
function makeResult() { return { label: a }; }
console.log(makeResult().label);
export { makeResult };
"#,
        vec![
            logical_module("provider", &[Member::new("a")]),
            logical_module("result", &[Member::new("makeResult")]),
        ],
    ));

    assert_entry_output(&fixture, "foo\n");
}

#[test]
fn return_object_keeps_local_binding_name() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "foo";
function makeResult() { return { label: a }; }
console.log(makeResult().label);
export { makeResult };
"#,
        vec![logical_module(
            "result",
            &[Member::new("a"), Member::new("makeResult")],
        )],
    ));

    assert_entry_output(&fixture, "foo\n");
}
