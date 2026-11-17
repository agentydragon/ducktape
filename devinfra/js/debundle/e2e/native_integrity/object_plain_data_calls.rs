//! Plain-data Object call summaries admit only structurally safe arguments.

use debundle_e2e_support::*;

fn source(initializer: &str, consumer: &str) -> String {
    format!(
        "const a = (() => 1)();\nconst b = {initializer};\nconst c = {consumer} + a;\nconsole.log(c);\nexport {{ a, b, c }};\n"
    )
}

fn opts(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![logical_module("b_module", &[Member::new("b")])],
    )
    .with_import_order(&["b_module", "residual"])
}

fn assert_admits(initializer: &str, consumer: &str, call_prefix: &str, stdout: &str) {
    let source = source(initializer, consumer);
    let contains = [call_prefix];
    assert_pure_cycle_break_with_opts(opts(&source), "b_module", &contains, &["const a"], stdout);
}

fn assert_rejected(initializer: &str, consumer: &str) {
    let source = source(initializer, consumer);
    expect_pure_cycle_rejection(opts(&source), "b_module");
}

#[test]
fn object_freeze_on_plain_object_literal_admits() {
    assert_admits(
        "Object.freeze({ x: 1 })",
        "b.x",
        "const b = Object.freeze(",
        "2\n",
    );
}

#[test]
fn object_entries_on_plain_object_literal_admits() {
    assert_admits(
        "Object.entries({ x: 1, y: 2 })",
        "b.length",
        "const b = Object.entries(",
        "3\n",
    );
}

#[test]
fn object_keys_on_plain_array_literal_admits() {
    assert_admits(
        "Object.keys([10, 20, 30])",
        "b.length",
        "const b = Object.keys(",
        "4\n",
    );
}

#[test]
fn object_from_entries_on_array_of_pair_literals_admits() {
    assert_admits(
        "Object.fromEntries([[\"x\", 1], [\"y\", 2]])",
        "b.x",
        "const b = Object.fromEntries(",
        "2\n",
    );
}

#[test]
fn object_entries_on_non_literal_arg_does_not_admit() {
    assert_rejected("Object.entries((() => ({ x: 1 }))())", "b.length");
}

#[test]
fn object_values_on_object_with_getter_does_not_admit() {
    assert_rejected("Object.values({ get x() { return 1; } })", "b.length");
}
