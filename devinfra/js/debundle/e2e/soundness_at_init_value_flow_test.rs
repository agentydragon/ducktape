//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

use debundle_e2e_support::*;

#[test]
fn function_passed_through_parameter_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("bar"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn constructor_body_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[Member::new("a"), Member::new("Foo"), Member::new("result")],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn callback_partition_with_initialized_value_in_dependency_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#,
        vec![
            logical_module(
                "mod_a",
                &[
                    Member::new("foo"),
                    Member::new("bar"),
                    Member::new("result"),
                ],
            ),
            logical_module("mod_b", &[Member::new("a"), Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn constructor_partition_with_initialized_value_in_dependency_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Foo"), Member::new("result")]),
            logical_module("mod_b", &[Member::new("a"), Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn callback_returned_through_parameter_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function identity(callback) { return callback; }
const result = identity(foo)();
console.log(result);
export { a, b, foo, identity, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("identity"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn callback_held_in_static_object_property_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
const box = { invoke: foo };
const result = box.invoke();
console.log(result);
export { a, b, foo, box, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("box"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}
