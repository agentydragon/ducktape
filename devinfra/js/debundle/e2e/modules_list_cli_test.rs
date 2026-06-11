//! End-to-end exercise of `debundle modules list`'s filters by
//! shelling out to the built binary against a tiny modules fixture.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn debundle_binary() -> PathBuf {
    // Test data declared in BUILD.bazel adds the debundle binary at
    // this runfiles path. The runfiles helper is overkill for a
    // single binary; reach for it directly via the env var.
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

fn setup_modules_fixture(root: &Path) {
    write(
        &root.join("runtime/plugins.yaml"),
        "comment: plugin glue layer\nmembers:\n  - selector: { binding: { name: XOe } }\n",
    );
    write(
        &root.join("ui/sidebar.yaml"),
        "members:\n  - selector: { binding: { name: YOe } }\n  - selector: { binding: { name: ZOe } }\n",
    );
    write(&root.join("residual/unhandled.yaml"), "members: []\n");
    write(&root.join("ui/empty.yaml"), "members: []\n");
}

#[test]
fn modules_list_emits_every_module_with_counts() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    setup_modules_fixture(&modules);

    let output = Command::new(debundle_binary())
        .args([
            "modules",
            "list",
            "--modules",
            modules.to_str().unwrap(),
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        output.status.success(),
        "non-zero exit: stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    let modules_arr = parsed["modules"].as_array().unwrap();
    assert_eq!(modules_arr.len(), 4);
    let paths: Vec<&str> = modules_arr
        .iter()
        .map(|m| m["path"].as_str().unwrap())
        .collect();
    assert!(paths.contains(&"runtime/plugins"));
    assert!(paths.contains(&"residual/unhandled"));
}

#[test]
fn modules_list_residual_filter() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    setup_modules_fixture(&modules);

    let output = Command::new(debundle_binary())
        .args([
            "modules",
            "list",
            "--modules",
            modules.to_str().unwrap(),
            "--residual",
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(output.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let modules_arr = parsed["modules"].as_array().unwrap();
    assert_eq!(modules_arr.len(), 1);
    assert_eq!(modules_arr[0]["path"].as_str(), Some("residual/unhandled"));
}

#[test]
fn modules_list_empty_filter() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    setup_modules_fixture(&modules);

    let output = Command::new(debundle_binary())
        .args([
            "modules",
            "list",
            "--modules",
            modules.to_str().unwrap(),
            "--empty",
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(output.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let modules_arr = parsed["modules"].as_array().unwrap();
    // Both `residual/unhandled` and `ui/empty` are empty.
    assert_eq!(modules_arr.len(), 2);
}

#[test]
fn modules_list_picks_up_modules_env_var() {
    // CLI flags override env vars (per docs/cli.md); both pointing at
    // the same dir is enough to prove env-var plumbing parses.
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    setup_modules_fixture(&modules);

    let output = Command::new(debundle_binary())
        .args(["modules", "list", "--format", "json"])
        .env("DEBUNDLE_MODULES", &modules)
        .output()
        .expect("spawn debundle");
    assert!(
        output.status.success(),
        "non-zero exit: stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(parsed["modules"].as_array().unwrap().len(), 4);
}
