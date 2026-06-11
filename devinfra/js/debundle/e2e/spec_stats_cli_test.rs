//! End-to-end exercise of `debundle spec stats` by shelling out to the
//! built binary against tiny modules-tree fixtures.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

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

fn run_stats(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec!["spec", "stats", "--modules", modules.to_str().unwrap()];
    args.extend_from_slice(extra);
    let out = Command::new(debundle_binary())
        .args(&args)
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "non-zero exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

#[test]
fn one_module_one_binding_emits_expected_totals() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(
        &modules.join("solo.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n",
    );

    let out = run_stats(&modules, &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["modules"]["total"], 1);
    assert_eq!(parsed["modules"]["residual"], 0);
    assert_eq!(parsed["modules"]["empty"], 0);
    assert_eq!(parsed["modules"]["with_comment"], 0);
    assert_eq!(parsed["modules"]["member_count"]["min"], 1);
    assert_eq!(parsed["modules"]["member_count"]["max"], 1);
    assert_eq!(parsed["modules"]["member_count"]["singletons"], 1);
    assert_eq!(parsed["modules"]["member_count"]["tiny_2_to_5"], 0);
    assert_eq!(parsed["modules"]["member_count"]["medium_6_to_20"], 0);
    assert_eq!(parsed["modules"]["member_count"]["large_21_plus"], 0);
    assert_eq!(parsed["bindings"]["total"], 1);
    assert_eq!(parsed["bindings"]["renamed"], 0);
    assert_eq!(parsed["bindings"]["unrenamed"], 1);
    assert_eq!(parsed["bindings"]["orphan"], 1);
    assert_eq!(parsed["bindings"]["with_comment"], 0);
}

#[test]
fn singleton_plus_multi_member_bucket_counts() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    // singleton with a readable name -> renamed + orphan
    write(
        &modules.join("solo.yaml"),
        "members:\n  - name: Solo\n    selector: { binding: { name: a } }\n",
    );
    // multi-member (3 members, falls in tiny_2_to_5)
    write(
        &modules.join("group.yaml"),
        "members:\n\
         \x20\x20- selector: { binding: { name: b } }\n\
         \x20\x20- selector: { binding: { name: c } }\n\
         \x20\x20- selector: { binding: { name: d } }\n",
    );

    let out = run_stats(&modules, &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["modules"]["total"], 2);
    assert_eq!(parsed["modules"]["member_count"]["singletons"], 1);
    assert_eq!(parsed["modules"]["member_count"]["tiny_2_to_5"], 1);
    assert_eq!(parsed["modules"]["member_count"]["min"], 1);
    assert_eq!(parsed["modules"]["member_count"]["max"], 3);
    assert_eq!(parsed["bindings"]["total"], 4);
    assert_eq!(parsed["bindings"]["renamed"], 1);
    assert_eq!(parsed["bindings"]["unrenamed"], 3);
    // Only `a` is an orphan (it's the only member of `solo`).
    assert_eq!(parsed["bindings"]["orphan"], 1);
}

#[test]
fn output_is_deterministic_across_runs() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
    );
    write(
        &modules.join("nested/c.yaml"),
        "members:\n  - selector: { binding: { name: c } }\n",
    );
    write(&modules.join("residual/unhandled.yaml"), "members: []\n");

    let out1 = run_stats(&modules, &["--format", "json"]);
    let out2 = run_stats(&modules, &["--format", "json"]);
    assert_eq!(out1.stdout, out2.stdout, "same spec -> same json");
}

#[test]
fn text_format_emits_non_empty_human_output() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(
        &modules.join("solo.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n",
    );

    let out = run_stats(&modules, &["--format", "text"]);
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert!(
        stdout.contains("modules:"),
        "missing modules header: {stdout}"
    );
    assert!(
        stdout.contains("bindings:"),
        "missing bindings header: {stdout}"
    );
    assert!(
        stdout.contains("singletons"),
        "missing bucket name: {stdout}"
    );
}

#[test]
fn ndjson_emits_one_line_per_section() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n",
    );

    let out = run_stats(&modules, &["--format", "ndjson"]);
    let stdout = String::from_utf8(out.stdout).unwrap();
    let lines: Vec<&str> = stdout.trim_end().split('\n').collect();
    assert_eq!(lines.len(), 2, "expected 2 lines, got {lines:?}");
    let l0: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
    let l1: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
    assert_eq!(l0["section"], "modules");
    assert_eq!(l1["section"], "bindings");
    assert_eq!(l0["total"], 1);
    assert_eq!(l1["total"], 1);
}

#[test]
fn residual_module_counted_under_modules_residual() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(
        &modules.join("ui/sidebar.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n",
    );
    write(&modules.join("residual/unhandled.yaml"), "members: []\n");

    let out = run_stats(&modules, &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["modules"]["total"], 2);
    assert_eq!(parsed["modules"]["residual"], 1);
    assert_eq!(parsed["modules"]["empty"], 1);
}
