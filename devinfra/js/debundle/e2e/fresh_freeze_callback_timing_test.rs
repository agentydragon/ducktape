//! Freezing a fresh ordinary record cannot synchronously invoke stored callbacks.

use debundle_e2e_support::*;

#[test]
fn fresh_record_freeze_keeps_later_reads_deferred() {
    let source = r#"function makeRecord(name, header) {
  return Object.freeze({ name, header });
}
const peek = () => later;
globalThis.peek = peek;
const early = makeRecord('first', 'ready');
const later = 'later';
console.log(early.name, globalThis.peek());
export { early, later };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("records/early", &[Member::new("early")]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "first later\n");
}

#[test]
fn frozen_record_retains_a_callback_for_later_use() {
    let source = r#"function makeRecord(callback) {
  return Object.freeze({ callback });
}
const early = makeRecord(() => later);
const later = 'later';
console.log(early.callback());
export { early, later };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("records/early", &[Member::new("early")]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "later\n");
}

#[test]
fn effectful_argument_still_observes_the_early_value() {
    let source = r#"function makeRecord(value) {
  return Object.freeze({ value });
}
const touch = () => later;
const early = makeRecord(touch());
var later = 'later';
console.log(early.value, later);
export { early, later };
"#;
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "runtime/all",
                &[
                    Member::new("makeRecord"),
                    Member::new("touch"),
                    Member::new("early"),
                    Member::new("later"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "undefined later\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("records/early", &[Member::new("early")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["records/early", "values/later"],
        &[],
    );
}

#[test]
fn uncertain_freeze_receiver_can_invoke_callback() {
    let source = r#"const peek = () => later;
const target = new Proxy({}, { ownKeys() { globalThis.observed = peek(); return []; } });
const early = Object.freeze(target);
var later = 'later';
console.log(globalThis.observed, later);
export { early, later };
"#;
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "runtime/all",
                &[
                    Member::new("peek"),
                    Member::new("target"),
                    Member::new("early"),
                    Member::new("later"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "undefined later\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("records/early", &[Member::new("early")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["records/early", "values/later"],
        &[],
    );
}
