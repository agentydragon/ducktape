//! A returned object can be mutated through an alias before a later read.
//! Classifying the original binding's member read as pure may reorder it.

use debundle_e2e_support::*;

#[test]
fn direct_alias_getter_is_observed_before_later_member_read() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const foo = { value: 1 };
const alias = foo;
Object.defineProperty(alias, "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, alias, result };
"#,
            vec![
                logical_module("mod_b_foo", &[Member::new("foo"), Member::new("alias")]),
                logical_module("mod_a_result", &[Member::new("result")]),
            ],
        ),
        &["mod_a_result", "anon_residual_sentinel"],
    );
}

#[test]
fn returned_object_getter_is_observed_before_later_member_read() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const foo = { value: 1 };
function getFoo() { return foo; }
const alias = getFoo();
Object.defineProperty(alias, "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, getFoo, alias, result };
"#,
            vec![
                logical_module(
                    "mod_b_foo",
                    &[
                        Member::new("foo"),
                        Member::new("getFoo"),
                        Member::new("alias"),
                    ],
                ),
                logical_module("mod_a_result", &[Member::new("result")]),
            ],
        ),
        &["mod_a_result", "anon_residual_sentinel"],
    );
}

#[test]
fn logically_selected_returned_object_is_not_plain_data() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const foo = { value: 1 };
function getFoo() { return foo; }
const alias = getFoo() || {};
Object.defineProperty(alias, "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, getFoo, alias, result };
"#,
            vec![
                logical_module(
                    "mod_b_foo",
                    &[
                        Member::new("foo"),
                        Member::new("getFoo"),
                        Member::new("alias"),
                    ],
                ),
                logical_module("mod_a_result", &[Member::new("result")]),
            ],
        ),
        &["mod_a_result", "anon_residual_sentinel"],
    );
}

#[test]
fn returned_object_mutation_and_read_in_same_module_run_in_order() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const foo = { value: 1 };
function getFoo() { return foo; }
const alias = getFoo();
Object.defineProperty(alias, "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, getFoo, alias, result };
"#,
        vec![
            logical_module(
                "mod_b_foo",
                &[
                    Member::new("foo"),
                    Member::new("getFoo"),
                    Member::new("alias"),
                ],
            ),
            logical_module_with_anon(
                "mod_a_result",
                &[Member::new("result")],
                &[r#"Object.defineProperty(alias, "value", { get() { return 2; } });"#],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn returned_object_without_mutation_remains_plain_data() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const foo = { value: 1 };
function getFoo() { return foo; }
const alias = getFoo();
const result = foo.value;
console.log(result);
export { foo, getFoo, alias, result };
"#,
        vec![
            logical_module(
                "mod_foo",
                &[
                    Member::new("foo"),
                    Member::new("getFoo"),
                    Member::new("alias"),
                ],
            ),
            logical_module("mod_result", &[Member::new("result")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1\n");
    let graph = fixture.owner_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == "result"))
        .expect("result owner");
    let purity = serde_json::to_value(&result.purity).expect("serialize purity");
    assert_eq!(
        purity["kind"], "pure",
        "returned object without mutation: {purity}"
    );
}

#[test]
fn returned_object_mutated_without_named_alias_is_not_plain_data() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const foo = { value: 1 };
function getFoo() { return foo; }
Object.defineProperty(getFoo(), "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, getFoo, result };
"#,
            vec![
                logical_module("mod_foo", &[Member::new("foo"), Member::new("getFoo")]),
                logical_module("mod_result", &[Member::new("result")]),
            ],
        ),
        &["mod_result", "anon_residual_sentinel"],
    );
}

#[test]
fn returned_object_mutated_through_static_property_is_not_plain_data() {
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            r#"const foo = { value: 1 };
function getFoo() { return foo; }
const box = { item: getFoo() };
Object.defineProperty(box.item, "value", { get() { return 2; } });
const result = foo.value;
console.log(result);
export { foo, getFoo, box, result };
"#,
            vec![
                logical_module(
                    "mod_foo",
                    &[
                        Member::new("foo"),
                        Member::new("getFoo"),
                        Member::new("box"),
                    ],
                ),
                logical_module("mod_result", &[Member::new("result")]),
            ],
        ),
        &["mod_result", "anon_residual_sentinel"],
    );
}
