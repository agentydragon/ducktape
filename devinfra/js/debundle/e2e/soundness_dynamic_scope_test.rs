//! Moving a function changes the lexical scope used by direct `eval`.
//! The input's lazy eval is admitted by the current chunk scan.

use debundle_e2e_support::*;

#[test]
fn lazy_eval_keeps_access_to_its_lexical_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 7;
function foo() { return eval("a"); }
console.log(foo());
export { a, foo };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a")]),
            logical_module("mod_foo", &[Member::new("foo")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7\n");
}
