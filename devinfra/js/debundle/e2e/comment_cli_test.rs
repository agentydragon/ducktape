//! End-to-end exercise of `cli::comment`'s public Rust entry points
//! plus a fake-`$EDITOR` flow.
//!
//! Calls the library directly so this doesn't depend on the built
//! `debundle` binary. The fake editor is a small POSIX shell script
//! written into the tempdir; `EDITOR` is pointed at it for the
//! `--edit` path.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Mutex;

use debundle_cli::comment::{CommentMode, apply_binding_comment, apply_module_comment};
use serde_yaml::Value;
use tempfile::TempDir;

/// Editor invocation tweaks `EDITOR` / `VISUAL`, which are process-
/// global. Serialize the edit-mode tests so they don't race when
/// cargo / bazel runs the test binary's tests in parallel.
static EDITOR_LOCK: Mutex<()> = Mutex::new(());

fn write(root: &Path, rel: &str, body: &str) {
    let p = root.join(rel);
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(p, body).unwrap();
}

fn read(root: &Path, rel: &str) -> String {
    fs::read_to_string(root.join(rel)).unwrap()
}

fn install_fake_editor(dir: &Path, replacement: &str) -> std::path::PathBuf {
    // The editor receives the tempfile path as $1; overwrite the
    // contents with our canned replacement.
    let path = dir.join("fake-editor.sh");
    let script = format!(
        "#!/bin/sh\nprintf %s {} > \"$1\"\n",
        shell_quote(replacement)
    );
    fs::write(&path, script).unwrap();
    let mut perm = fs::metadata(&path).unwrap().permissions();
    perm.set_mode(0o755);
    fs::set_permissions(&path, perm).unwrap();
    path
}

fn shell_quote(s: &str) -> String {
    // Single-quote and escape any embedded single quotes.
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for c in s.chars() {
        if c == '\'' {
            out.push_str("'\\''");
        } else {
            out.push(c);
        }
    }
    out.push('\'');
    out
}

#[test]
fn binding_comment_set_read_clear_roundtrip() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "runtime/plugins.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );

    let set = apply_binding_comment(
        root,
        "XOe",
        CommentMode::Set("acquires plugin settings".into()),
        false,
    )
    .unwrap();
    assert_eq!(set.action, "set");
    assert_eq!(set.comment.as_deref(), Some("acquires plugin settings"));

    let read_out = apply_binding_comment(root, "XOe", CommentMode::Read, false).unwrap();
    assert_eq!(read_out.action, "read");
    assert_eq!(
        read_out.comment.as_deref(),
        Some("acquires plugin settings")
    );

    let body = read(root, "runtime/plugins.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert_eq!(
        doc["members"][0]["comment"].as_str(),
        Some("acquires plugin settings")
    );

    let cleared = apply_binding_comment(root, "XOe", CommentMode::Clear, false).unwrap();
    assert_eq!(cleared.action, "cleared");
    let body = read(root, "runtime/plugins.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert!(
        doc["members"][0]
            .as_mapping()
            .unwrap()
            .get(Value::String("comment".into()))
            .is_none()
    );
}

#[test]
fn module_comment_set_read_clear_roundtrip() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(root, "runtime/plugins.yaml", "members: []\n");

    let set = apply_module_comment(
        root,
        "runtime/plugins",
        CommentMode::Set("plugin glue layer".into()),
        false,
    )
    .unwrap();
    assert_eq!(set.action, "set");
    assert_eq!(set.comment.as_deref(), Some("plugin glue layer"));

    let body = read(root, "runtime/plugins.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert_eq!(doc["comment"].as_str(), Some("plugin glue layer"));

    let read_out = apply_module_comment(root, "runtime/plugins", CommentMode::Read, false).unwrap();
    assert_eq!(read_out.comment.as_deref(), Some("plugin glue layer"));

    let cleared = apply_module_comment(root, "runtime/plugins", CommentMode::Clear, false).unwrap();
    assert_eq!(cleared.action, "cleared");
    let body = read(root, "runtime/plugins.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert!(
        doc.as_mapping()
            .unwrap()
            .get(Value::String("comment".into()))
            .is_none()
    );
}

#[test]
fn ambiguous_sym_refuses_with_list_of_locations() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "a.yaml",
        "members:\n  - selector: { binding: { name: Foo } }\n",
    );
    write(
        root,
        "b.yaml",
        "members:\n  - name: Foo\n    selector: { binding: { name: YYY } }\n",
    );

    let err =
        apply_binding_comment(root, "Foo", CommentMode::Set("nope".into()), false).unwrap_err();
    let msg = format!("{err}");
    assert!(msg.contains("ambiguous"), "msg={msg}");
    assert!(msg.contains("a.yaml"), "msg={msg}");
    assert!(msg.contains("b.yaml"), "msg={msg}");
    // No file was modified.
    for f in ["a.yaml", "b.yaml"] {
        let body = read(root, f);
        assert!(!body.contains("comment:"), "{f} got mutated:\n{body}");
    }
}

#[test]
fn edit_mode_uses_fake_editor() {
    let _lock = EDITOR_LOCK.lock().unwrap();
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "m.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );

    let editor = install_fake_editor(root, "from-fake-editor");
    // SAFETY: tests in this file serialize on EDITOR_LOCK before
    // mutating the process-global env.
    unsafe {
        std::env::set_var("EDITOR", &editor);
        std::env::remove_var("VISUAL");
    }
    let out = apply_binding_comment(root, "XOe", CommentMode::Edit, false).unwrap();
    unsafe {
        std::env::remove_var("EDITOR");
    }

    assert_eq!(out.action, "set");
    assert_eq!(out.comment.as_deref(), Some("from-fake-editor"));
    let body = read(root, "m.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert_eq!(
        doc["members"][0]["comment"].as_str(),
        Some("from-fake-editor")
    );
}

#[test]
fn edit_mode_with_empty_buffer_clears_comment() {
    let _lock = EDITOR_LOCK.lock().unwrap();
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "m.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n    comment: existing\n",
    );

    let editor = install_fake_editor(root, "");
    unsafe {
        std::env::set_var("EDITOR", &editor);
        std::env::remove_var("VISUAL");
    }
    let out = apply_binding_comment(root, "XOe", CommentMode::Edit, false).unwrap();
    unsafe {
        std::env::remove_var("EDITOR");
    }
    assert_eq!(out.action, "cleared");
    let body = read(root, "m.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert!(
        doc["members"][0]
            .as_mapping()
            .unwrap()
            .get(Value::String("comment".into()))
            .is_none()
    );
}

#[test]
fn dry_run_set_does_not_modify_file() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let initial = "members:\n  - selector: { binding: { name: XOe } }\n";
    write(root, "m.yaml", initial);
    let out = apply_binding_comment(
        root,
        "XOe",
        CommentMode::Set("should not stick".into()),
        true,
    )
    .unwrap();
    assert_eq!(out.action, "dry-run");
    assert_eq!(read(root, "m.yaml"), initial);
}
