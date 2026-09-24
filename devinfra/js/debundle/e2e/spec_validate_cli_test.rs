//! End-to-end exercise of `debundle spec validate` by shelling
//! out to the built binary. The keep-going classification itself is pinned by
//! `selector_diagnostics_report_test`; this test pins the CLI verb: that one
//! pass surfaces the selector outcomes on stdout in each `--format`.

use std::fs;
use std::path::Path;
use std::process::Command;

use debundle_e2e_support::{
    CommandResult, FixtureOpts, Member, debundler_path, find_outcome, logical_module,
    run_source_only_validate, run_spec_validate, write_validate_fixture_spec,
};
use serde_json::{Value, json};

/// One fixture exercising a no-match, an ambiguous selector and a duplicate
/// claim (two members resolving to the same declaration) at once.
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
    assert_eq!(
        report["counts"],
        json!({"no_match": 1, "ambiguous": 1, "duplicate_claim": 1}),
        "{report:#}"
    );
    let outcomes = outcomes(&report);
    assert_eq!(outcomes.len(), 3, "{report:#}");

    let missing = find_outcome(outcomes, "no_match", "MissingFormatter");
    assert_eq!(missing["chunk"], "static/app");
    assert_eq!(
        missing["placement"]["logical_module"],
        "diagnostics/missing"
    );
    assert_eq!(missing["placement"]["selector_kind"], "source_matches");

    let ambiguous = find_outcome(outcomes, "ambiguous", "AmbiguousHelper");
    assert_eq!(candidate_owners(ambiguous), [1, 2], "{ambiguous:#}");

    let duplicate = outcomes
        .iter()
        .find(|record| record["outcome"]["kind"] == "duplicate_claim")
        .expect("duplicate claim outcome");
    assert_eq!(duplicate["outcome"]["binding"], "renderCard");
}

#[test]
fn validate_ndjson_streams_one_object_per_outcome_plus_summary() {
    let fixture = write_validate_fixture_spec(mixed_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "ndjson"]);
    assert!(out.status.success(), "stderr={}", out.stderr);

    let lines: Vec<&str> = out.stdout.trim_end().split('\n').collect();
    // 3 outcomes + 1 summary line.
    assert_eq!(lines.len(), 4, "stdout:\n{}", out.stdout);

    let parsed: Vec<Value> = lines
        .iter()
        .map(|line| serde_json::from_str(line).expect("each ndjson line is valid json"))
        .collect();
    let summary = parsed.last().unwrap();
    assert_eq!(summary["section"], "summary");
    assert_eq!(
        summary["counts"],
        json!({"no_match": 1, "ambiguous": 1, "duplicate_claim": 1})
    );

    for line in &parsed[..3] {
        assert_eq!(line["section"], "outcome");
        assert_eq!(line["chunk"], "static/app");
        assert!(line["outcome"]["kind"].is_string(), "{line:#}");
    }
}

#[test]
fn validate_text_summarizes_counts_and_one_line_per_outcome() {
    let fixture = write_validate_fixture_spec(mixed_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "text"]);
    assert!(out.status.success(), "stderr={}", out.stderr);

    let stdout = out.stdout;
    for required in [
        "3 selector outcome(s): no_match=1, ambiguous=1, duplicate_claim=1",
        "[no_match] static/app::diagnostics/missing as `MissingFormatter`",
        "[ambiguous] static/app::diagnostics/ambiguous as `AmbiguousHelper`",
        "[duplicate_claim]",
    ] {
        assert!(stdout.contains(required), "missing {required:?}:\n{stdout}");
    }
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
    assert!(outcomes(&report).is_empty(), "{report:#}");

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
    assert!(outcomes(&report).is_empty(), "{report:#}");
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
    assert_eq!(
        report["counts"],
        json!({"no_match": 1, "ambiguous": 1}),
        "{report:#}"
    );

    let outcomes = outcomes(&report);
    let missing = find_outcome(outcomes, "no_match", "MissingWidget");
    assert_eq!(missing["chunk"], fixture.source_file.to_str().unwrap());
    assert_eq!(
        missing["placement"],
        json!({
            "logical_module": "ui/missing",
            "entity": {"export": "MissingWidget"},
            "selector_kind": "source_matches",
        })
    );
    assert_eq!(missing["target_binding"], "w");

    let ambiguous = find_outcome(outcomes, "ambiguous", "AmbiguousPanel");
    assert_eq!(ambiguous["placement"]["logical_module"], "ui/ambiguous");
    assert_eq!(
        ambiguous["outcome"]["candidates"],
        json!([
            {"owner": 0, "binding": "leftPanel"},
            {"owner": 1, "binding": "rightPanel"},
        ])
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
        assert_eq!(line["section"], "outcome");
        assert!(line["placement"]["logical_module"].is_string(), "{line:#}");
        assert!(
            line["placement"]["entity"]["export"].is_string(),
            "{line:#}"
        );
    }
    assert_eq!(parsed[2]["section"], "summary");
    assert_eq!(parsed[2]["counts"], json!({"no_match": 1, "ambiguous": 1}));
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
    assert!(outcomes(&report).is_empty(), "{report:#}");
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
    let outcomes = outcomes(&report);
    assert_eq!(outcomes.len(), 1, "{report:#}");
    let record = &outcomes[0];
    assert_eq!(
        record["placement"],
        json!({"logical_module": "ui/widget", "selector_kind": "annotations"})
    );
    assert_eq!(record["outcome"]["kind"], "invalid");
    assert!(
        record["outcome"]["error"]
            .as_str()
            .unwrap()
            .contains("annotations key `StaleWidget` does not match"),
        "{record:#}"
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
    assert_eq!(
        report["counts"],
        json!({"no_match": 1, "ambiguous": 1}),
        "{report:#}"
    );

    let outcomes = outcomes(&report);
    let missing = find_module_outcome(outcomes, "effects/missing");
    assert_eq!(missing["outcome"]["kind"], "no_match");
    assert_eq!(
        missing["placement"],
        json!({
            "logical_module": "effects/missing",
            "entity": {"anonymous_statement": 0},
            "selector_kind": "anonymous_statements.source_match",
        })
    );

    let ambiguous = find_module_outcome(outcomes, "effects/ambiguous");
    assert_eq!(
        ambiguous["outcome"],
        json!({
            "kind": "ambiguous",
            "candidates": [{"owner": 0}, {"owner": 1}],
            "truncated": false,
        })
    );
}

#[test]
fn validate_source_only_reports_multi_statement_anonymous_selector_as_invalid() {
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
    let outcomes = outcomes(&report);
    assert_eq!(outcomes.len(), 1, "{report:#}");
    let record = &outcomes[0];
    assert_eq!(record["outcome"]["kind"], "invalid", "{record:#}");
    assert_eq!(
        record["placement"],
        json!({
            "logical_module": "effects/startup",
            "entity": {"anonymous_statement": 0},
            "selector_kind": "anonymous_statements.source_match",
        })
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
    assert_eq!(report["counts"], json!({"ambiguous": 2}), "{report:#}");

    let outcomes = outcomes(&report);
    for (export_name, target_binding, bindings) in [
        ("LeftPanel", "left", ["leftOne", "leftTwo"]),
        ("RightPanel", "right", ["rightOne", "rightTwo"]),
    ] {
        let record = find_outcome(outcomes, "ambiguous", export_name);
        assert_eq!(record["placement"]["selector_kind"], "source_matches");
        assert_eq!(record["target_binding"], target_binding);
        assert_eq!(
            record["outcome"]["candidates"],
            json!([
                {"owner": 0, "binding": bindings[0]},
                {"owner": 1, "binding": bindings[1]},
            ])
        );
    }
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

fn outcomes(report: &Value) -> &[Value] {
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
}

fn candidate_owners(record: &Value) -> Vec<u64> {
    record["outcome"]["candidates"]
        .as_array()
        .unwrap_or_else(|| panic!("candidates must be an array: {record:#}"))
        .iter()
        .map(|candidate| candidate["owner"].as_u64().unwrap())
        .collect()
}

fn find_module_outcome<'a>(outcomes: &'a [Value], logical_module: &str) -> &'a Value {
    outcomes
        .iter()
        .find(|record| record["placement"]["logical_module"] == logical_module)
        .unwrap_or_else(|| panic!("missing outcome for {logical_module}: {outcomes:#?}"))
}
