//! A returned object can be mutated through an alias before a later read.
//! Classifying the original binding's member read as pure may reorder it.

use debundle_e2e_support::*;

#[test]
fn returned_object_getter_is_observed_before_later_member_read() {
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
            logical_module("mod_a_result", &[Member::new("result")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}
