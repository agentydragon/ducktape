//! End-to-end exercise of `debundle spec validate --keep-going` by shelling
//! out to the built binary. The keep-going classification itself is pinned by
//! `selector_diagnostics_report_test`; this test pins the CLI verb: that one
//! pass surfaces the selector diagnostics on stdout in each `--format`.

use std::fs;
use std::path::Path;
use std::process::Command;

use debundle_e2e_support::{
    CommandResult, FixtureOpts, Member, debundler_path, logical_module, run_source_only_validate,
    run_spec_validate, write_validate_fixture_spec,
};
use serde_json::Value;

/// One fixture exercising every covered failure class at once:
/// - `selector_resolution_error`: `source_match` selectors that do not produce
///   a valid native global selector assignment;
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
    assert_eq!(missing["selector_kind"], "source_matches");
    assert!(
        missing["recommended_next_action"]
            .as_str()
            .unwrap()
            .contains("Update the selector source"),
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
        stdout.contains("[unresolved_selector]")
            && stdout.contains("[ambiguous_selector]")
            && stdout.contains("[duplicate_claim]"),
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

#[test]
fn validate_spec_ignores_source_only_environment_defaults() {
    let opts = FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
export { renderCard };
"#,
        vec![logical_module("owners/card", &[Member::new("renderCard")])],
    );
    let fixture = write_validate_fixture_spec(opts);
    let bin = debundler_path();
    let output = Command::new(&bin)
        .arg("spec")
        .arg("validate")
        .arg("--spec")
        .arg(&fixture.spec_path)
        .arg("--format")
        .arg("json")
        .env("DEBUNDLE_MODULES", "/definitely/not/source/modules")
        .env("DEBUNDLE_SOURCE_ROOT", "/definitely/not/source/root")
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    let out = CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    };

    assert!(
        out.status.success(),
        "spec validate should ignore source-only env defaults\nstdout:\n{}\nstderr:\n{}",
        out.stdout,
        out.stderr,
    );
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 0, "{report:#}");
}

#[test]
fn validate_source_only_json_reports_every_source_selector_failure_without_ortools() {
    let fixture = write_source_only_validate_fixture();
    let out = run_source_only_validate(
        &fixture.modules_root,
        &fixture.source_file,
        &["--format", "json"],
    );
    assert!(
        out.status.success(),
        "source-only validate exited non-zero\nstdout:\n{}\nstderr:\n{}",
        out.stdout,
        out.stderr,
    );

    let report: Value = serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout));
    assert_eq!(report["total"], 2, "{report:#}");
    assert_eq!(report["counts"]["unresolved_selector"], 1, "{report:#}");
    assert_eq!(report["counts"]["ambiguous_selector"], 1, "{report:#}");

    let chunk = report["chunks"]
        .as_array()
        .expect("chunks array")
        .first()
        .expect("one source chunk report");
    let diagnostics = chunk["diagnostics"].as_array().expect("diagnostics array");
    let missing = find_entry(diagnostics, "unresolved_selector", "MissingWidget");
    assert_eq!(missing["module_path"], "ui/missing");
    assert_eq!(missing["selector_kind"], "source_matches");
    assert_eq!(missing["body_indices"], serde_json::json!([]));
    assert!(
        missing["claim_origin"]
            .as_str()
            .unwrap()
            .contains("missing.yaml#source_matches[0]"),
        "{missing:#}",
    );

    let ambiguous = find_entry(diagnostics, "ambiguous_selector", "AmbiguousPanel");
    assert_eq!(ambiguous["module_path"], "ui/ambiguous");
    assert_eq!(ambiguous["body_indices"], serde_json::json!([0, 1]));
    assert!(ambiguous["source_match_hash"].is_string(), "{ambiguous:#}");
    assert!(
        chunk["coverage_notes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|note| note
                .as_str()
                .unwrap()
                .contains("source-only validation covers")),
        "{chunk:#}",
    );
}

#[test]
fn validate_source_only_ndjson_is_one_line_per_queue_item_plus_summary() {
    let fixture = write_source_only_validate_fixture();
    let out = run_source_only_validate(
        &fixture.modules_root,
        &fixture.source_file,
        &["--format", "ndjson"],
    );
    assert!(out.status.success(), "stderr={}", out.stderr);

    let lines: Vec<&str> = out.stdout.trim_end().split('\n').collect();
    assert_eq!(lines.len(), 3, "stdout:\n{}", out.stdout);
    let parsed: Vec<Value> = lines
        .iter()
        .map(|line| serde_json::from_str(line).expect("ndjson line is valid json"))
        .collect();
    for line in &parsed[..2] {
        assert_eq!(line["section"], "diagnostic");
        assert!(line["module_path"].is_string(), "{line:#}");
        assert!(line["export_name"].is_string(), "{line:#}");
        assert!(line["claim_origin"].is_string(), "{line:#}");
    }
    assert_eq!(parsed[2]["section"], "summary");
    assert_eq!(parsed[2]["total"], 2);
}

#[test]
fn validate_source_only_clean_modules_report_no_problems() {
    let dir = tempfile::tempdir().unwrap();
    let source_file = dir.path().join("chunk.js");
    write(
        &source_file,
        r#"const widget = makeWidget("ok");
"#,
    );
    let modules_root = dir.path().join("modules");
    write(
        &modules_root.join("ui/widget.yaml"),
        r#"source_matches:
  - match: 'const w = makeWidget("ok");'
    bindings:
      - local: w
        name: Widget
"#,
    );

    let out = run_source_only_validate(&modules_root, &source_file, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 0, "{report:#}");
    assert!(
        report["chunks"].as_array().unwrap().is_empty(),
        "{report:#}"
    );
}

#[test]
fn validate_source_only_reports_stale_annotations() {
    let dir = tempfile::tempdir().unwrap();
    let source_file = dir.path().join("chunk.js");
    write(
        &source_file,
        r#"const claimed = makeWidget("ok");
"#,
    );
    let modules_root = dir.path().join("modules");
    write(
        &modules_root.join("ui/widget.yaml"),
        r#"source_matches:
  - match: 'const selected = makeWidget("ok");'
    bindings:
      - local: selected
        name: Widget
annotations:
  StaleWidget:
    note: no matching claim
"#,
    );

    let out = run_source_only_validate(&modules_root, &source_file, &["--format", "json"]);
    assert!(out.status.success(), "stderr={}", out.stderr);
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 1, "{report:#}");
    let diagnostic = &report["chunks"][0]["diagnostics"][0];
    assert_eq!(diagnostic["category"], "selector_resolution_error");
    assert_eq!(diagnostic["selector_kind"], "annotations");
    assert_eq!(diagnostic["module_path"], "ui/widget");
    assert!(
        diagnostic["message"]
            .as_str()
            .unwrap()
            .contains("annotations key `StaleWidget` does not match"),
        "{diagnostic:#}"
    );
}

#[test]
fn validate_source_only_reports_anonymous_statement_failures() {
    let dir = tempfile::tempdir().unwrap();
    let source_file = dir.path().join("chunk.js");
    write(
        &source_file,
        r#"sideEffect("shared");
sideEffect("shared");
"#,
    );
    let modules_root = dir.path().join("modules");
    write(
        &modules_root.join("effects/ambiguous.yaml"),
        r#"anonymous_statements:
  - source_match:
      match: 'sideEffect("shared");'
"#,
    );
    write(
        &modules_root.join("effects/missing.yaml"),
        r#"anonymous_statements:
  - source_match:
      match: 'sideEffect("missing");'
"#,
    );

    let out = run_source_only_validate(&modules_root, &source_file, &["--format", "json"]);
    assert!(
        out.status.success(),
        "source-only validate exited non-zero\nstdout:\n{}\nstderr:\n{}",
        out.stdout,
        out.stderr,
    );
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 2, "{report:#}");
    assert_eq!(report["counts"]["unresolved_selector"], 1, "{report:#}");
    assert_eq!(report["counts"]["ambiguous_selector"], 1, "{report:#}");

    let diagnostics = report["chunks"][0]["diagnostics"]
        .as_array()
        .expect("diagnostics array");
    let missing = diagnostics
        .iter()
        .find(|entry| {
            entry["category"] == "unresolved_selector" && entry["module_path"] == "effects/missing"
        })
        .expect("missing anonymous diagnostic");
    assert_eq!(
        missing["selector_kind"],
        "anonymous_statements.source_match"
    );
    assert_eq!(missing["export_name"], serde_json::Value::Null);

    let ambiguous = diagnostics
        .iter()
        .find(|entry| {
            entry["category"] == "ambiguous_selector" && entry["module_path"] == "effects/ambiguous"
        })
        .expect("ambiguous anonymous diagnostic");
    assert_eq!(ambiguous["body_indices"], serde_json::json!([0, 1]));
}

#[test]
fn validate_source_only_reports_multi_statement_anonymous_selector_as_resolution_error() {
    let dir = tempfile::tempdir().unwrap();
    let source_file = dir.path().join("chunk.js");
    write(
        &source_file,
        r#"setup();
start();
"#,
    );
    let modules_root = dir.path().join("modules");
    write(
        &modules_root.join("effects/startup.yaml"),
        r#"anonymous_statements:
  - source_match:
      match: |
        setup();
        start();
"#,
    );

    let out = run_source_only_validate(&modules_root, &source_file, &["--format", "json"]);
    assert!(
        out.status.success(),
        "source-only validate exited non-zero\nstdout:\n{}\nstderr:\n{}",
        out.stdout,
        out.stderr,
    );
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 1, "{report:#}");
    let entry = &report["chunks"][0]["diagnostics"][0];
    assert_eq!(entry["category"], "selector_resolution_error", "{entry:#}");
    assert_eq!(entry["module_path"], "effects/startup");
    assert_eq!(entry["selector_kind"], "anonymous_statements.source_match");
    assert!(
        entry["claim_origin"]
            .as_str()
            .unwrap()
            .contains("startup.yaml#anonymous_statements[0]"),
        "{entry:#}",
    );
}

#[test]
fn validate_source_only_reports_source_match_failures_per_export() {
    let dir = tempfile::tempdir().unwrap();
    let source_file = dir.path().join("chunk.js");
    write(
        &source_file,
        r#"const leftOne = renderPanel("shared"), rightOne = renderPanel("shared");
const leftTwo = renderPanel("shared"), rightTwo = renderPanel("shared");
"#,
    );
    let modules_root = dir.path().join("modules");
    write(
        &modules_root.join("ui/panels.yaml"),
        r#"source_matches:
  - match: 'const left = renderPanel("shared"), right = renderPanel("shared");'
    bindings:
      - local: left
        name: LeftPanel
      - local: right
        name: RightPanel
"#,
    );

    let out = run_source_only_validate(&modules_root, &source_file, &["--format", "json"]);
    assert!(
        out.status.success(),
        "source-only validate exited non-zero\nstdout:\n{}\nstderr:\n{}",
        out.stdout,
        out.stderr,
    );
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(report["total"], 2, "{report:#}");
    assert_eq!(report["counts"]["ambiguous_selector"], 2, "{report:#}");

    let diagnostics = report["chunks"][0]["diagnostics"]
        .as_array()
        .expect("diagnostics array");
    let left = find_entry(diagnostics, "ambiguous_selector", "LeftPanel");
    assert_eq!(left["selector_kind"], "source_matches");
    assert_eq!(left["target_binding"], "left");
    assert_eq!(left["body_indices"], serde_json::json!([0, 1]));

    let right = find_entry(diagnostics, "ambiguous_selector", "RightPanel");
    assert_eq!(right["selector_kind"], "source_matches");
    assert_eq!(right["target_binding"], "right");
    assert_eq!(right["body_indices"], serde_json::json!([0, 1]));
}

struct SourceOnlyValidateFixture {
    _root: tempfile::TempDir,
    modules_root: std::path::PathBuf,
    source_file: std::path::PathBuf,
}

fn write_source_only_validate_fixture() -> SourceOnlyValidateFixture {
    let root = tempfile::tempdir().unwrap();
    let source_file = root.path().join("chunk.js");
    write(
        &source_file,
        r#"const leftPanel = renderPanel("shared");
const rightPanel = renderPanel("shared");
const widget = makeWidget("ok");
"#,
    );
    let modules_root = root.path().join("modules");
    write(
        &modules_root.join("ui/ok.yaml"),
        r#"source_matches:
  - match: 'const w = makeWidget("ok");'
    bindings:
      - local: w
        name: Widget
"#,
    );
    write(
        &modules_root.join("ui/missing.yaml"),
        r#"source_matches:
  - match: 'const w = makeWidget("missing");'
    bindings:
      - local: w
        name: MissingWidget
"#,
    );
    write(
        &modules_root.join("ui/ambiguous.yaml"),
        r#"source_matches:
  - match: 'const panel = renderPanel("shared");'
    bindings:
      - local: panel
        name: AmbiguousPanel
"#,
    );
    SourceOnlyValidateFixture {
        _root: root,
        modules_root,
        source_file,
    }
}

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn find_entry<'a>(diagnostics: &'a [Value], category: &str, export_name: &str) -> &'a Value {
    diagnostics
        .iter()
        .find(|entry| entry["category"] == category && entry["export_name"] == export_name)
        .unwrap_or_else(|| {
            panic!("missing {category} entry for export {export_name}: {diagnostics:#?}")
        })
}
