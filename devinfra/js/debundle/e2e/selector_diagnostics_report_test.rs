use std::fs;

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, Member, logical_module, logical_module_with_anon,
    logical_module_with_anon_alpha_many, logical_module_with_binding_groups,
    run_keep_going_dry_run_rejection_fixture,
};
use serde_json::Value;

#[test]
fn keep_going_writes_machine_readable_selector_diagnostics_report() {
    let missing_selector = r#"function selectedFormatter(value) {
  return value.toLowerCase();
}"#;
    let ambiguous_selector = r#"function repeatedHelper() {
  return "shared";
}"#;
    let opts = FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
function decoratePrimary() {
  return "shared";
}
function decorateSecondary() {
  return "shared";
}
console.log(renderCard(" ok "), decoratePrimary(), decorateSecondary());
export { renderCard, decoratePrimary, decorateSecondary };
"#,
        vec![
            logical_module(
                "diagnostics/missing",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module("owners/card", &[Member::new("renderCard")]),
            logical_module(
                "duplicates/card",
                &[Member::renamed("renderCardAgain", "renderCard")],
            ),
            logical_module(
                "diagnostics/ambiguous",
                &[Member::source_alpha("AmbiguousHelper", ambiguous_selector)],
            ),
            logical_module_with_anon("diagnostics/anon", &[], &["console.warn(\"absent\");"]),
        ],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    assert!(
        rejected
            .stderr
            .contains("Source-match selector diagnostic report: 2 unresolved selector(s) found"),
        "human source-match diagnostics must remain intact:\n{}",
        rejected.stderr
    );
    assert!(
        rejected
            .stderr
            .contains("Duplicate binding claim report: 1 duplicate claim(s) found"),
        "human duplicate diagnostics must remain intact:\n{}",
        rejected.stderr
    );
    assert!(
        rejected
            .stderr
            .contains("Anonymous statement selector diagnostic report"),
        "human anonymous diagnostics must appear:\n{}",
        rejected.stderr
    );

    let report_path = rejected
        .report_root
        .join("static")
        .join("app")
        .join("selector_diagnostics.json");
    let report: Value = serde_json::from_str(
        &fs::read_to_string(&report_path)
            .unwrap_or_else(|error| panic!("read {}: {error}", report_path.display())),
    )
    .unwrap();
    assert_eq!(report["chunk_id"], "static/app");
    assert_eq!(report["counts"]["unresolved_selector"], 2);
    assert_eq!(report["counts"]["ambiguous_selector"], 1);
    assert_eq!(report["counts"]["duplicate_claim"], 1);

    let diagnostics = report["diagnostics"]
        .as_array()
        .expect("diagnostics must be an array");
    assert_eq!(diagnostics.len(), 4, "{report:#}");

    let missing = find_entry(diagnostics, "unresolved_selector", "MissingFormatter");
    assert_eq!(missing["module_path"], "diagnostics/missing");
    assert_eq!(missing["selector_kind"], "source_matches");
    assert_eq!(missing["target_binding"], "selectedFormatter");
    assert!(
        missing["source_match_preview"]
            .as_str()
            .unwrap()
            .contains("selectedFormatter"),
        "{missing:#}"
    );
    assert!(
        missing["source_match_hash"].as_str().unwrap().len() >= 16,
        "{missing:#}"
    );
    assert!(
        missing["first_mismatch"]
            .as_str()
            .is_some_and(|s| !s.is_empty()),
        "{missing:#}"
    );
    assert!(
        missing["recommended_next_action"]
            .as_str()
            .unwrap()
            .contains("Update the selector source"),
        "{missing:#}"
    );

    let ambiguous = find_entry(diagnostics, "ambiguous_selector", "AmbiguousHelper");
    assert_eq!(ambiguous["module_path"], "diagnostics/ambiguous");
    assert_eq!(ambiguous["body_indices"], serde_json::json!([1, 2]));
    assert!(
        ambiguous["message"]
            .as_str()
            .unwrap()
            .contains("is ambiguous"),
        "{ambiguous:#}"
    );
    assert!(
        ambiguous["recommended_next_action"]
            .as_str()
            .unwrap()
            .contains("Refine the selector"),
        "{ambiguous:#}"
    );

    let duplicate = diagnostics
        .iter()
        .find(|entry| entry["category"] == "duplicate_claim")
        .expect("duplicate claim entry");
    assert_eq!(duplicate["duplicate_claim"]["binding"], "renderCard");
    let duplicate_sites = [
        duplicate["duplicate_claim"]["existing"]["module_id"]
            .as_str()
            .unwrap(),
        duplicate["duplicate_claim"]["duplicate"]["module_id"]
            .as_str()
            .unwrap(),
    ];
    assert!(duplicate_sites.contains(&"static/app::owners/card"));
    assert!(duplicate_sites.contains(&"static/app::duplicates/card"));

    let anon = diagnostics
        .iter()
        .find(|entry| entry["selector_kind"] == "anonymous_statements.source_match")
        .expect("anonymous statement diagnostic entry");
    assert_eq!(anon["category"], "unresolved_selector");
    assert_eq!(anon["module_path"], "diagnostics/anon");
    assert!(anon["export_name"].is_null(), "{anon:#}");
    assert!(
        anon["source_match_preview"]
            .as_str()
            .unwrap()
            .contains("console.warn"),
        "{anon:#}"
    );
}

/// A selector the matcher places nowhere is reported on its own and never
/// enters the solve, so it cannot take the chunk's other selectors down with it.
#[test]
fn keep_going_unmatched_selector_does_not_cascade() {
    let missing_selector = r#"function missingFormatter(value) {
  return value.toLowerCase();
}"#;
    let valid_selector = r#"function keepMe(value) {
  return value.trim();
}"#;
    let opts = FixtureOpts::new(
        r#"function keepMe(value) {
  return value.trim();
}
console.log(keepMe(" ok "));
export { keepMe };
"#,
        vec![
            logical_module(
                "diagnostics/root",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module(
                "diagnostics/cascade",
                &[Member::source_alpha("KeepMe", valid_selector)],
            ),
        ],
    );

    let diagnostics = keep_going_diagnostics(opts);
    find_entry(&diagnostics, "unresolved_selector", "MissingFormatter");
    assert_eq!(diagnostics.len(), 1, "{diagnostics:#?}");
}

/// Two pairs of selectors each compete for the one declaration both members
/// of the pair match, under the injectivity `all_different`. Each pair is its
/// own contradiction: its selectors name each other, and neither pair takes
/// down the other or the independent selector.
#[test]
fn keep_going_localizes_each_selector_conflict() {
    let opts = FixtureOpts::new(
        r#"function alphaOne() {
  return "alpha";
}
function betaOne() {
  return "beta";
}
function gammaOne(value) {
  return value.trim();
}
console.log(alphaOne(), betaOne(), gammaOne(" ok "));
export { alphaOne, betaOne, gammaOne };
"#,
        vec![
            logical_module(
                "conflicts/alpha_left",
                &[Member::source_alpha(
                    "AlphaLeft",
                    "function left() {\n  return \"alpha\";\n}",
                )],
            ),
            logical_module(
                "conflicts/alpha_right",
                &[Member::source_alpha(
                    "AlphaRight",
                    "function right() {\n  return \"alpha\";\n}",
                )],
            ),
            logical_module(
                "conflicts/beta_left",
                &[Member::source_alpha(
                    "BetaLeft",
                    "function left() {\n  return \"beta\";\n}",
                )],
            ),
            logical_module(
                "conflicts/beta_right",
                &[Member::source_alpha(
                    "BetaRight",
                    "function right() {\n  return \"beta\";\n}",
                )],
            ),
            logical_module(
                "independent/gamma",
                &[Member::source_alpha(
                    "Gamma",
                    "function g(value) {\n  return value.trim();\n}",
                )],
            ),
            // A name pin on `gammaOne`: the duplicate claim it draws is the
            // witness that the independent selector resolved.
            logical_module("witness/gamma", &[Member::renamed("GammaPin", "gammaOne")]),
        ],
    );

    let diagnostics = keep_going_diagnostics(opts);
    for (export_name, partner) in [
        ("AlphaLeft", "AlphaRight"),
        ("AlphaRight", "AlphaLeft"),
        ("BetaLeft", "BetaRight"),
        ("BetaRight", "BetaLeft"),
    ] {
        let entry = find_entry(&diagnostics, "conflicting_selector", export_name);
        let message = entry["message"].as_str().unwrap();
        assert!(
            message.contains(&format!("conflicts with `{partner}`")),
            "{entry:#}"
        );
        let other_pair = if export_name.starts_with("Alpha") {
            "Beta"
        } else {
            "Alpha"
        };
        assert!(!message.contains(other_pair), "{entry:#}");
    }
    let duplicate = diagnostics
        .iter()
        .find(|entry| entry["category"] == "duplicate_claim")
        .unwrap_or_else(|| panic!("missing duplicate-claim witness: {diagnostics:#?}"));
    assert_eq!(duplicate["duplicate_claim"]["binding"], "gammaOne");
    assert_eq!(diagnostics.len(), 5, "{diagnostics:#?}");
}

#[test]
fn keep_going_unmatched_anonymous_statement_does_not_cascade() {
    let opts = FixtureOpts::new(
        r#"console.log("present");
"#,
        vec![logical_module_with_anon_alpha_many(
            "diagnostics/anon",
            &[],
            &[r#"console.warn("missing");"#, r#"console.log("present");"#],
        )],
    );

    let diagnostics = keep_going_diagnostics(opts);
    let missing = find_anon_entry(&diagnostics, "console.warn");
    assert_eq!(missing["category"], "unresolved_selector", "{missing:#}");
    assert_eq!(diagnostics.len(), 1, "{diagnostics:#?}");
}

#[test]
fn keep_going_reports_unmatched_source_match_group_per_target_binding() {
    let opts = FixtureOpts::new(
        r#"const presentLeft = 1, presentRight = 2;
console.log(presentLeft, presentRight);
export { presentLeft, presentRight };
"#,
        vec![logical_module_with_binding_groups(
            "diagnostics/group",
            &[],
            &[BindingGroup::source_alpha(
                r#"const missingLeft = "missing-left", missingRight = "missing-right";"#,
                &[
                    ("missingLeft", "ExportedLeft"),
                    ("missingRight", "ExportedRight"),
                ],
            )],
        )],
    );

    let diagnostics = keep_going_diagnostics(opts);
    for (export_name, target_binding) in [
        ("ExportedLeft", "missingLeft"),
        ("ExportedRight", "missingRight"),
    ] {
        let entry = find_entry(&diagnostics, "unresolved_selector", export_name);
        assert_eq!(entry["selector_kind"], "source_matches");
        assert_eq!(entry["target_binding"], target_binding);
    }
}

#[test]
fn keep_going_reports_canonical_source_match_group_failure() {
    let opts = FixtureOpts::new(
        r#"const present = 1;
console.log(present);
export { present };
"#,
        vec![logical_module_with_binding_groups(
            "diagnostics/unsupported-group",
            &[],
            &[BindingGroup::source_alpha(
                r#"const left = makeLeft();
STMT_LIST;
const right = makeRight();"#,
                &[("left", "ExportedLeft"), ("right", "ExportedRight")],
            )],
        )],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    assert!(
        rejected
            .stderr
            .contains("source_matches[].bindings[`left`]"),
        "human diagnostics should report the canonical source match:\n{}",
        rejected.stderr
    );
    let diagnostics = read_diagnostics(&rejected.report_root);
    let left = find_entry(&diagnostics, "unresolved_selector", "ExportedLeft");
    assert_eq!(left["selector_kind"], "source_matches");
    assert_eq!(left["target_binding"], "left");
    assert!(
        left["message"]
            .as_str()
            .unwrap()
            .contains("did not match any top-level declaration group"),
        "{left:#}"
    );
    let right = find_entry(&diagnostics, "unresolved_selector", "ExportedRight");
    assert_eq!(right["selector_kind"], "source_matches");
    assert_eq!(right["target_binding"], "right");
}

fn keep_going_diagnostics(opts: FixtureOpts<'_>) -> Vec<Value> {
    read_diagnostics(&run_keep_going_dry_run_rejection_fixture(opts).report_root)
}

fn read_diagnostics(report_root: &std::path::Path) -> Vec<Value> {
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

fn find_anon_entry<'a>(diagnostics: &'a [Value], preview_needle: &str) -> &'a Value {
    diagnostics
        .iter()
        .find(|entry| {
            entry["selector_kind"] == "anonymous_statements.source_match"
                && entry["source_match_preview"]
                    .as_str()
                    .is_some_and(|preview| preview.contains(preview_needle))
        })
        .unwrap_or_else(|| {
            panic!("missing anonymous entry containing {preview_needle}: {diagnostics:#?}")
        })
}
