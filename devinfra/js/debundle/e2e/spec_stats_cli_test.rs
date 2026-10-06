//! End-to-end exercise of `debundle spec stats` by shelling out to the
//! built binary against tiny modules-tree fixtures.

use debundle_e2e_support::{run_debundle, write_text_file};
use std::path::Path;

fn run_stats(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec!["spec", "stats", "--modules", modules.to_str().unwrap()];
    args.extend_from_slice(extra);
    let out = run_debundle(&args);
    assert!(
        out.status.success(),
        "non-zero exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

fn module_tree(files: &[(&str, &str)]) -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    for (path, yaml) in files {
        write_text_file(&root.path().join(path), yaml);
    }
    root
}

#[test]
fn one_module_one_binding_emits_expected_totals() {
    let modules = module_tree(&[(
        "solo.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    )]);

    let out = run_stats(modules.path(), &["--format", "json"]);
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
    let modules = module_tree(&[
        (
            "solo.yaml",
            "members:\n  - name: Solo\n    selector: { binding: { name: a } }\n",
        ),
        (
            "group.yaml",
            "members:\n\
         \x20\x20- selector: { binding: { name: b } }\n\
         \x20\x20- selector: { binding: { name: c } }\n\
         \x20\x20- selector: { binding: { name: d } }\n",
        ),
    ]);

    let out = run_stats(modules.path(), &["--format", "json"]);
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
fn source_match_bindings_count_toward_orphans_and_renames() {
    let modules = module_tree(&[
        (
            "solo.yaml",
            "source_matches: [{match: 'const a = 1;', bindings: [{local: a, name: Alpha}]}]\n",
        ),
        (
            "pair.yaml",
            "source_matches: [{match: 'const b = 2; const c = 3;', bindings: [b, c]}]\n",
        ),
    ]);

    let out = run_stats(modules.path(), &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["modules"]["member_count"]["singletons"], 1);
    assert_eq!(parsed["modules"]["member_count"]["tiny_2_to_5"], 1);
    assert_eq!(parsed["bindings"]["total"], 3);
    assert_eq!(parsed["bindings"]["renamed"], 1);
    assert_eq!(parsed["bindings"]["unrenamed"], 2);
    // Only `Alpha` is an orphan (the sole binding of `solo`).
    assert_eq!(parsed["bindings"]["orphan"], 1);
}

#[test]
fn text_format_emits_non_empty_human_output() {
    let modules = module_tree(&[(
        "solo.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    )]);

    let out = run_stats(modules.path(), &["--format", "text"]);
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert!(!stdout.trim().is_empty(), "text output is empty");
    assert!(
        serde_json::from_str::<serde_json::Value>(&stdout).is_err(),
        "`--format text` printed JSON: {stdout}"
    );
}

#[test]
fn ndjson_emits_one_line_per_section() {
    let modules = module_tree(&[(
        "a.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    )]);

    let out = run_stats(modules.path(), &["--format", "ndjson"]);
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
    let modules = module_tree(&[(
        "ui/sidebar.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    )]);
    write_text_file(
        &modules.path().join("residual/unhandled.yaml"),
        "members: []\n",
    );

    let out = run_stats(modules.path(), &["--format", "json"]);
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["modules"]["total"], 2);
    assert_eq!(parsed["modules"]["residual"], 1);
    assert_eq!(parsed["modules"]["empty"], 1);
}
