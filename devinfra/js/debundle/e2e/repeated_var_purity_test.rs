//! A module-scope `var` initializer inside a block writes the same binding
//! as a direct top-level declaration. Purity inference must not use only the
//! direct initializer to claim later member reads are accessor-free.

use debundle_e2e_support::*;

const REPEATED_VAR_PURITY_ENTRY: &str = r#"var state = { value: "before" };
function readValue() { return state.value; }
const before = readValue();
if (true) {
  var state = { get value() { console.log("getter"); return "after"; } };
}
const after = readValue();
console.log(before, after);
export { before, after };
"#;

#[test]
fn nested_var_reinitializer_does_not_let_split_drop_getter_ordering() {
    // `readValue` looks pure only if `state` is treated as PlainData. The
    // block's `var state` replaces it with an accessor, so `before` must
    // stay ordered before that write. Splitting the lazy read into `before`
    // and the binding into `state` would otherwise create a link-order
    // dependency in the opposite direction and must be rejected.
    expect_rejection(
        FixtureOpts::new(
            REPEATED_VAR_PURITY_ENTRY,
            vec![
                logical_module("before", &[Member::new("readValue"), Member::new("before")]),
                logical_module("state", &[Member::new("state")]),
                logical_module("after", &[Member::new("after")]),
            ],
        ),
        &["atomic-factor-unit conflict", "cycle"],
    );
}

#[test]
fn repeated_var_purity_fixture_preserves_original_runtime_order() {
    // With no owner split the input remains in source order: the first
    // helper call sees the plain object, while only the second fires the
    // accessor installed by the nested var declaration.
    let fixture = run_fixture(FixtureOpts::new(REPEATED_VAR_PURITY_ENTRY, vec![]));
    assert_entry_output(&fixture, "getter\nbefore after\n");
}
