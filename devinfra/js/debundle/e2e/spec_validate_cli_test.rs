//! End-to-end exercise of `debundle spec validate` by shelling
//! out to the built binary. The keep-going classification itself is pinned by
//! `selector_diagnostics_report_test`; this test pins the CLI verb: that one
//! pass surfaces the selector outcomes on stdout in each `--format`.

use std::process::Command;

use debundle_e2e_support::{
    CommandResult, FixtureOpts, Member, debundler_path, find_outcome, logical_module,
    mixed_selector_failure_fixture, outcomes, run_source_only_validate, run_spec_validate,
    write_text_file, write_validate_fixture_spec,
};
use serde_json::{Value, json};

#[test]
fn validate_ndjson_streams_one_object_per_outcome_plus_summary() {
    let fixture = write_validate_fixture_spec(mixed_selector_failure_fixture());
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
    let fixture = write_validate_fixture_spec(mixed_selector_failure_fixture());
    let out = run_spec_validate(&fixture.spec_path, &["--format", "text"]);
    assert!(out.status.success(), "stderr={}", out.stderr);

    let stdout = out.stdout;
    let expected: [(&str, &[&str]); 3] = [
        ("no_match", &["diagnostics/missing", "MissingFormatter"]),
        ("ambiguous", &["diagnostics/ambiguous", "AmbiguousHelper"]),
        ("duplicate_claim", &[]),
    ];
    for (kind, identifiers) in expected {
        let count = format!("{kind}=1");
        assert!(stdout.contains(&count), "missing {count:?}:\n{stdout}");
        let tag = format!("[{kind}]");
        let line = stdout
            .lines()
            .find(|line| line.contains(&tag))
            .unwrap_or_else(|| panic!("no {tag} line:\n{stdout}"));
        for identifier in identifiers {
            assert!(line.contains(identifier), "missing {identifier:?}: {line}");
        }
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
    assert!(text.status.success(), "stderr={}", text.stderr);

    let default = Command::new(debundler_path())
        .args(["spec", "validate", "--spec"])
        .arg(&fixture.spec_path)
        .args(["--format", "json"])
        .output()
        .unwrap();
    assert!(default.status.success(), "stderr={:?}", default.stderr);
}

#[test]
fn validate_reports_all_outcomes_before_failing_by_default() {
    let fixture = write_validate_fixture_spec(mixed_selector_failure_fixture());
    let output = Command::new(debundler_path())
        .args(["spec", "validate", "--spec"])
        .arg(&fixture.spec_path)
        .args(["--format", "json"])
        .output()
        .unwrap();
    let out = CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    };
    assert!(!out.status.success(), "stdout:\n{}", out.stdout);
    assert!(
        out.stderr.contains("validation found selector errors"),
        "{}",
        out.stderr
    );
    let report: Value = serde_json::from_str(&out.stdout).unwrap();
    assert_eq!(
        report["counts"],
        json!({"no_match": 1, "ambiguous": 1, "duplicate_claim": 1})
    );
}

#[test]
fn validate_source_only_fails_by_default_after_ndjson_report() {
    let fixture = write_source_only_validate_fixture();
    let output = Command::new(debundler_path())
        .args(["spec", "validate", "--modules"])
        .arg(&fixture.modules_root)
        .arg("--source-file")
        .arg(&fixture.source_file)
        .args(["--format", "ndjson"])
        .output()
        .unwrap();
    assert!(!output.status.success(), "stdout:\n{:?}", output.stdout);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let summary: Value = serde_json::from_str(stdout.lines().last().unwrap()).unwrap();
    assert_eq!(summary["section"], "summary");
    assert_eq!(summary["counts"], json!({"no_match": 1, "ambiguous": 1}));
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
fn validate_source_only_json_reports_every_source_selector_failure() {
    let fixture = write_source_only_validate_fixture();
    let report = fixture.json();
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
    let out = fixture.run("ndjson");

    let parsed: Vec<Value> = out
        .stdout
        .trim_end()
        .split('\n')
        .map(|line| serde_json::from_str(line).expect("ndjson line is valid json"))
        .collect();
    let (summary, records) = parsed.split_last().expect("a summary line");
    assert_eq!(summary["section"], "summary");
    assert_eq!(summary["counts"], json!({"no_match": 1, "ambiguous": 1}));
    // Outcomes first, then the matched templates' identifiers.
    let outcomes = records
        .iter()
        .take_while(|line| line["section"] == "outcome")
        .collect::<Vec<_>>();
    assert_eq!(outcomes.len(), 2, "stdout:\n{}", out.stdout);
    for line in outcomes {
        assert!(line["placement"]["logical_module"].is_string(), "{line:#}");
        assert!(
            line["placement"]["entity"]["export"].is_string(),
            "{line:#}"
        );
    }
    for line in &records[2..] {
        assert_eq!(line["section"], "template", "{line:#}");
        assert!(line["identifiers"].is_array(), "{line:#}");
    }
}

#[test]
fn validate_source_only_reports_stale_annotations() {
    let fixture = SourceOnlyValidateFixture::new(
        r#"const claimed = makeWidget("ok");
"#,
        &[(
            "ui/widget.yaml",
            r#"source_matches:
  - match: 'const selected = makeWidget("ok");'
    bindings:
      - local: selected
        name: Widget
annotations:
  StaleWidget:
    note: no matching claim
"#,
        )],
    );
    let report = fixture.json();
    let outcomes = outcomes(&report);
    assert_eq!(outcomes.len(), 1, "{report:#}");
    let record = &outcomes[0];
    assert_eq!(
        record["placement"],
        json!({"logical_module": "ui/widget", "selector_kind": "annotations"})
    );
    assert_eq!(record["outcome"]["kind"], "invalid");
}

#[test]
fn validate_source_only_reports_anonymous_statement_failures() {
    let fixture = SourceOnlyValidateFixture::new(
        r#"sideEffect("shared");
sideEffect("shared");
"#,
        &[
            (
                "effects/ambiguous.yaml",
                r#"anonymous_statements:
  - source_match:
      match: 'sideEffect("shared");'
"#,
            ),
            (
                "effects/missing.yaml",
                r#"anonymous_statements:
  - source_match:
      match: 'sideEffect("missing");'
"#,
            ),
        ],
    );
    let report = fixture.json();
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
            "differentiators": [
                {"owner": 0, "statement": 1, "anchor": "string literal \"shared\""},
                {"owner": 1, "statement": 0, "anchor": "string literal \"shared\""},
            ],
        })
    );
}

#[test]
fn validate_source_only_reports_multi_statement_anonymous_selector_as_invalid() {
    let fixture = SourceOnlyValidateFixture::new(
        r#"setup();
start();
"#,
        &[(
            "effects/startup.yaml",
            r#"anonymous_statements:
  - source_match:
      match: |
        setup();
        start();
"#,
        )],
    );
    let report = fixture.json();
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
    let fixture = SourceOnlyValidateFixture::new(
        r#"const leftOne = renderPanel("shared"), rightOne = renderPanel("shared");
const leftTwo = renderPanel("shared"), rightTwo = renderPanel("shared");
"#,
        &[(
            "ui/panels.yaml",
            r#"source_matches:
  - match: 'const left = renderPanel("shared"), right = renderPanel("shared");'
    bindings:
      - local: left
        name: LeftPanel
      - local: right
        name: RightPanel
"#,
        )],
    );
    let report = fixture.json();
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

impl SourceOnlyValidateFixture {
    fn new(source: &str, modules: &[(&str, &str)]) -> Self {
        let root = tempfile::tempdir().unwrap();
        let source_file = root.path().join("chunk.js");
        let modules_root = root.path().join("modules");
        write_text_file(&source_file, source);
        for (path, yaml) in modules {
            write_text_file(&modules_root.join(path), yaml);
        }
        Self {
            _root: root,
            source_file,
            modules_root,
        }
    }

    fn run(&self, format: &str) -> CommandResult {
        let out =
            run_source_only_validate(&self.modules_root, &self.source_file, &["--format", format]);
        assert!(
            out.status.success(),
            "source-only validate failed\nstdout:\n{}\nstderr:\n{}",
            out.stdout,
            out.stderr
        );
        out
    }

    fn json(&self) -> Value {
        let out = self.run("json");
        serde_json::from_str(&out.stdout)
            .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
    }
}

fn write_source_only_validate_fixture() -> SourceOnlyValidateFixture {
    SourceOnlyValidateFixture::new(
        r#"const leftPanel = renderPanel("shared");
const rightPanel = renderPanel("shared");
const widget = makeWidget("ok");
"#,
        &[
            (
                "ui/ok.yaml",
                r#"source_matches:
  - match: 'const w = makeWidget("ok");'
    bindings:
      - local: w
        name: Widget
"#,
            ),
            (
                "ui/missing.yaml",
                r#"source_matches:
  - match: 'const w = makeWidget("missing");'
    bindings:
      - local: w
        name: MissingWidget
"#,
            ),
            (
                "ui/ambiguous.yaml",
                r#"source_matches:
  - match: 'const panel = renderPanel("shared");'
    bindings:
      - local: panel
        name: AmbiguousPanel
"#,
            ),
        ],
    )
}

fn find_module_outcome<'a>(outcomes: &'a [Value], logical_module: &str) -> &'a Value {
    outcomes
        .iter()
        .find(|record| record["placement"]["logical_module"] == logical_module)
        .unwrap_or_else(|| panic!("missing outcome for {logical_module}: {outcomes:#?}"))
}

#[test]
fn misspelled_unassigned_mode_field_is_rejected() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new("const a = 1;", vec![]));
    let source = std::fs::read_to_string(&fixture.spec_path).unwrap();
    assert!(source.contains("kind: catchall_file"), "{source}");
    write_text_file(
        &fixture.spec_path,
        &source.replace(
            "kind: catchall_file",
            "kind: catchall_file\n    target_path: residual/typo",
        ),
    );
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(!out.status.success(), "{}", out.stdout);
    assert!(out.stderr.contains("target_path"), "{}", out.stderr);
}

#[test]
fn binding_patches_refuse_non_binding_selectors() {
    let run = debundle_e2e_support::run_tree_fixture(
        &debundle_e2e_support::TreeFixture {
            chunks: &[("main", "const a = 1;")],
            module_roots: &[("main", "main")],
            modules: &[
                ("main/empty.yaml", "members: []\n"),
                (
                    "../binding_patches.yaml",
                    "members:\n  - name: renamed\n    selector:\n      cross_ref: { references: a }\n",
                ),
            ],
        },
        &[],
    );
    assert!(!run.result.status.success(), "{}", run.result.stdout);
    assert!(
        run.result.stderr.contains("binding_patches"),
        "{}",
        run.result.stderr
    );
    assert!(
        run.result.stderr.contains("binding selector"),
        "{}",
        run.result.stderr
    );
}
