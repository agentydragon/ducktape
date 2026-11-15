//! Freezing a fresh ordinary record cannot synchronously invoke stored callbacks.

use debundle_e2e_support::*;

fn assert_schedule_refusal_preserves_source_and_candidate(
    opts: FixtureOpts<'_>,
    expected_output: &str,
) {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected a conservative evaluation-schedule refusal:\n{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
}

#[test]
fn published_freeze_callback_split_keeps_initialization_phase_unproved() {
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
    assert_schedule_refusal_preserves_source_and_candidate(
        FixtureOpts::new(
            source,
            vec![
                logical_module("records/early", &[Member::new("early")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        "first later\n",
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "records/all",
            &[
                Member::new("makeRecord"),
                Member::new("peek"),
                Member::new("early"),
                Member::new("later"),
            ],
            &[
                "globalThis.peek = peek;",
                "console.log(early.name, globalThis.peek());",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "first later\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/records/all.js",
        &[
            "makeRecord",
            "peek",
            "early",
            "later",
            "globalThis.peek = peek",
            "console.log(early.name",
        ],
        &[],
    );
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
