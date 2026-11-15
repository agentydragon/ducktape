//! A returned closure can also expose its captured reader as an own method.
//! Exercise registration at scale through a real spec and emitted JavaScript.

use debundle_e2e_support::*;
use runfiles::{Runfiles, rlocation};

fn assert_generated_case(variable: &str) {
    let runfiles = Runfiles::create().expect("Bazel runfiles");
    let path = rlocation!(&runfiles, std::env::var(variable).unwrap()).unwrap();
    let source = std::fs::read_to_string(path).unwrap();
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0 1023\n");
}

#[test]
fn attached_reader_callbacks_preserve_output() {
    assert_generated_case("DEBUNDLE_ATTACHED_CALLBACK_SOURCE");
}

#[test]
fn lexical_reader_callbacks_preserve_output() {
    assert_generated_case("DEBUNDLE_LEXICAL_CALLBACK_SOURCE");
}
