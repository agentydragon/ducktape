//! Pin the owner-graph `purity` field carries structured reasons
//! for non-Pure verdicts. Replaces the legacy `has_side_effect:
//! bool` with `purity: Purity { kind: "pure" | "not_pure", reasons }`.
//!
//! Diagnostic contract: every owner with `purity.kind != "pure"`
//! has at least one `PurityReason` describing which classifier
//! rule fired and where (`source_location` is populated). For
//! compound shapes (e.g. `new Set([f()])`) the offending inner
//! sub-expression's reason is concatenated alongside the outer
//! `unknown_new` umbrella reason in source order.

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use std::{fs, path::Path};

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

fn load_owner_graph(fixture: &Fixture) -> OwnerGraphReport {
    read_json(&fixture.report_root.join("static/app/owner_graph.json"))
}

#[test]
fn pure_const_decl_reports_pure_purity() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1;
console.log(a);
export { a };
"#,
        vec![logical_module("a_module", &[Member::new("a")])],
    ));
    assert_entry_output(&fixture, "1\n");
    let graph = load_owner_graph(&fixture);
    let owner = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "a"))
        .expect("owner declaring a should exist");
    let v = serde_json::to_value(&owner.purity).expect("serialize purity");
    assert_eq!(
        v["kind"], "pure",
        "Pure declarator should serialize as kind=pure: {v}"
    );
}

#[test]
fn unknown_call_init_reports_unknown_call_reason() {
    // `Number(x)` is a global-callable that may coerce via
    // user `[Symbol.toPrimitive]` / `valueOf` / `toString` —
    // not on the PURE_GLOBAL_CALLS whitelist. The classifier
    // returns NotPure with rule = unknown_call.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const x = "1";
const a = Number(x);
console.log(a);
export { a };
"#,
        vec![logical_module("a_module", &[Member::new("a")])],
    ));
    assert_entry_output(&fixture, "1\n");

    let graph = load_owner_graph(&fixture);
    let owner = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "a"))
        .expect("owner declaring a should exist");

    let v = serde_json::to_value(&owner.purity).expect("serialize purity");
    assert_eq!(
        v["kind"], "not_pure",
        "Number(x) call init should be NotPure: {v}"
    );
    let reasons = v["reasons"]
        .as_array()
        .expect("reasons array on NotPure verdict");
    assert!(
        !reasons.is_empty(),
        "NotPure must carry at least one reason: {v}"
    );
    let rules: Vec<&str> = reasons
        .iter()
        .map(|r| r["rule"].as_str().unwrap_or(""))
        .collect();
    assert!(
        rules.contains(&"unknown_call"),
        "expected an unknown_call reason; got rules {rules:?}"
    );
    // Source location must be populated by the per-chunk line resolver.
    let sl = &reasons[0]["source_location"];
    assert!(
        sl.is_object(),
        "first reason should expose a source_location: {v}"
    );
    assert_eq!(
        sl["source_path"], "static/app.js",
        "source_path resolves to the input-chunk path the classifier saw: {sl}"
    );
}

#[test]
fn nested_unknown_in_new_reports_both_outer_and_inner_reasons() {
    // `new Set([Number(x)])` — the array-iterable rule recognizes
    // the shape but the element classifies as NotPure (unknown_call).
    // The classifier emits both the inner reason (unknown_call on
    // Number) and the outer umbrella (unknown_new on the Set
    // constructor) in source order.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const x = "1";
const a = new Set([Number(x)]);
console.log(a.size);
export { a };
"#,
        vec![logical_module("a_module", &[Member::new("a")])],
    ));
    assert_entry_output(&fixture, "1\n");

    let graph = load_owner_graph(&fixture);
    let owner = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "a"))
        .expect("owner declaring a should exist");
    let v = serde_json::to_value(&owner.purity).expect("serialize purity");
    assert_eq!(v["kind"], "not_pure", "should be NotPure: {v}");
    let rules: Vec<&str> = v["reasons"]
        .as_array()
        .expect("reasons array")
        .iter()
        .map(|r| r["rule"].as_str().unwrap_or(""))
        .collect();
    assert!(
        rules.contains(&"unknown_new"),
        "outer umbrella unknown_new should be present: {rules:?}"
    );
    assert!(
        rules.contains(&"unknown_call"),
        "inner unknown_call should be present: {rules:?}"
    );
}

#[test]
fn delete_operator_reports_delete_rule() {
    // `delete o.x` is unambiguously side-effecting — rule
    // delete_operator. Wrapping in a sequence makes the value
    // computable at init time without changing the verdict.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const o = { x: 1 };
const a = (delete o.x, 0);
console.log(a);
export { a };
"#,
        vec![logical_module("a_module", &[Member::new("a")])],
    ));
    assert_entry_output(&fixture, "0\n");

    let graph = load_owner_graph(&fixture);
    let owner = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "a"))
        .expect("owner declaring a should exist");
    let v = serde_json::to_value(&owner.purity).expect("serialize purity");
    let rules: Vec<&str> = v["reasons"]
        .as_array()
        .expect("reasons array")
        .iter()
        .map(|r| r["rule"].as_str().unwrap_or(""))
        .collect();
    assert!(
        rules.contains(&"delete_operator"),
        "expected delete_operator reason; got rules {rules:?}"
    );
}

#[test]
fn multiple_offenders_concatenate_reasons_in_source_order() {
    // `f() + g()` with both calls unknown — both reasons are
    // emitted on the owner's purity, not just the first.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const x = 1;
const a = Number(x) + Number(x);
console.log(a);
export { a };
"#,
        vec![logical_module("a_module", &[Member::new("a")])],
    ));
    assert_entry_output(&fixture, "2\n");

    let graph = load_owner_graph(&fixture);
    let owner = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "a"))
        .expect("owner declaring a should exist");
    let v = serde_json::to_value(&owner.purity).expect("serialize purity");
    let unknown_call_count = v["reasons"]
        .as_array()
        .expect("reasons array")
        .iter()
        .filter(|r| r["rule"].as_str() == Some("unknown_call"))
        .count();
    assert!(
        unknown_call_count >= 2,
        "expected ≥2 unknown_call reasons (one per call), got {unknown_call_count} in {v}"
    );
}
