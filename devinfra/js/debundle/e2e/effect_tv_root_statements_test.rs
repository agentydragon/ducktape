//! Report-only effect translation validation for anonymous root statements:
//! a top-level `receiver.property = value` runs through the kernel's store
//! rule and seals its owner when the store hits a writable own data slot.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn source_path(rejected: &RejectedFixture) -> std::path::PathBuf {
    rejected
        .source_root
        .join(format!("{}.js", rejected.chunk_id))
}

/// Both owners of the swapped pair are covered, and the verdict names the
/// `value` slot they both touch: a modeled conflict, not uncovered havoc.
fn assert_covered_slot_conflict(rejected: &RejectedFixture) {
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_eq!(effects["decision"]["status"], "conflict", "{effects:#}");
    assert_eq!(effects["decision"]["havoc"], false, "{effects:#}");
    assert!(
        examples(&effects).iter().any(|row| {
            row["havoc"] == false
                && row["cells"].as_array().is_some_and(|cells| {
                    cells.iter().any(|cell| {
                        cell["cell"]["kind"] == "own_property"
                            && cell["cell"]["property"] == "value"
                    })
                })
        }),
        "{effects:#}"
    );
}

fn assert_owner_coverage_refusal(fixture: &Fixture, reason: &str, case: &str) {
    let report = fixture.effect_translation_validation();
    assert!(
        report["coverage"]["owner_coverage_refusals"][reason]
            .as_u64()
            .is_some_and(|owners| owners > 0),
        "{case}: no owner refused with {reason}: {report:#}"
    );
}

#[test]
fn a_store_to_an_existing_record_slot_commutes_with_an_independent_declaration() {
    for (value, expected) in [
        ("1", "7 1\n"),
        ("independentLabel", "7 label\n"),
        ("() => 'made'", "7 made\n"),
    ] {
        let store = format!("record.value = {value};");
        let source = format!(
            "const independentLabel = 'label';\nconst record = {{ value: 0 }};\nconst independent = 7;\n{store}\nconst shown = typeof record.value === 'function' ? record.value() : record.value;\nconsole.log(independent, shown);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "record",
                &[Member::new("independentLabel"), Member::new("record")],
                &[store.as_str()],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_verdict_accepts(&fixture, &store);
    }
}

/// Opposing control: the relocated store runs before a declaration that
/// reads the slot it writes. Both owners are covered, so the effect verdict
/// names the cell conflict; the emitted program prints the new value where
/// the source prints the old one.
#[test]
fn a_store_moved_before_a_reader_of_its_slot_is_a_cell_conflict() {
    let source = "const record = { value: 0 };\nfunction read() { return record.value; }\nconst before = read();\nrecord.value = 1;\nconsole.log(before, record.value);\nexport { before };\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("record", &[Member::new("record")], &["record.value = 1;"]),
            logical_module("reader", &[Member::new("read"), Member::new("before")]),
        ],
    ));
    assert_covered_slot_conflict(&rejected);
    assert_node_output(&source_path(&rejected), "0 1\n", "");
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

/// Headline witness: two stores to different records are ordered by the
/// legacy strict chain, so moving the later one ahead is a legacy refusal.
/// Each store writes only its own slot, the effect verdict accepts the
/// swap, and the emitted program prints what the source prints.
#[test]
fn stores_to_different_records_commute_where_the_legacy_chain_refuses() {
    let source = "const first = { value: 0 };\nconst second = { value: 0 };\nfirst.value = 1;\nsecond.value = 2;\nconsole.log(first.value, second.value);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "second",
            &[Member::new("second")],
            &["second.value = 2;"],
        )],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert!(
        effects["swapped_pairs"]
            .as_u64()
            .is_some_and(|pairs| pairs > 0),
        "{effects:#}"
    );
    assert_eq!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "1 2\n",
        "",
    );
    assert_node_output(&rejected.entry_path(), "1 2\n", "");
}

/// Valid splits the effect verdict cannot prove yet: a global publication
/// and a store that adds a property change state outside the record slots
/// the store rule models. Each keeps the typed `unproved_field_store`
/// refusal; once a rule covers it, this fails and becomes a positive.
#[test]
fn global_publications_and_new_properties_are_pending_a_store_rule() {
    for store in ["globalThis.saved = 1;", "record.extra = 1;"] {
        let source = format!(
            "const record = {{ value: 0 }};\nconst independent = 7;\n{store}\nconsole.log(independent, record.extra ?? globalThis.saved);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "record",
                &[Member::new("record")],
                &[store],
            )],
        ));
        assert_preserved_entry_output(&fixture, "7 1\n");
        assert_effect_verdict_pending(&fixture, "incomplete", store);
        assert_owner_coverage_refusal(&fixture, "unproved_field_store", store);
    }
}

/// The kernel's store rule is shared by every kernel body: a store inside a
/// local callable covers the initializer that calls it, as a root statement's
/// store covers the statement.
#[test]
fn a_store_inside_a_callable_commutes_with_an_independent_declaration() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 0 };\nconst other = { value: 5 };\nconst independent = 7;\nfunction set() { record.value = other.value; return 1; }\nconst done = set();\nconsole.log(independent, done, record.value);\n",
        vec![logical_module(
            "writer",
            &[
                Member::new("record"),
                Member::new("other"),
                Member::new("set"),
                Member::new("done"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 1 5\n");
    assert_effect_verdict_accepts(&fixture, "callable store");
}

/// A comma initializer has no call, so neither ordered lane selects it and the
/// store rule never runs: the owner stays `unsupported_initializer`. Once a
/// lane admits it, this fails and becomes a positive.
#[test]
fn a_store_inside_a_comma_initializer_is_pending_an_ordered_lane() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 0 };\nconst independent = 7;\nconst done = (record.value = 5, 1);\nconsole.log(independent, done, record.value);\n",
        vec![logical_module(
            "writer",
            &[Member::new("record"), Member::new("done")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 1 5\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "comma store");
    assert_owner_coverage_refusal(&fixture, "unsupported_initializer", "comma store");
}

/// Opposing control for the callable store: moving the writer's owner ahead
/// of a reader of the slot is a covered conflict, and the moved program
/// prints the new value.
#[test]
fn a_callable_store_moved_before_a_reader_of_its_slot_is_a_cell_conflict() {
    let source = "const record = { value: 0 };\nfunction read() { return record.value; }\nfunction set() { record.value = 1; return 1; }\nconst before = read();\nconst done = set();\nconsole.log(before, done, record.value);\nexport { before, done };\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "writer",
                &[
                    Member::new("record"),
                    Member::new("set"),
                    Member::new("done"),
                ],
            ),
            logical_module("reader", &[Member::new("read"), Member::new("before")]),
        ],
    ));
    assert_covered_slot_conflict(&rejected);
    assert_node_output(&source_path(&rejected), "0 1 1\n", "");
    assert_node_output(&rejected.entry_path(), "1 1 1\n", "");
}

/// A store through an accessor runs the setter: the store is not an own data
/// write, keeps the typed `unproved_field_store` refusal, and moving it ahead
/// of a reader of what the setter writes changes the output.
#[test]
fn a_store_through_an_own_setter_stays_uncovered() {
    let source = "const log = [];\nconst record = { set value(v) { log.push(v); } };\nconst seen = log.length;\nrecord.value = 1;\nconsole.log(seen, log.length);\nexport { seen };\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "record",
                &[Member::new("log"), Member::new("record")],
                &["record.value = 1;"],
            ),
            logical_module("reader", &[Member::new("seen")]),
        ],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_ne!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert!(
        effects["coverage"]["owner_coverage_refusals"]["unproved_field_store"]
            .as_u64()
            .is_some_and(|owners| owners > 0),
        "{effects:#}"
    );
    assert_node_output(&source_path(&rejected), "0 1\n", "");
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

/// `Object.freeze` moved ahead of the store makes the strict-mode store
/// throw. The freeze of another statement's record is not a covered native
/// row, so the verdict cannot accept the swap.
#[test]
fn a_freeze_moved_ahead_of_the_store_is_refused() {
    let source = "const record = { value: 0 };\nrecord.value = 1;\nObject.freeze(record);\nconsole.log(record.value);\n";
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "frozen",
            &[Member::new("record")],
            &["Object.freeze(record);"],
        )],
    ));
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_ne!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert_node_output(&source_path(&rejected), "1\n", "");
    assert_node_error(&rejected.entry_path(), "TypeError");
}

/// Stores the prefix cannot prove are writable own data slots of an unshared
/// record keep the typed refusal: a slot made non-writable (the store
/// throws in both programs), a record published to the global object, and a
/// receiver that may be either of two records.
#[test]
fn unproved_receivers_keep_the_typed_field_store_refusal() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 0 };\nObject.defineProperty(record, 'value', { writable: false });\nconst independent = 7;\nrecord.value = 1;\nconsole.log(independent);\n",
        vec![logical_module("marker", &[Member::new("independent")])],
    ));
    assert_preserved_entry_error(&fixture, "TypeError");
    assert_owner_coverage_refusal(&fixture, "unproved_field_store", "non-writable slot");

    for (setup, store) in [
        ("globalThis.keep = record;", "record.value = 1;"),
        (
            "const alias = Math.random() < 2 ? record : other;",
            "alias.value = 1;",
        ),
    ] {
        let source = format!(
            "const record = {{ value: 0 }};\nconst other = {{ value: 0 }};\n{setup}\nconst independent = 7;\n{store}\nconsole.log(independent, record.value);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "record",
                &[Member::new("record"), Member::new("other")],
                &[setup, store],
            )],
        ));
        assert_preserved_entry_output(&fixture, "7 1\n");
        assert_effect_verdict_pending(&fixture, "incomplete", store);
        assert_owner_coverage_refusal(&fixture, "unproved_field_store", store);
    }
}

/// A receiver read through a proven alias of one record is that record's
/// slot: the store covers its owner.
#[test]
fn a_store_through_a_proven_alias_commutes() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 0 };\nconst alias = record;\nconst independent = 7;\nalias.value = 1;\nconsole.log(independent, record.value);\n",
        vec![logical_module_with_anon(
            "record",
            &[Member::new("record"), Member::new("alias")],
            &["alias.value = 1;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 1\n");
    assert_effect_verdict_accepts(&fixture, "alias.value = 1;");
}

/// An admitted store whose kernel attempt does not seal (a generator's
/// creation has no closure receipt) rewinds to the flattened lane and is
/// labelled `unproved_statement_operation`.
#[test]
fn an_unsealed_store_attempt_is_labelled_and_rewound() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 0 };\nconst independent = 7;\nrecord.value = function* () {};\nconsole.log(independent, typeof record.value);\n",
        vec![logical_module_with_anon(
            "record",
            &[Member::new("record")],
            &["record.value = function* () {};"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 function\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "generator store");
    assert_owner_coverage_refusal(&fixture, "unproved_statement_operation", "generator store");
}
