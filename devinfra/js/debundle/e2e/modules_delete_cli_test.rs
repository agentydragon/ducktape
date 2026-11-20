//! CLI deletion refusal, dry-run, force, and batch atomicity.

use std::fs;

use debundle_e2e_support::{process, run_debundle, write_text_file};
use tempfile::TempDir;

#[test]
fn delete_empty_module_succeeds() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_text_file(&root.join("ui/empty.yaml"), "members: []\n");

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "ui/empty.yaml",
        "--no-verify",
        // Stdout is a pipe here, which would default to JSON;
        // pin the human text rendering explicitly.
        "--format",
        "text",
    ]);
    process::assert_success(&out);
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
    write_text_file(&root.join("ui/full.yaml"), body);

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "ui/full.yaml",
    ]);
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("1 claim(s)") && stderr.contains("--force"),
        "expected refusal naming claim count + --force, got stderr: {stderr}",
    );
    // File must still be on disk.
    assert!(root.join("ui/full.yaml").exists());
    assert_eq!(fs::read_to_string(root.join("ui/full.yaml")).unwrap(), body);
}

#[test]
fn delete_non_empty_module_with_force_succeeds() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_text_file(
        &root.join("ui/full.yaml"),
        "members:\n  - selector: { binding: { name: a } }\n",
    );

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "ui/full.yaml",
        "--force",
        // Skip the gate: this test only exercises the
        // --force filesystem path, not the gate verdict.
        "--no-verify",
    ]);
    process::assert_success(&out);
    assert!(!root.join("ui/full.yaml").exists());
}

#[test]
fn delete_multiple_atomic_all_succeed() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_text_file(&root.join("a.yaml"), "members: []\n");
    write_text_file(&root.join("b.yaml"), "members: []\n");
    write_text_file(&root.join("c.yaml"), "members: []\n");

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "a.yaml",
        "b.yaml",
        "c.yaml",
        "--no-verify",
        "--format",
        "text",
    ]);
    process::assert_success(&out);
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
    write_text_file(&root.join("ui/empty.yaml"), body);

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "ui/empty",
        "--no-verify",
        "--dry-run",
        "--format",
        "text",
    ]);
    process::assert_success(&out);
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

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "does/not/exist.yaml",
    ]);
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
    write_text_file(&root.join("x.yaml"), empty_body);
    write_text_file(&root.join("y.yaml"), full_body);
    write_text_file(&root.join("z.yaml"), empty_body);

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        root.to_str().unwrap(),
        "x.yaml",
        "y.yaml",
        "z.yaml",
    ]);
    assert!(!out.status.success(), "expected non-zero exit");
    // Nothing must be deleted.
    assert!(root.join("x.yaml").exists());
    assert!(root.join("y.yaml").exists());
    assert!(root.join("z.yaml").exists());
}
