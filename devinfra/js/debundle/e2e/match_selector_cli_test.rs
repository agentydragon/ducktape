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
// the lone `makeWidget` call.
const CHUNK: &str = r#"const leftPanel = renderPanel("left");
const widgetConfig = makeWidget("widget", 3, theme);
const rightPanel = renderPanel("right");
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
        assert!(
            relaxed.contains("makeWidget") && relaxed.contains("ANYTHING"),
            "relaxed selector should keep the makeWidget anchor and add a hole: {relaxed}"
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
