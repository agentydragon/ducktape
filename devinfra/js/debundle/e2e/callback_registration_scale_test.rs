//! A returned closure can also expose its captured reader as an own method.
//! Exercise registration at scale through a real spec and emitted JavaScript.

use debundle_e2e_support::*;
use runfiles::{Runfiles, rlocation};

fn generated_source(variable: &str) -> String {
    let runfiles = Runfiles::create().expect("Bazel runfiles");
    let path = rlocation!(&runfiles, std::env::var(variable).unwrap()).unwrap();
    std::fs::read_to_string(path).unwrap()
}

fn assert_generated_case(variable: &str) {
    let source = generated_source(variable);
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

fn assert_imported_generated_case(variable: &str) {
    let source = generated_source(variable);
    let (factory, registrations) = source
        .split_once("const callback0 =")
        .expect("the owned corpus generator starts registrations at callback0");
    let provider = format!("{factory}\nexport {{ make }};\n");
    let entry =
        format!("import {{ make }} from './provider.js';\nconst callback0 ={registrations}");
    let fixture = ProgramPlacement::new(&entry)
        .with_unassigned_mode(unassigned_mode_inline())
        .add_chunk("static/provider", &provider, vec![])
        .run();
    assert_preserved_entry_output(&fixture, "0 1023\n");
}

#[test]
fn imported_attached_reader_callbacks_preserve_output() {
    assert_imported_generated_case("DEBUNDLE_ATTACHED_CALLBACK_SOURCE");
}

#[test]
fn imported_lexical_reader_callbacks_preserve_output() {
    assert_imported_generated_case("DEBUNDLE_LEXICAL_CALLBACK_SOURCE");
}
