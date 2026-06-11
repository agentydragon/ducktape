//! E2e for `debundle bindings list`, `bindings rename`, and the
//! large `bindings assign` (positional + --batch). Calls the library
//! entry point directly so no debundle binary is required.

use std::fs;
use std::path::Path;

use debundle_cli::binding::{
    BindingsListFilters, Move, parse_batch_json, parse_move_triple, rename_binding,
    run_bindings_assign, run_bindings_list, run_bindings_unassign,
};
use debundle_cli::edit_gate::Gate;
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
    assert_eq!(out.outcome.action, "applied");
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
    assert_eq!(out.outcome.action, "dry-run");
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
    let out = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
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
    run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
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
    let err = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap_err();
    assert!(
        format!("{err}").contains("name collision"),
        "expected collision error: {err}"
    );
    // No files written.
    let src_body = read(root, "src.yaml");
    assert!(src_body.contains("XOe"));
}

#[test]
fn assign_rename_collision_with_unrenamed_minified_name_rejects() {
    // A member without an explicit `name:` keeps its minified
    // binding name as its public identity — `bindings rename`
    // already treats that as a collision target; `bindings assign`
    // must use the same predicate.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: a } }\n",
    );
    write(
        root,
        "other.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );
    let moves = vec![Move {
        sym: "a".into(),
        module: "dest".into(),
        readable: Some("XOe".into()),
    }];
    let err = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap_err();
    assert!(
        format!("{err}").contains("name collision"),
        "expected collision with unrenamed minified XOe: {err}"
    );
    assert!(!root.join("dest.yaml").exists(), "no files written");
}

#[test]
fn assign_batch_with_both_spellings_of_one_member_moves_it_once() {
    // `<sym>` accepts the minified OR readable spelling. A batch
    // carrying both spellings of the SAME member must collapse to a
    // single move — not produce two plan entries for one
    // (file, index), where the second extraction pushes the null
    // sentinel into the destination as a literal `- null` member.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - name: PluginSettings\n    selector: { binding: { name: XOe } }\n",
    );
    let moves = vec![
        Move {
            sym: "XOe".into(),
            module: "dest".into(),
            readable: None,
        },
        Move {
            sym: "PluginSettings".into(),
            module: "dest".into(),
            readable: None,
        },
    ];
    let out = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
    assert_eq!(out.moves_applied, 1, "one member, one move");
    let dest = read(root, "dest.yaml");
    let doc: Value = serde_yaml::from_str(&dest).unwrap();
    let members = doc["members"].as_sequence().unwrap();
    assert_eq!(members.len(), 1, "exactly one member in dest: {dest}");
    assert!(
        members.iter().all(|m| !m.is_null()),
        "no null members may be spliced into the spec: {dest}"
    );
}

#[test]
fn assign_batch_with_contradictory_destinations_for_one_member_rejects() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - name: PluginSettings\n    selector: { binding: { name: XOe } }\n",
    );
    let pre = read(root, "src.yaml");
    let moves = vec![
        Move {
            sym: "XOe".into(),
            module: "dest_one".into(),
            readable: None,
        },
        Move {
            sym: "PluginSettings".into(),
            module: "dest_two".into(),
            readable: None,
        },
    ];
    let err = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap_err();
    let msg = format!("{err}");
    assert!(
        msg.contains("same member"),
        "expected contradictory-destination rejection: {msg}"
    );
    assert_eq!(read(root, "src.yaml"), pre, "spec untouched on rejection");
    assert!(!root.join("dest_one.yaml").exists());
    assert!(!root.join("dest_two.yaml").exists());
}

#[test]
fn assign_preserves_unrelated_empty_module() {
    // The drained-module sweep must only delete modules that were
    // sources of a move in THIS operation — a pre-existing empty
    // module shell is not this command's business.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );
    write(root, "unrelated_empty.yaml", "members: []\n");
    let moves = vec![Move {
        sym: "XOe".into(),
        module: "dest".into(),
        readable: None,
    }];
    run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
    assert!(
        root.join("unrelated_empty.yaml").exists(),
        "unrelated pre-existing empty module must survive an assign"
    );
    assert!(!root.join("src.yaml").exists(), "drained source is deleted");
}

#[test]
fn unassign_preserves_unrelated_empty_module() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: XOe } }\n",
    );
    write(root, "unrelated_empty.yaml", "members: []\n");
    run_bindings_unassign(root, vec!["XOe".into()], false, Gate::Skip).unwrap();
    assert!(
        root.join("unrelated_empty.yaml").exists(),
        "unrelated pre-existing empty module must survive an unassign"
    );
    assert!(!root.join("src.yaml").exists(), "drained source is deleted");
}

#[test]
fn assign_canonicalizes_destination_module_path_case() {
    // `ModulePath::parse` lowercases module identities; the assign
    // writer must resolve destinations through the same
    // canonicalization so `UI/Widgets` and `ui/widgets` cannot
    // become two distinct files.
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "src.yaml",
        "members:\n  - selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
    );
    let moves = vec![
        Move {
            sym: "a".into(),
            module: "UI/Widgets".into(),
            readable: None,
        },
        Move {
            sym: "b".into(),
            module: "ui/widgets".into(),
            readable: None,
        },
    ];
    run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
    assert!(
        root.join("ui/widgets.yaml").exists(),
        "canonical lowercase destination must exist"
    );
    assert!(
        !root.join("UI").exists(),
        "no separate case-variant directory may be created"
    );
    let doc: Value = serde_yaml::from_str(&read(root, "ui/widgets.yaml")).unwrap();
    assert_eq!(
        doc["members"].as_sequence().unwrap().len(),
        2,
        "both members land in the one canonical file"
    );
}

#[test]
fn parse_move_triple_rejects_colon_in_readable() {
    // `bindings rename` rejects `:` in names; the positional triple
    // parser must not silently accept it via splitn(3).
    assert!(parse_move_triple("XOe:runtime/plugins:Bad:Name").is_err());
}
