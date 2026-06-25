use std::fs;

use debundle_e2e_support::{
    FixtureOpts, Member, logical_module, logical_module_with_anon,
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
    assert_eq!(missing["selector_kind"], "members.source_match");
    assert!(missing["target_binding"].is_null(), "{missing:#}");
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
            .contains("logged selector context"),
        "{missing:#}"
    );

    let ambiguous = find_entry(diagnostics, "ambiguous_selector", "AmbiguousHelper");
    assert_eq!(ambiguous["module_path"], "diagnostics/ambiguous");
    assert_eq!(ambiguous["body_indices"], serde_json::json!([1, 2]));
    assert!(
        ambiguous["message"].as_str().unwrap().contains("ambiguous"),
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

fn find_entry<'a>(diagnostics: &'a [Value], category: &str, export_name: &str) -> &'a Value {
    diagnostics
        .iter()
        .find(|entry| entry["category"] == category && entry["export_name"] == export_name)
        .unwrap_or_else(|| {
            panic!("missing {category} entry for export {export_name}: {diagnostics:#?}")
        })
}
