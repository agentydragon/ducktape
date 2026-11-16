//! Candidate-root indexing must preserve array contents reachable through
//! aliased function values passed to calls.

use debundle_e2e_support::*;
use std::fmt::Write as _;

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

#[test]
fn known_local_sink_arguments_union_shared_candidate_roots() {
    const FANOUT: usize = 512;
    let mut source = String::with_capacity(FANOUT * 110);

    source.push_str("const carrier = {\n");
    for index in 0..FANOUT {
        writeln!(source, "  field{index}: [{index}],").unwrap();
    }
    source.push_str("};\n");
    for index in 0..FANOUT {
        writeln!(source, "function make{index}() {{ return carrier; }}").unwrap();
    }
    for index in 1..FANOUT {
        writeln!(source, "const condition{index} = false;").unwrap();
    }
    source.push_str("const receiver = ");
    for index in 1..FANOUT {
        write!(source, "condition{index} ? make{index} : (").unwrap();
    }
    source.push_str("make0");
    for _ in 1..FANOUT {
        source.push(')');
    }
    source.push_str(";\nfunction sink(_factory) { return 0; }\nfunction stress() {\n");
    for _ in 0..FANOUT {
        source.push_str("  sink(receiver);\n");
    }
    source.push_str("}\nconsole.log(\"ok\");\n");

    // Keep stress uncalled: the heap summary visits its known-local sink
    // arguments without introducing opaque-call interpreter fallback.
    let fixture = run_fixture(
        FixtureOpts::new(&source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_entry_output(&fixture, "ok\n");
}
