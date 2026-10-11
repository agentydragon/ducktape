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

#[test]
fn return_object_does_not_rename_unresolved_global() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function factory() { return { logger: console }; }
console.log(factory().logger === console);
export { factory };
"#,
        vec![logical_module("factory", &[Member::new("factory")])],
    ));

    assert_entry_output(&fixture, "true\n");
}

#[test]
fn one_source_binding_keeps_original_and_readable_uses() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "foo";
function factory() { return { label: a }; }
function direct() { return a; }
console.log(factory().label + ":" + direct());
export { factory, direct };
"#,
        vec![logical_module(
            "consumer",
            &[Member::new("factory"), Member::new("direct")],
        )],
    ));

    assert_entry_output(&fixture, "foo:foo\n");
}
