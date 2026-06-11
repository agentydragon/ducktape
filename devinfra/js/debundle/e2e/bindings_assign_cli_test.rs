//! E2e for `debundle bindings list`, `bindings rename`, and the
//! large `bindings assign` (positional + --batch). Calls the library
//! entry point directly so no debundle binary is required.

use std::fs;
use std::path::Path;

use debundle_cli::binding::{
    BindingsListFilters, Move, parse_batch_json, parse_move_triple, rename_binding,
    run_bindings_assign, run_bindings_list,
};
use serde_yaml::Value;
use tempfile::TempDir;

fn write(root: &Path, rel: &str, body: &str) {
    let p = root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, body).unwrap();
}

fn read(root: &Path, rel: &str) -> String {
    fs::read_to_string(root.join(rel)).unwrap()
}

#[test]
fn list_filters_unrenamed_and_orphan() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "a.yaml",
        "members:\n  - selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
    );
    write(
        root,
        "solo.yaml",
        "members:\n  - name: Solo\n    selector: { binding: { name: c } }\n",
    );
    let all = run_bindings_list(root, &BindingsListFilters::default()).unwrap();
    assert_eq!(all.bindings.len(), 3);
    let orphans = run_bindings_list(
        root,
        &BindingsListFilters {
            orphan: true,
            ..Default::default()
        },
    )
    .unwrap();
    assert_eq!(orphans.bindings.len(), 1);
    assert_eq!(orphans.bindings[0].name.minified(), "c");
    let unrenamed = run_bindings_list(
        root,
        &BindingsListFilters {
            unrenamed: true,
            ..Default::default()
        },
    )
    .unwrap();
    assert_eq!(unrenamed.bindings.len(), 2);
}

#[test]
fn rename_round_trip_validates_then_writes() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "m.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );
    let out = rename_binding(root, "XOe", "PluginSettings", false, false).unwrap();
    assert_eq!(out.action, "renamed");
    let body = read(root, "m.yaml");
    let doc: Value = serde_yaml::from_str(&body).unwrap();
    assert_eq!(doc["members"][0]["name"].as_str(), Some("PluginSettings"));
}

#[test]
fn rename_dry_run_skips_write() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let body0 = "members:\n  - selector: { binding: { name: XOe } }\n";
    write(root, "m.yaml", body0);
    let out = rename_binding(root, "XOe", "PluginSettings", true, false).unwrap();
    assert_eq!(out.action, "dry-run");
    assert_eq!(read(root, "m.yaml"), body0);
}

#[test]
fn parse_move_triple_matches_doc_examples() {
    let two = parse_move_triple("XOe:runtime/plugins").unwrap();
    assert_eq!(two.module, "runtime/plugins");
    assert_eq!(two.readable, None);
    let three = parse_move_triple("XOe:runtime/plugins:PluginSettings").unwrap();
    assert_eq!(three.readable.as_deref(), Some("PluginSettings"));
}

#[test]
fn parse_batch_json_round_trip() {
    let moves =
        parse_batch_json(r#"[{"sym":"a","module":"x"},{"sym":"b","module":"y","readable":"B"}]"#)
            .unwrap();
    assert_eq!(moves.len(), 2);
    assert_eq!(moves[1].readable.as_deref(), Some("B"));
}

#[test]
fn assign_atomic_batch_creates_destinations_and_drains_sources() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src/old1.yaml",
        "members:\n  - selector: { binding: { name: AOe } }\n",
    );
    write(
        root,
        "src/old2.yaml",
        "members:\n  - selector: { binding: { name: BOe } }\n",
    );
    let moves = vec![
        Move {
            sym: "AOe".into(),
            module: "ui/widgets".into(),
            readable: Some("ButtonRegistry".into()),
        },
        Move {
            sym: "BOe".into(),
            module: "ui/widgets".into(),
            readable: None,
        },
    ];
    let out = run_bindings_assign(root, moves, false, false, None, None).unwrap();
    assert_eq!(out.moves_applied, 2);
    assert!(root.join("ui/widgets.yaml").exists());
    assert!(!root.join("src/old1.yaml").exists());
    assert!(!root.join("src/old2.yaml").exists());
    let dest = read(root, "ui/widgets.yaml");
    let doc: Value = serde_yaml::from_str(&dest).unwrap();
    let names: Vec<&str> = doc["members"]
        .as_sequence()
        .unwrap()
        .iter()
        .map(|m| m["selector"]["binding"]["name"].as_str().unwrap())
        .collect();
    assert!(names.contains(&"AOe") && names.contains(&"BOe"));
    let readables: Vec<Option<&str>> = doc["members"]
        .as_sequence()
        .unwrap()
        .iter()
        .map(|m| m["name"].as_str())
        .collect();
    assert!(readables.contains(&Some("ButtonRegistry")));
}

#[test]
fn assign_keeps_source_with_module_comment() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "kept.yaml",
        "comment: keepalive\nmembers:\n  - selector: { binding: { name: XOe } }\n",
    );
    let moves = vec![Move {
        sym: "XOe".into(),
        module: "elsewhere".into(),
        readable: None,
    }];
    run_bindings_assign(root, moves, false, false, None, None).unwrap();
    assert!(
        root.join("kept.yaml").exists(),
        "module-level comment should preserve drained source"
    );
}

#[test]
fn assign_rename_collision_rejects() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );
    write(
        root,
        "dest.yaml",
        "members:\n  - name: Existing\n    selector: { binding: { name: YOe } }\n",
    );
    let moves = vec![Move {
        sym: "XOe".into(),
        module: "dest".into(),
        readable: Some("Existing".into()),
    }];
    let err = run_bindings_assign(root, moves, false, false, None, None).unwrap_err();
    assert!(
        format!("{err}").contains("name collision"),
        "expected collision error: {err}"
    );
    // No files written.
    let src_body = read(root, "src.yaml");
    assert!(src_body.contains("XOe"));
}
