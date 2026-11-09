//! `explain-integrity` joins a source unit's failed native guards to the
//! census witnesses that leave each required slot unknown.

use debundle_e2e_graph_support::GraphFixture;
use serde_json::{Value, json};

// Line 1 writes an Object static, line 2 deletes `map` through a global's
// value, and line 3's `map` needs `%Array.prototype.map%`.
const SOURCE: &str = "Object.entries = function(object) { return []; };\n\
function forget() { delete globalThis.registry.map; }\n\
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
        // The Object static write names neither another object's property
        // nor the constructor's Symbol keys.
        assert_eq!(
            guard["slots"],
            json!([
                {"slot": "%Array.prototype.constructor%", "intact": true},
                {"slot": "%Array.prototype.map%", "intact": false},
                {"slot": "%Array[@@species]%", "intact": true},
            ])
        );
        let array_prototype = slot(&report, "%Array.prototype.map%");
        assert_eq!(array_prototype["intact"], false);
        assert_eq!(reaches(array_prototype), [reach("unknown_receiver", 2)]);
        let delete = &array_prototype["witnesses"][0];
        assert_eq!(delete["receiver_global"], "registry");
        assert_eq!(
            delete["possible_discharges"],
            json!([
                "intrinsics_never_written",
                "predefined_globals_bundle_ownership",
                "no_accessor_install_callables"
            ])
        );
        assert!(delete.get("discharged_by").is_none());
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
    assert_eq!(limited["intact"], false);
    assert_eq!(
        limited["witnesses"][0]["possible_discharges"],
        json!(["intrinsics_never_written"])
    );

    let text = fixture.command(&[
        "explain-integrity",
        "--slot",
        "Object.prototype",
        "--limit",
        "1",
        "--format",
        "text",
    ]);
    assert!(
        text.status.success(),
        "{}",
        String::from_utf8_lossy(&text.stderr)
    );
    assert!(
        String::from_utf8_lossy(&text.stdout)
            .contains("possible discharges (audit candidates only): intrinsics_never_written"),
        "{}",
        String::from_utf8_lossy(&text.stdout)
    );
}

#[test]
fn precision_loss_limit_applies_per_unit_after_location_filtering() {
    let fixture = GraphFixture::new(
        "Object.freeze = value => value;\n\
         const values =\n\
         Object.freeze([\n\
         firstMissing(),\n\
         secondMissing(),\n\
         thirdMissing(),\n\
         ]);\n\
         console.log(values.length);\n",
        &[],
    );
    let all = fixture.json(&["explain-integrity", "--id", "values", "--all"]);
    let limited = fixture.json(&["explain-integrity", "--id", "values", "--limit", "1"]);
    let [all_unit] = all["source_units"].as_array().unwrap().as_slice() else {
        panic!("one values unit: {all}");
    };
    let limited_unit = limited["source_units"]
        .as_array()
        .unwrap()
        .iter()
        .find(|unit| unit["id"] == all_unit["id"])
        .unwrap_or_else(|| panic!("limited values unit: {limited}"));
    let all_losses = all_unit["precision_losses"].as_array().unwrap();
    assert!(all_losses.len() > 1, "{all_unit}");
    let loss_total = all_losses.len();
    assert!(
        !all_unit["failed_guards"].as_array().unwrap().is_empty(),
        "fixture keeps a failed guard in report: {all_unit}"
    );
    assert_eq!(
        all_unit["precision_loss_limit"],
        json!({"total": loss_total, "emitted": loss_total, "truncated": false})
    );
    let limited_losses = limited_unit["precision_losses"].as_array().unwrap();
    assert_eq!(
        limited_losses.len(),
        1,
        "--limit should bound this source unit's precision losses: {limited_unit}"
    );
    assert_eq!(limited_losses[0], all_losses[0]);
    assert_eq!(
        limited_unit["precision_loss_limit"],
        json!({"total": loss_total, "emitted": 1, "truncated": true})
    );
    for field in [
        "id",
        "source_location",
        "complete",
        "evaluated_guards",
        "failed_guards",
    ] {
        assert_eq!(limited_unit[field], all_unit[field], "{field} changed");
    }

    let at = fixture.json(&["explain-integrity", "--at", "main.js:3", "--limit", "1"]);
    let [at_unit] = at["source_units"].as_array().unwrap().as_slice() else {
        panic!("one source unit with a loss at line 3: {at}");
    };
    assert_eq!(at_unit["id"], all_unit["id"]);
    assert_eq!(at_unit["precision_losses"].as_array().unwrap().len(), 1);
    assert_eq!(
        at_unit["precision_losses"][0]["source_location"]["start_line"],
        3
    );
    assert_eq!(
        at_unit["precision_loss_limit"],
        json!({"total": 1, "emitted": 1, "truncated": false})
    );

    let text = fixture.command(&[
        "explain-integrity",
        "--id",
        "values",
        "--limit",
        "1",
        "--format",
        "text",
    ]);
    assert!(
        text.status.success(),
        "{}",
        String::from_utf8_lossy(&text.stderr)
    );
    assert!(
        String::from_utf8_lossy(&text.stdout).contains(&format!(
            "showing 1 of {loss_total} precision losses; use --all for the rest"
        )),
        "{}",
        String::from_utf8_lossy(&text.stdout)
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

#[test]
fn host_census_family_does_not_suggest_an_unavailable_author_family_premise() {
    let fixture = GraphFixture::new(
        "function forget() { delete globalThis.registry.TextEncoder; }\nconst encoder = new TextEncoder();\nexport { encoder, forget };\n",
        &[],
    );
    let report = fixture.json(&["explain-integrity", "--slot", "TextEncoder"]);
    let encoder = slot(&report, "%TextEncoder%");
    assert_eq!(encoder["intact"], false);
    assert_eq!(reaches(encoder), [reach("unknown_receiver", 1)]);
    assert_eq!(
        encoder["witnesses"][0]["possible_discharges"],
        json!([
            "predefined_globals_bundle_ownership",
            "no_accessor_install_callables"
        ])
    );
}
