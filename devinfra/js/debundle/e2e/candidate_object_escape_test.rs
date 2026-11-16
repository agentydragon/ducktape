//! Candidate-root indexing must preserve array contents reachable through
//! aliased function values passed to calls.

use debundle_e2e_support::*;

#[test]
fn aliased_factories_escape_their_shared_record_arrays() {
    let source = r#"
const carrier = { first: [11], last: [29] };
function makeFirst() { return carrier; }
function makeSecond() { return carrier; }
const receiver = true ? makeFirst : makeSecond;
const host = {
  consume(factory) {
    const value = factory();
    console.log(value.first[0], value.last[0]);
  }
};
host.consume(receiver);
host.consume(receiver);
"#;
    let fixture = run_fixture(
        FixtureOpts::new(source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_entry_output(&fixture, "11 29\n11 29\n");
}
