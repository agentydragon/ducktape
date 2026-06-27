//! End-to-end coverage for `debundle spec match-selector`: the prove-gate probe
//! that resolves a candidate `source_match` against a chunk and reports what it
//! binds, whether it is unique, and (by default) how much further it could be
//! holed.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

fn debundle_binary() -> PathBuf {
    let runfiles_path = std::env::var("RUNFILES_DIR")
        .or_else(|_| std::env::var("TEST_SRCDIR"))
        .expect("runfiles env var");
    let candidate = Path::new(&runfiles_path).join("_main/devinfra/js/debundle/debundle");
    assert!(
        candidate.exists(),
        "debundle binary not at {}",
        candidate.display()
    );
    candidate
}

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn match_selector(source: &Path, match_source: &str, extra: &[&str]) -> Value {
    let mut args = vec![
        "spec",
        "match-selector",
        "--source-file",
        source.to_str().unwrap(),
        "--match",
        match_source,
        "--format",
        "json",
    ];
    args.extend_from_slice(extra);
    let out = Command::new(debundle_binary())
        .args(&args)
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "non-zero exit\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err}):\n{}",
            String::from_utf8_lossy(&out.stdout)
        )
    })
}

// leftPanel and rightPanel differ only by their string argument; widgetConfig is
// the lone `makeWidget` call; errorState is the lone object literal; computeTotal
// is the lone function declaration.
const CHUNK: &str = r#"const leftPanel = renderPanel("left");
const widgetConfig = makeWidget("widget", 3, theme);
const rightPanel = renderPanel("right");
const errorState = { kind: "error", code: 500, retry: false };
function computeTotal(items) {
  const base = items.length;
  const tax = base * 2;
  return base + tax;
}
"#;

fn fixture() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    write(&source, CHUNK);
    (dir, source)
}

#[test]
fn unique_match_reports_the_bound_target() {
    let (_dir, source) = fixture();
    let report = match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    let matches = report["matches"].as_array().unwrap();
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0]["body_index"], 1);
    assert_eq!(matches[0]["binding_name"], "widgetConfig");
}

#[test]
fn split_declarator_match_reports_pre_split_body_index() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    write(
        &source,
        "const runtimeFirst = build(\"left\"), runtimeSecond = build(\"right\");\n\
         const after = initAfter();\n",
    );
    let report = match_selector(
        &source,
        "const first = build(\"left\"), second = build(\"right\");",
        &["--target-binding", "second", "--no-slack"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    let matches = report["matches"].as_array().unwrap();
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0]["body_index"], 0);
    assert_eq!(matches[0]["binding_name"], "runtimeSecond");
}

#[test]
fn no_match_is_not_unique() {
    let (_dir, source) = fixture();
    let report = match_selector(
        &source,
        "const w = { kind: \"missing\" };",
        &["--target-binding", "w"],
    );
    assert_eq!(report["unique"], Value::Bool(false));
    assert!(report["matches"].as_array().unwrap().is_empty());
    // Slack is undefined for a non-unique selector.
    assert!(report.get("slack").is_none());
}

#[test]
fn ambiguous_match_lists_every_candidate_in_body_order() {
    let (_dir, source) = fixture();
    let report = match_selector(
        &source,
        "const p = renderPanel(ANYTHING);",
        &["--target-binding", "p"],
    );
    assert_eq!(report["unique"], Value::Bool(false));
    let names: Vec<&str> = report["matches"]
        .as_array()
        .unwrap()
        .iter()
        .map(|matched| matched["binding_name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["leftPanel", "rightPanel"]);
}

#[test]
fn over_pinned_selector_reports_holeable_slack() {
    let (_dir, source) = fixture();
    // The callee + arity already single out the one `makeWidget` call, so the
    // pinned literal arguments are unnecessary precision.
    let report = match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    let slack = report["slack"].as_array().unwrap();
    assert!(!slack.is_empty(), "expected over-pin slack, got {report}");
    for relaxation in slack {
        let relaxed = relaxation["relaxed_match"].as_str().unwrap();
        // Every slack variant keeps the discriminating `makeWidget` callee; the
        // arguments were the unnecessary precision (holed to ANYTHING or dropped
        // via ARGS).
        assert!(
            relaxed.contains("makeWidget"),
            "relaxed selector should keep the makeWidget anchor: {relaxed}"
        );
    }
}

#[test]
fn minimally_pinned_selector_reports_empty_slack() {
    let (_dir, source) = fixture();
    // The "left" literal is the only thing distinguishing leftPanel from
    // rightPanel; holing it would make the selector ambiguous, so there is no
    // slack to report.
    let report = match_selector(
        &source,
        "const p = renderPanel(\"left\");",
        &["--target-binding", "p"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    assert_eq!(report["matches"][0]["binding_name"], "leftPanel");
    assert!(report["slack"].as_array().unwrap().is_empty());
}

#[test]
fn no_slack_flag_skips_slack_analysis() {
    let (_dir, source) = fixture();
    let report = match_selector(
        &source,
        "const w = makeWidget(\"widget\", 3, theme);",
        &["--target-binding", "w", "--no-slack"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    // With --no-slack the field is omitted even though the selector is unique.
    assert!(report.get("slack").is_none());
}

#[test]
fn slack_drops_an_unneeded_object_property() {
    let (_dir, source) = fixture();
    // errorState is the only object literal, so any one of its keys alone pins
    // it — the others are droppable kvps, not just holeable values.
    let report = match_selector(
        &source,
        "const e = { kind: \"error\", code: 500, retry: false };",
        &["--target-binding", "e"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    assert_eq!(report["matches"][0]["binding_name"], "errorState");
    let slack = report["slack"].as_array().unwrap();
    // A property-drop relaxation removes the `code` kvp entirely (key and value),
    // which value-holing alone could never do.
    assert!(
        slack.iter().any(|relaxation| !relaxation["relaxed_match"]
            .as_str()
            .unwrap()
            .contains("code")),
        "expected a relaxation that drops the `code` property: {report}"
    );
}

#[test]
fn slack_drops_a_statement_from_an_over_pinned_body() {
    let (_dir, source) = fixture();
    // computeTotal is the only function, so its body statements are not needed
    // for uniqueness and collapse to STMT_LIST runs.
    let report = match_selector(
        &source,
        "function f(items) { const base = items.length; const tax = base * 2; return base + tax; }",
        &["--target-binding", "f"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    assert_eq!(report["matches"][0]["binding_name"], "computeTotal");
    let slack = report["slack"].as_array().unwrap();
    assert!(
        slack.iter().any(|relaxation| relaxation["relaxed_match"]
            .as_str()
            .unwrap()
            .contains("STMT_LIST")),
        "expected a relaxation that drops a body statement: {report}"
    );
}

#[test]
fn slack_drops_a_destructure_pattern_property() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("app.js");
    // One object-destructuring statement; the `loadConfig` initializer plus any
    // one destructured key already pin it, so the sibling pattern props are
    // droppable (the destructure analogue of an object-literal property drop).
    write(
        &source,
        "const lone = initLone();\nconst { primary, secondary, tertiary } = loadConfig();\n",
    );
    let report = match_selector(
        &source,
        "const { primary, secondary, tertiary } = loadConfig();",
        &["--target-binding", "primary"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    assert_eq!(report["matches"][0]["binding_name"], "primary");
    let slack = report["slack"].as_array().unwrap();
    // A pattern-prop drop removes the `secondary` binding from the destructure
    // entirely (not just holing a value), while the target `primary` stays
    // declared — the guard never drops the target's own binding.
    assert!(
        slack.iter().any(|relaxation| {
            let relaxed = relaxation["relaxed_match"].as_str().unwrap();
            relaxed.contains("primary") && !relaxed.contains("secondary")
        }),
        "expected a relaxation that drops a destructure property: {report}"
    );
}

#[test]
fn slack_drops_a_top_level_context_statement() {
    let (_dir, source) = fixture();
    // The `makeWidget` call alone pins widgetConfig, so the trailing `rightPanel`
    // context statement is unnecessary precision and is dropped outright (a
    // top-level STMT_LIST is not honored on the member-resolution path). No error:
    // the guard keeps the target's own declaration, which the matcher requires
    // `target_binding` to name.
    let report = match_selector(
        &source,
        "const widgetConfig = makeWidget(\"widget\", 3, theme);\nconst rightPanel = renderPanel(\"right\");",
        &["--target-binding", "widgetConfig"],
    );
    assert_eq!(report["unique"], Value::Bool(true));
    assert_eq!(report["matches"][0]["binding_name"], "widgetConfig");
    let slack = report["slack"].as_array().unwrap();
    // The context-statement drop removes the whole `rightPanel` statement (binding
    // and all) while keeping the `makeWidget` target — distinct from value-holing,
    // which would keep the `rightPanel` binding with its init holed.
    assert!(
        slack.iter().any(|relaxation| {
            let relaxed = relaxation["relaxed_match"].as_str().unwrap();
            relaxed.contains("makeWidget") && !relaxed.contains("rightPanel")
        }),
        "expected a top-level context-statement drop: {report}"
    );
}
