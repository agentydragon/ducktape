//! Desired behavior for a shared top-level `var` counter in consecutive loops.

use debundle_e2e_support::{FixtureOpts, assert_entry_output, run_fixture};

#[test]
fn repeated_var_loop_counter_preserves_execution() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"for (var i = 0; i < 2; i++) console.log(i);
for (var i = 3; i < 5; i++) console.log(i);
console.log(i);
"#,
        vec![],
    ));
    // Both initializers run in order; `var` leaves one shared binding after the loops.
    assert_entry_output(&fixture, "0\n1\n3\n4\n5\n");
}
