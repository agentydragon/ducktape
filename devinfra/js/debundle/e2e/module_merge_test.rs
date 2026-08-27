//! End-to-end check of `cli::module::merge_modules` against a tempdir
//! fixture. Hits the public Rust function directly so the test does
//! not depend on the built `debundle` binary.

use std::fs;
use std::path::Path;
use std::process::Command;

use debundle_cli::module::merge_modules;
use debundle_e2e_support::{debundler_path, write_file};
use serde_yaml::Value;
use tempfile::TempDir;

fn member_names(doc: &Value) -> Vec<String> {
    doc["members"]
        .as_sequence()
        .expect("members sequence")
        .iter()
        .map(|m| {
            m["selector"]["binding"]["name"]
                .as_str()
                .expect("name")
                .to_string()
        })
        .collect()
}

#[test]
fn merges_two_sources_into_target_and_deletes_sources() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();

    write_file(
        root,
        "ui/target.yaml",
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_file(
        root,
        "ui/src1.yaml",
        "members:\n  - selector: { binding: { name: bravo } }\n",
    );
    write_file(
        root,
        "ui/src2.yaml",
        "members:\n  - selector: { binding: { name: charlie } }\n",
    );

    let summary = merge_modules(
        root,
        Path::new("ui/target.yaml"),
        &[Path::new("ui/src1.yaml"), Path::new("ui/src2.yaml")],
    )
    .expect("merge succeeds");

    assert_eq!(summary.merged_sources.len(), 2);
    let line = summary.summary_line();
    assert!(line.contains("merged 2 source(s) into"), "line={line}");
    assert!(line.contains("ui/target.yaml"), "line={line}");

    assert!(!root.join("ui/src1.yaml").exists());
    assert!(!root.join("ui/src2.yaml").exists());
    assert!(root.join("ui/target.yaml").exists());

    let merged_text = fs::read_to_string(root.join("ui/target.yaml")).unwrap();
    // Provenance lives in a durable, non-emitting `note:` field, not a
    // `#` YAML comment the rewriters would drop on the next edit.
    assert!(
        !merged_text.contains("# merged from"),
        "provenance must not be a `#` comment:\n{merged_text}"
    );
    let merged: Value = serde_yaml::from_str(&merged_text).unwrap();
    assert_eq!(
        merged["note"].as_str(),
        Some("merged from: ui/src1.yaml, ui/src2.yaml"),
        "missing provenance note in:\n{merged_text}"
    );
    assert_eq!(member_names(&merged), vec!["alpha", "bravo", "charlie"]);
}

#[test]
fn duplicate_member_name_across_sources_errors_and_keeps_sources() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();

    write_file(
        root,
        "target.yaml",
        "members:\n  - selector: { binding: { name: keep } }\n",
    );
    write_file(
        root,
        "a.yaml",
        "members:\n  - selector: { binding: { name: collide } }\n",
    );
    write_file(
        root,
        "b.yaml",
        "members:\n  - selector: { binding: { name: collide } }\n",
    );

    let err = merge_modules(
        root,
        Path::new("target.yaml"),
        &[Path::new("a.yaml"), Path::new("b.yaml")],
    )
    .expect_err("collision must error");
    let msg = format!("{err}");
    assert!(
        msg.contains("duplicate member name \"collide\""),
        "msg={msg}"
    );

    // Sources must remain on disk after a failed merge so the author
    // can fix the conflict and re-run.
    assert!(root.join("a.yaml").exists());
    assert!(root.join("b.yaml").exists());
}

#[test]
fn duplicate_member_name_between_member_and_source_match_binding_errors() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();

    write_file(
        root,
        "target.yaml",
        "members:\n  - name: Widget\n    selector: { binding: { name: minifiedWidget } }\n",
    );
    write_file(
        root,
        "src.yaml",
        "source_matches:\n  - match: 'const selected = makeWidget();'\n    bindings:\n      - local: selected\n        name: Widget\n",
    );

    let err = merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")])
        .expect_err("collision must error");
    let msg = format!("{err}");
    assert!(
        msg.contains("duplicate member name \"Widget\""),
        "msg={msg}"
    );

    assert!(root.join("src.yaml").exists());
}

#[test]
fn modules_merge_new_subcommand_path_works_through_binary() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_file(
        root,
        "target.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    );
    write_file(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: b } }\n",
    );

    let out = Command::new(debundler_path())
        .args([
            "modules",
            "merge",
            "--modules",
            root.to_str().unwrap(),
            "--target",
            "target.yaml",
            "src.yaml",
            // Skip the gate: this test only exercises the YAML
            // splice surface, not the realizability gate.
            "--no-verify",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!root.join("src.yaml").exists());
    let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    assert_eq!(member_names(&doc), vec!["a", "b"]);
}

#[test]
fn modules_merge_can_create_missing_target_through_binary() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let src_body = "members:\n  - selector: { binding: { name: a } }\n";
    write_file(root, "src.yaml", src_body);

    let dry_run = Command::new(debundler_path())
        .args([
            "modules",
            "merge",
            "--modules",
            root.to_str().unwrap(),
            "--target",
            "new/group",
            "src",
            "--dry-run",
            "--no-verify",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        dry_run.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&dry_run.stderr)
    );
    assert!(
        String::from_utf8_lossy(&dry_run.stdout).contains("dry-run"),
        "expected dry-run verdict on stdout, got {:?}",
        String::from_utf8_lossy(&dry_run.stdout)
    );
    assert!(!root.join("new/group.yaml").exists());
    assert_eq!(fs::read_to_string(root.join("src.yaml")).unwrap(), src_body);

    let out = Command::new(debundler_path())
        .args([
            "modules",
            "merge",
            "--modules",
            root.to_str().unwrap(),
            "--target",
            "new/group",
            "src",
            "--no-verify",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!root.join("src.yaml").exists());
    let merged = fs::read_to_string(root.join("new/group.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    assert_eq!(member_names(&doc), vec!["a"]);
}

#[test]
fn modules_merge_dry_run_does_not_modify_files() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let src_body = "members:\n  - selector: { binding: { name: b } }\n";
    let target_body = "members:\n  - selector: { binding: { name: a } }\n";
    write_file(root, "target.yaml", target_body);
    write_file(root, "src.yaml", src_body);

    let out = Command::new(debundler_path())
        .args([
            "modules",
            "merge",
            "--modules",
            root.to_str().unwrap(),
            "--target",
            "target.yaml",
            "src.yaml",
            "--dry-run",
            // Skip the gate: this test only exercises the YAML
            // splice surface.
            "--no-verify",
        ])
        .output()
        .expect("spawn debundle");
    assert!(out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stdout).contains("dry-run"),
        "expected dry-run verdict on stdout, got {:?}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(root.join("src.yaml").exists(), "src must not be deleted");
    assert_eq!(fs::read_to_string(root.join("src.yaml")).unwrap(), src_body);
    assert_eq!(
        fs::read_to_string(root.join("target.yaml")).unwrap(),
        target_body
    );
}

#[test]
fn merge_carries_source_matches_into_target() {
    // `source_matches:` entries are claims just like `members:` —
    // destroying them with the source file silently unclaims their
    // owners on the next `debundle run`.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_file(
        root,
        "target.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    );
    write_file(
        root,
        "src.yaml",
        "source_matches:\n  - match: 'const x = 1;'\n    bindings:\n      - local: x\n        name: ExportedX\nmembers: []\n",
    );

    merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")]).unwrap();

    let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    let groups = doc["source_matches"]
        .as_sequence()
        .unwrap_or_else(|| panic!("source_matches must be carried into the target: {merged}"));
    assert_eq!(groups.len(), 1, "merged={merged}");
    assert_eq!(
        groups[0]["bindings"][0]["name"].as_str(),
        Some("ExportedX"),
        "merged={merged}"
    );
}

#[test]
fn merge_concatenates_module_comments_with_divider() {
    // README.md § "Comments" promises: `modules merge` concatenates
    // source-module comments into the target's module-level `comment:`
    // with a `--- from <source>:` divider.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_file(
        root,
        "target.yaml",
        "comment: target overview\nmembers:\n  - selector: { binding: { name: a } }\n",
    );
    write_file(
        root,
        "src1.yaml",
        "comment: src1 notes\nmembers:\n  - selector: { binding: { name: b } }\n",
    );
    write_file(
        root,
        "src2.yaml",
        "members:\n  - selector: { binding: { name: c } }\n",
    );

    merge_modules(
        root,
        Path::new("target.yaml"),
        &[Path::new("src1.yaml"), Path::new("src2.yaml")],
    )
    .unwrap();

    let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    let comment = doc["comment"].as_str().expect("merged comment present");
    assert!(comment.contains("target overview"), "comment={comment}");
    assert!(comment.contains("--- from src1.yaml:"), "comment={comment}");
    assert!(comment.contains("src1 notes"), "comment={comment}");
    assert!(
        !comment.contains("src2.yaml"),
        "comment-less sources add no divider: {comment}"
    );
}

#[test]
fn merge_into_uncommented_target_adopts_source_comment_with_divider() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_file(
        root,
        "target.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    );
    write_file(
        root,
        "src.yaml",
        "comment: src notes\nmembers:\n  - selector: { binding: { name: b } }\n",
    );

    merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")]).unwrap();

    let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    let comment = doc["comment"].as_str().expect("merged comment present");
    assert!(comment.contains("--- from src.yaml:"), "comment={comment}");
    assert!(comment.contains("src notes"), "comment={comment}");
}

#[test]
fn merge_appends_provenance_to_existing_note() {
    // `modules merge` composes its `merged from:` provenance with any
    // existing module-level `note:` rather than clobbering it. The note
    // is non-emitting scratch metadata that survives rewriter edits.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write_file(
        root,
        "target.yaml",
        "note: hand-authored debt rationale\nmembers:\n  - selector: { binding: { name: a } }\n",
    );
    write_file(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: b } }\n",
    );

    merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")]).unwrap();

    let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&merged).unwrap();
    let note = doc["note"].as_str().expect("merged note present");
    assert_eq!(
        note, "hand-authored debt rationale\nmerged from: src.yaml",
        "merged={merged}"
    );
}
