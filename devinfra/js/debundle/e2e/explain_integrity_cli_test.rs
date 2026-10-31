//! `explain-integrity` joins a source unit's failed native guards to the
//! census witnesses that leave each required slot unknown.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};

// Line 1 writes an Object static, line 2 deletes through a global's value,
// and line 3's `map` needs the Array intrinsics.
const SOURCE: &str = "Object.entries = function(object) { return []; };\n\
function forget() { delete globalThis.registry.entry; }\n\
const values = [1, 2].map(value => value + 1);\n\
console.log(values.join(','), typeof forget);\n";

fn slot<'a>(report: &'a Value, name: &str) -> &'a Value {
    report["slots"]
        .as_array()
        .unwrap()
        .iter()
        .find(|slot| slot["slot"] == name)
        .unwrap_or_else(|| panic!("no {name} in {report}"))
}

/// Each witness's reach and source line.
fn reaches(slot: &Value) -> Vec<(String, u64)> {
    slot["witnesses"]
        .as_array()
        .unwrap()
        .iter()
        .map(|witness| {
            (
                witness["reach"].as_str().unwrap().to_owned(),
                witness["source_location"]["start_line"].as_u64().unwrap(),
            )
        })
        .collect()
}

fn reach(name: &str, line: u64) -> (String, u64) {
    (name.to_owned(), line)
}

#[test]
fn a_failed_guard_names_the_witnesses_behind_its_unknown_slots() {
    let fixture = GraphFixture::new(SOURCE, &[]);
    for query in [["--at", "main.js:3"], ["--id", "values"]] {
        let report = fixture.json(&["explain-integrity", query[0], query[1]]);
        // `--at` also lists units whose execution reached line 3 indirectly.
        let unit = report["source_units"]
            .as_array()
            .unwrap()
            .iter()
            .find(|unit| unit["source_location"]["start_line"] == 3)
            .unwrap_or_else(|| panic!("the `values` unit: {report}"));
        assert!(
            unit["precision_losses"]
                .as_array()
                .unwrap()
                .iter()
                .any(|loss| loss["reason"]["code"] == "unknown_call_target"),
            "{unit}"
        );
        let guard = unit["failed_guards"]
            .as_array()
            .unwrap()
            .iter()
            .find(|guard| guard["guard"] == "array_intrinsics")
            .unwrap_or_else(|| panic!("array guard failed: {unit}"));
        assert_eq!(
            guard["slots"],
            json!([
                {"slot": "%Array.prototype%", "intact": false},
                {"slot": "%ArrayIteratorPrototype%", "intact": false},
                {"slot": "%Object.prototype%", "intact": false},
            ])
        );
        // The Object static write couples only its own family's prototype.
        assert_eq!(
            reaches(slot(&report, "%Object.prototype%")),
            [reach("family_coupling", 1), reach("unknown_receiver", 2)]
        );
        let array_prototype = slot(&report, "%Array.prototype%");
        assert_eq!(reaches(array_prototype), [reach("unknown_receiver", 2)]);
        let delete = &array_prototype["witnesses"][0];
        assert_eq!(delete["receiver_global"], "registry");
        assert_eq!(
            delete["discharged_by"],
            json!([
                "predefined_globals_bundle_ownership",
                "no_accessor_install_callables"
            ])
        );
    }
}

#[test]
fn a_slot_query_lists_only_the_witnesses_that_can_name_it() {
    let fixture = GraphFixture::new(SOURCE, &[]);
    for (query, intact, expected) in [
        ("Object.entries", false, vec![reach("named_write", 1)]),
        // The same-family static write names another property.
        ("%Object.keys%", true, vec![]),
        ("Set", true, vec![]),
    ] {
        let report = fixture.json(&["explain-integrity", "--slot", query]);
        let [slot] = report["slots"].as_array().unwrap().as_slice() else {
            panic!("one slot: {report}");
        };
        assert_eq!(slot["intact"], intact, "{query}: {slot}");
        assert_eq!(reaches(slot), expected, "{query}");
    }
    let limited = fixture.json(&[
        "explain-integrity",
        "--slot",
        "Object.prototype",
        "--limit",
        "1",
    ]);
    let limited = slot(&limited, "%Object.prototype%");
    assert_eq!(reaches(limited), [reach("family_coupling", 1)]);
    assert_eq!(
        limited["witness_limit"],
        json!({"total": 2, "emitted": 1, "truncated": true})
    );
}

#[test]
fn a_failed_native_call_identity_needs_only_its_written_slot() {
    let fixture = GraphFixture::new(
        "Object.freeze = function(value) { return value; };\n\
         const frozen = Object.freeze({ a: 1 });\n\
         console.log(frozen.a);\n",
        &[],
    );
    let report = fixture.json(&["explain-integrity", "--id", "frozen"]);
    let guards = &report["source_units"][0]["failed_guards"];
    assert!(
        guards.as_array().unwrap().iter().any(|guard| {
            guard["guard"] == "native_call_identity"
                && guard["slots"]
                    == json!([
                        {"slot": "%Object%", "intact": true},
                        {"slot": "%Object.freeze%", "intact": false},
                    ])
        }),
        "{report}"
    );
    assert_eq!(
        reaches(slot(&report, "%Object.freeze%")),
        [reach("named_write", 1)]
    );
}

#[test]
fn an_unknown_slot_or_location_is_rejected() {
    let fixture = GraphFixture::new(SOURCE, &[]);
    for query in [
        ["--slot", "Foo.bar"],
        ["--at", "main.js:99"],
        ["--id", "absent"],
    ] {
        let out = fixture.command(&["explain-integrity", query[0], query[1]]);
        assert!(!out.status.success(), "{query:?}");
    }
}
