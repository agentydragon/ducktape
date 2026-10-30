//! An opaque host may run the callbacks it receives in any order, any number
//! of times. A callback observes the bindings every other pool member can
//! rebind, so its TDZ observation keeps the reader before the declaration.

use debundle_e2e_support::*;

const PEER: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(first, second) { second(); first(); }\n",
)];

fn assert_observation_stays_before_declaration(source: &str) {
    let colocated = run_fixture(
        FixtureOpts::new(source, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![logical_module("value", &[Member::new("later")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value"],
        &[],
    );
}

#[test]
fn reader_before_writer_in_pool() {
    assert_observation_stays_before_declaration(
        r#"import { host } from './peer.js';
let seen = 'unset';
let read = () => 'initial';
function a() { try { seen = read(); } catch (error) { seen = error.name; } }
function b() { read = () => later; }
host(a, b);
const later = 'value';
console.log(seen);
"#,
    );
}

#[test]
fn writer_before_reader_in_pool() {
    assert_observation_stays_before_declaration(
        r#"import { host } from './peer.js';
let seen = 'unset';
let read = () => 'initial';
function b() { read = () => later; }
function a() { try { seen = read(); } catch (error) { seen = error.name; } }
host(a, b);
const later = 'value';
console.log(seen);
"#,
    );
}
