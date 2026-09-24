//! Selector outcomes decided around the joint solve: a selector the shape
//! matcher places too widely is `too_broad_selector` without a solve, and one
//! that is unique only because other selectors claimed its alternatives
//! resolves with a `resolved_by_elimination` warning.

use std::fs;
use std::path::Path;

use debundle_e2e_support::{
    FixtureOpts, Member, logical_module, run_dry_run_fixture, run_dry_run_rejection_fixture,
    run_spec_validate, write_validate_fixture_spec,
};
use serde_json::Value;

/// 101 identical `const a_i = f();` declarations, one past the candidate cap,
/// beside one distinctive function.
fn too_broad_fixture_source() -> String {
    let mut source = (0..101)
        .map(|index| format!("const a_{index} = f();\n"))
        .collect::<String>();
    source.push_str(
        r#"function keepMe(value) {
  return value.trim();
}
console.log(keepMe(" ok "));
"#,
    );
    source
}

fn too_broad_fixture(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "broad/everything",
                &[Member::source_alpha("TooBroad", "const x = f();")],
            ),
            logical_module(
                "keep/me",
                &[Member::source_alpha(
                    "KeepMe",
                    "function k(value) {\n  return value.trim();\n}",
                )],
            ),
            // A name pin on `keepMe`: the duplicate claim it draws is the
            // witness that the other selector still resolved.
            logical_module("witness/keep", &[Member::renamed("KeepPin", "keepMe")]),
        ],
    )
}

#[test]
fn selector_over_candidate_cap_is_too_broad_and_others_still_resolve() {
    let source = too_broad_fixture_source();
    let rejected = run_dry_run_rejection_fixture(too_broad_fixture(&source));
    let diagnostics = read_diagnostics(&rejected.report_root);

    let broad = find_entry(&diagnostics, "too_broad_selector", "TooBroad");
    assert_eq!(broad["severity"], "error", "{broad:#}");
    assert!(
        broad["message"]
            .as_str()
            .unwrap()
            .contains("matches 101 places (limit 100); anchor it more specifically"),
        "{broad:#}"
    );
    let duplicate = diagnostics
        .iter()
        .find(|entry| entry["category"] == "duplicate_claim")
        .unwrap_or_else(|| panic!("missing duplicate-claim witness: {diagnostics:#?}"));
    assert_eq!(duplicate["duplicate_claim"]["binding"], "keepMe");
    assert_eq!(diagnostics.len(), 2, "{diagnostics:#?}");

    let validate = validate_json(too_broad_fixture(&source));
    assert_eq!(validate["counts"]["too_broad_selector"], 1, "{validate:#}");
}

/// `Either` matches both functions; `Other` matches only `second`. `Either`
/// resolves to `first` solely because `Other` claimed its alternative.
fn elimination_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"function first() {
  return "shared";
}
function second() {
  return "other";
}
function third(value) {
  return value.trim();
}
console.log(first(), second(), third(" ok "));
"#,
        vec![
            logical_module(
                "elimination/either",
                &[Member::source_alpha(
                    "Either",
                    "function f() {\n  return EXPR;\n}",
                )],
            ),
            logical_module(
                "elimination/other",
                &[Member::source_alpha(
                    "Other",
                    "function g() {\n  return \"other\";\n}",
                )],
            ),
            logical_module(
                "elimination/third",
                &[Member::source_alpha(
                    "Third",
                    "function t(value) {\n  return value.trim();\n}",
                )],
            ),
        ],
    )
}

#[test]
fn selector_unique_only_by_elimination_warns_and_run_succeeds() {
    let fixture = run_dry_run_fixture(elimination_fixture());
    let diagnostics = read_diagnostics(&fixture.report_root);

    let either = find_entry(&diagnostics, "resolved_by_elimination", "Either");
    assert_eq!(either["severity"], "warning", "{either:#}");
    let message = either["message"].as_str().unwrap();
    assert!(
        message.contains("matches 2 places, and the others are claimed by `Other` in"),
        "{either:#}"
    );
    // `Other` and `Third` are unique on their own candidates: no warning.
    assert_eq!(diagnostics.len(), 1, "{diagnostics:#?}");
    assert!(
        fixture.stderr.contains("resolved by elimination"),
        "{}",
        fixture.stderr
    );

    let validate = validate_json(elimination_fixture());
    assert_eq!(
        validate["counts"]["resolved_by_elimination"], 1,
        "{validate:#}"
    );
    let entry = &validate["chunks"][0]["diagnostics"][0];
    assert_eq!(entry["export_name"], "Either", "{validate:#}");
    assert_eq!(entry["severity"], "warning", "{validate:#}");
}

fn validate_json(opts: FixtureOpts<'_>) -> Value {
    let fixture = write_validate_fixture_spec(opts);
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );
    serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
}

fn read_diagnostics(report_root: &Path) -> Vec<Value> {
    let report_path = report_root
        .join("static")
        .join("app")
        .join("selector_diagnostics.json");
    let report: Value = serde_json::from_str(
        &fs::read_to_string(&report_path)
            .unwrap_or_else(|error| panic!("read {}: {error}", report_path.display())),
    )
    .unwrap();
    report["diagnostics"]
        .as_array()
        .expect("diagnostics must be an array")
        .clone()
}

fn find_entry<'a>(diagnostics: &'a [Value], category: &str, export_name: &str) -> &'a Value {
    diagnostics
        .iter()
        .find(|entry| entry["category"] == category && entry["export_name"] == export_name)
        .unwrap_or_else(|| {
            panic!("missing {category} entry for export {export_name}: {diagnostics:#?}")
        })
}
