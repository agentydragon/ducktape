//! Report-only effect translation validation for anonymous root statements:
//! a top-level `receiver.property = value` runs through the kernel's store
//! rule and seals its owner when the store hits a writable own data slot.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

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
    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");
    assert_eq!(effects["decision"]["status"], "conflict", "{effects:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "0 1\n",
        "",
    );
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
