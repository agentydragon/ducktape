//! End-to-end exercise of `debundle spec validate --keep-going` by shelling
//! out to the built binary. The keep-going classification itself is pinned by
//! `selector_diagnostics_report_test`; this test pins the CLI verb: that one
//! pass surfaces every failure class on stdout in each `--format`.

use debundle_e2e_support::{
    FixtureOpts, Member, logical_module, run_spec_validate, write_validate_fixture_spec,
};
use serde_json::Value;

/// One fixture exercising every covered failure class at once:
/// - `unresolved_selector`: a `source_match` whose body matches nothing;
/// - `ambiguous_selector`: a `source_match` matching two identical helpers;
/// - `duplicate_claim`: two members resolving to the same declaration.
fn mixed_failure_fixture() -> FixtureOpts<'static> {
    let missing_selector = r#"function selectedFormatter(value) {
  return value.toLowerCase();
}"#;
    let ambiguous_selector = r#"function repeatedHelper() {
  return "shared";
}"#;
    FixtureOpts::new(
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
        ],
    )
}

#[test]
fn validate_json_reports_every_failure_class_in_one_pass() {
    let fixture = write_validate_fixture_spec(mixed_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );

    let report: Value = serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout));

    assert_eq!(report["total"], 3, "{report:#}");
    assert_eq!(report["counts"]["unresolved_selector"], 1, "{report:#}");
    assert_eq!(report["counts"]["ambiguous_selector"], 1, "{report:#}");
    assert_eq!(report["counts"]["duplicate_claim"], 1, "{report:#}");

    let chunks = report["chunks"].as_array().expect("chunks array");
    let chunk = chunks
        .iter()
        .find(|chunk| chunk["chunk_id"] == "static/app")
        .expect("static/app chunk report");
    let diagnostics = chunk["diagnostics"].as_array().expect("diagnostics array");
    assert_eq!(diagnostics.len(), 3, "{chunk:#}");

    let missing = find_entry(diagnostics, "unresolved_selector", "MissingFormatter");
    assert_eq!(missing["module_path"], "diagnostics/missing");
    assert_eq!(missing["selector_kind"], "members.source_match");
    assert!(
        missing["recommended_next_action"]
            .as_str()
            .unwrap()
            .contains("logged selector context"),
        "{missing:#}"
    );

    let ambiguous = find_entry(diagnostics, "ambiguous_selector", "AmbiguousHelper");
    assert_eq!(ambiguous["body_indices"], serde_json::json!([1, 2]));

    let duplicate = diagnostics
        .iter()
        .find(|entry| entry["category"] == "duplicate_claim")
        .expect("duplicate claim entry");
    assert_eq!(duplicate["duplicate_claim"]["binding"], "renderCard");
}

#[test]
fn validate_ndjson_streams_one_object_per_diagnostic_plus_summary() {
    let fixture = write_validate_fixture_spec(mixed_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "ndjson"]);
    assert!(out.status.success(), "stderr={}", out.stderr);

    let lines: Vec<&str> = out.stdout.trim_end().split('\n').collect();
    // 3 diagnostics + 1 summary line.
    assert_eq!(lines.len(), 4, "stdout:\n{}", out.stdout);

    let parsed: Vec<Value> = lines
        .iter()
        .map(|line| serde_json::from_str(line).expect("each ndjson line is valid json"))
        .collect();
    let summary = parsed.last().unwrap();
    assert_eq!(summary["section"], "summary");
    assert_eq!(summary["total"], 3);

    for line in &parsed[..3] {
        assert_eq!(line["section"], "diagnostic");
        assert_eq!(line["chunk_id"], "static/app");
        assert!(line["category"].is_string(), "{line:#}");
    }
}

#[test]
fn validate_text_summarizes_counts_and_per_chunk_findings() {
    let fixture = write_validate_fixture_spec(mixed_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "text"]);
    assert!(out.status.success(), "stderr={}", out.stderr);

    let stdout = out.stdout;
    assert!(
        stdout.contains("3 selector problem(s)"),
        "missing total line:\n{stdout}"
    );
    assert!(
        stdout.contains("chunk static/app:"),
        "missing chunk header:\n{stdout}"
    );
    assert!(
        stdout.contains("[unresolved_selector]") && stdout.contains("[ambiguous_selector]"),
        "missing classified findings:\n{stdout}"
    );
}

#[test]
fn validate_clean_spec_reports_no_problems() {
    let opts = FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
console.log(renderCard(" ok "));
export { renderCard };
"#,
        vec![logical_module("owners/card", &[Member::new("renderCard")])],
    );
    let fixture = write_validate_fixture_spec(opts);

    let json = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(json.status.success(), "stderr={}", json.stderr);
    let report: Value = serde_json::from_str(&json.stdout).unwrap();
    assert_eq!(report["total"], 0, "{report:#}");
    assert!(
        report["chunks"].as_array().unwrap().is_empty(),
        "{report:#}"
    );

    let text = run_spec_validate(&fixture.spec_path, &["--format", "text"]);
    assert!(
        text.stdout.contains("No selector problems found"),
        "{}",
        text.stdout
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
