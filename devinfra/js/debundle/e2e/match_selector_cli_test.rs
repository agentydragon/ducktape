//! End-to-end coverage for `debundle spec match-selector`: the prove-gate probe
//! that resolves a candidate `source_match` against a chunk and reports what it
//! binds and whether it is unique.

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

const CHUNK: &str = r#"const leftPanel = renderPanel("left");
const widgetConfig = { kind: "widget", label: "Primary" };
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
        "const w = { kind: \"widget\", ANYTHING };",
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
    let matches = report["matches"].as_array().unwrap();
    let names: Vec<&str> = matches
        .iter()
        .map(|matched| matched["binding_name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["leftPanel", "rightPanel"]);
}
