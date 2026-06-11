//! End-to-end check of `debundle modules delete` against tempdir
//! fixtures. The structural Rust entry point is hit directly via
//! `cli::module::delete_modules`, and the CLI surface (refusal
//! semantics, `--force`, `--dry-run`, `--no-verify`, atomicity) is
//! exercised by shelling out to the built `debundle` binary so the
//! clap wiring and the env-var plumbing are covered too.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use debundle_cli::module::delete_modules;
use tempfile::TempDir;

fn debundle_binary() -> PathBuf {
    let runfiles_path = std::env::var("RUNFILES_DIR")
        .or_else(|_| std::env::var("TEST_SRCDIR"))
        .expect("runfiles env var");
    Path::new(&runfiles_path).join("_main/devinfra/js/debundle/debundle")
}

fn write(root: &Path, rel: &str, body: &str) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

#[test]
fn delete_empty_module_succeeds() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(root, "ui/empty.yaml", "members: []\n");

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "ui/empty.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!root.join("ui/empty.yaml").exists());
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("deleted 1 file(s)"),
        "expected 'deleted 1 file(s)' on stdout, got {stdout:?}",
    );
}

#[test]
fn delete_non_empty_module_without_force_refuses() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let body = "members:\n  - selector: { binding: { name: a } }\n";
    write(root, "ui/full.yaml", body);

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "ui/full.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("1 member(s)") && stderr.contains("--force"),
        "expected refusal naming member count + --force, got stderr: {stderr}",
    );
    // File must still be on disk.
    assert!(root.join("ui/full.yaml").exists());
    assert_eq!(fs::read_to_string(root.join("ui/full.yaml")).unwrap(), body);
}

#[test]
fn delete_non_empty_module_with_force_succeeds() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "ui/full.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "ui/full.yaml",
            "--force",
            // Skip the gate: this test only exercises the
            // --force filesystem path, not the gate verdict.
            "--no-verify",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!root.join("ui/full.yaml").exists());
}

#[test]
fn delete_multiple_atomic_all_succeed() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(root, "a.yaml", "members: []\n");
    write(root, "b.yaml", "members: []\n");
    write(root, "c.yaml", "members: []\n");

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "a.yaml",
            "b.yaml",
            "c.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!root.join("a.yaml").exists());
    assert!(!root.join("b.yaml").exists());
    assert!(!root.join("c.yaml").exists());
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("deleted 3 file(s)"),
        "expected 'deleted 3 file(s)', got {stdout:?}",
    );
}

#[test]
fn dry_run_prints_verdict_without_deleting() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let body = "members: []\n";
    write(root, "ui/empty.yaml", body);

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "ui/empty.yaml",
            "--dry-run",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("dry-run: would delete 1 file(s)"),
        "expected dry-run verdict on stdout, got {stdout:?}",
    );
    assert!(
        root.join("ui/empty.yaml").exists(),
        "file must remain on disk"
    );
    assert_eq!(
        fs::read_to_string(root.join("ui/empty.yaml")).unwrap(),
        body
    );
}

#[test]
fn delete_nonexistent_module_clear_error() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "does/not/exist.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("does not exist"),
        "expected 'does not exist' error, got stderr: {stderr}",
    );
}

#[test]
fn batch_with_one_non_empty_refuses_atomically() {
    // Three modules; the second one has a member. Without --force,
    // the whole batch must refuse and none should be deleted.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let empty_body = "members: []\n";
    let full_body = "members:\n  - selector: { binding: { name: a } }\n";
    write(root, "x.yaml", empty_body);
    write(root, "y.yaml", full_body);
    write(root, "z.yaml", empty_body);

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            root.to_str().unwrap(),
            "x.yaml",
            "y.yaml",
            "z.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success(), "expected non-zero exit");
    // Nothing must be deleted.
    assert!(root.join("x.yaml").exists());
    assert!(root.join("y.yaml").exists());
    assert!(root.join("z.yaml").exists());
}

// Library-level coverage for `delete_modules` — confirms the
// filesystem half of the operation in isolation from the CLI.
#[test]
fn delete_modules_library_call_deletes_then_reports() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(root, "a.yaml", "members: []\n");
    write(root, "b.yaml", "members: []\n");
    let abs = [root.join("a.yaml"), root.join("b.yaml")];

    let summary = delete_modules(&abs, false).unwrap();
    assert_eq!(summary.deleted.len(), 2);
    assert!(!summary.dry_run);
    assert!(summary.summary_line().contains("deleted 2 file(s)"));
    assert!(!root.join("a.yaml").exists());
    assert!(!root.join("b.yaml").exists());
}

#[test]
fn delete_modules_library_dry_run_leaves_files_in_place() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(root, "a.yaml", "members: []\n");
    let abs = [root.join("a.yaml")];

    let summary = delete_modules(&abs, true).unwrap();
    assert_eq!(summary.deleted.len(), 1);
    assert!(summary.dry_run);
    assert!(
        summary
            .summary_line()
            .contains("dry-run: would delete 1 file(s)")
    );
    assert!(root.join("a.yaml").exists());
}
