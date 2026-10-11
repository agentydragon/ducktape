//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

use debundle_e2e_support::*;

#[test]
fn function_passed_through_parameter_reads_initialized_value() {
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
                    Member::new("a"),
                    Member::new("foo"),
                    Member::new("bar"),
                    Member::new("result"),
                ],
            ),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn constructor_body_reads_initialized_value() {
    let fixture = run_fixture(FixtureOpts::new(
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
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}
