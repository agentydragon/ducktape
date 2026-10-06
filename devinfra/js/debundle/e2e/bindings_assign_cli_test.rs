//! JS + YAML → editing CLI → edited YAML + emitted Node behavior.

use std::fs;

use debundle_e2e_support::{GraphFixture, write_text_file};
use serde_yaml::Value;

fn module(fixture: &GraphFixture, path: &str) -> Value {
    serde_yaml::from_slice(&fs::read(fixture.modules.join(path)).unwrap()).unwrap()
}

#[test]
fn list_filters_cover_members_and_source_match_bindings() {
    let fixture = GraphFixture::new(
        "const a = 1; const b = 2; const c = 3; console.log(a + b + c);",
        &[
            (
                "pair.yaml",
                "source_matches: [{match: 'const a = 1; const b = 2;', bindings: [a, {local: b, name: Beta}]}]",
            ),
            (
                "solo.yaml",
                "members: [{name: Solo, selector: {binding: {name: c}}}]",
            ),
        ],
    );
    let all = fixture.json(&["bindings", "list"]);
    assert_eq!(all["bindings"].as_array().unwrap().len(), 3);
    assert_eq!(all["bindings"][0]["minified"], "a");
    assert_eq!(all["bindings"][1]["name"], "Beta");
    assert_eq!(all["bindings"][0]["kind"], "minified");
    assert!(all["bindings"][0].get("name").is_none());
    assert_eq!(all["bindings"][1]["kind"], "readable");
    assert_eq!(all["bindings"][1]["minified"], "b");
    let orphan = fixture.json(&["bindings", "list", "--orphan"]);
    assert_eq!(orphan["bindings"].as_array().unwrap().len(), 1);
    assert_eq!(orphan["bindings"][0]["minified"], "c");
    let unrenamed = fixture.json(&["bindings", "list", "--unrenamed"]);
    assert_eq!(unrenamed["bindings"].as_array().unwrap().len(), 1);
    assert_eq!(unrenamed["bindings"][0]["minified"], "a");
    assert_eq!(
        fixture.json(&["bindings", "list", "--in", "pair"])["bindings"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn rename_members_and_source_match_shorthand_rekeys_annotations() {
    for selector in [
        "members: [{selector: {binding: {name: a}}}]",
        "source_matches: [{match: 'const a = 1;', bindings: [a]}]",
    ] {
        let yaml = format!("{selector}\nannotations: {{a: {{note: stable selector debt}}}}\n");
        let fixture = GraphFixture::new("const a = 1; console.log(a);", &[("m.yaml", &yaml)]);
        let file = fixture.modules.join("m.yaml");
        let before = fs::read(&file).unwrap();
        let dry = fixture.json(&["bindings", "rename", "a", "Readable", "--dry-run"]);
        assert_eq!(dry["action"], "dry-run");
        assert_eq!(fs::read(&file).unwrap(), before);
        let applied = fixture.json(&["bindings", "rename", "a", "Readable"]);
        assert_eq!(applied["action"], "applied");
        assert_eq!(applied["new_readable"], "Readable");
        let renamed = fs::read(&file).unwrap();
        assert_eq!(
            fixture.json(&["bindings", "rename", "a", "Readable"])["action"],
            "unchanged"
        );
        assert_eq!(fs::read(&file).unwrap(), renamed);
        let doc = module(&fixture, "m.yaml");
        if selector.starts_with("members") {
            assert_eq!(doc["members"][0]["name"], "Readable");
        } else {
            assert_eq!(doc["source_matches"][0]["bindings"][0]["local"], "a");
            assert_eq!(doc["source_matches"][0]["bindings"][0]["name"], "Readable");
        }
        assert!(doc["annotations"]["a"].is_null());
        assert_eq!(
            doc["annotations"]["Readable"]["note"],
            "stable selector debt"
        );
        fixture.assert_runs("1\n");
    }
}

#[test]
fn source_match_groups_cannot_be_split_by_assign_or_unassign() {
    let fixture = GraphFixture::new(
        "const a = 1; console.log(a);",
        &[(
            "m.yaml",
            "source_matches: [{match: 'const a = 1;', bindings: [a]}]",
        )],
    );
    for (verb, operand) in [("assign", "a:dest"), ("unassign", "a")] {
        fixture.assert_rejected_unchanged(
            &["bindings", verb, operand],
            &["does not yet support", "source_matches[0].bindings[0]"],
        );
    }
}

#[test]
fn rename_and_assign_refuse_collisions_with_all_binding_forms() {
    for (occupied, location) in [
        ("members: [{selector: {binding: {name: b}}}]", "members[0]"),
        (
            "members: [{name: b, selector: {binding: {name: c}}}]",
            "members[0]",
        ),
        (
            "source_matches: [{match: 'const c = 2;', bindings: [{local: c, name: b}]}]",
            "source_matches[0].bindings[0]",
        ),
    ] {
        let source = if occupied.contains("name: c") || occupied.contains("local: c") {
            "const a = 1; const c = 2; console.log(a + c);"
        } else {
            "const a = 1; const b = 2; console.log(a + b);"
        };
        let fixture = GraphFixture::new(
            source,
            &[
                ("src.yaml", "members: [{selector: {binding: {name: a}}}]"),
                ("occupied.yaml", occupied),
            ],
        );
        for args in [
            vec!["bindings", "rename", "a", "b"],
            vec!["bindings", "assign", "a:dest:b"],
        ] {
            fixture.assert_rejected_unchanged(&args, &["name collision", location]);
        }
    }
}

#[test]
fn positional_and_json_batches_create_one_canonical_destination() {
    for batch_json in [false, true] {
        let fixture = GraphFixture::new(
            "const a = 1; const b = 2; console.log(a + b);",
            &[
                ("src/a.yaml", "members: [{selector: {binding: {name: a}}}]"),
                ("src/b.yaml", "members: [{selector: {binding: {name: b}}}]"),
            ],
        );
        let batch = fixture.graph.with_file_name("moves.json");
        write_text_file(
            &batch,
            r#"[{"sym":"a","module":"UI/Widgets","readable":"Alpha"},{"sym":"b","module":"ui/widgets"}]"#,
        );
        let args = if batch_json {
            vec!["bindings", "assign", "--batch", batch.to_str().unwrap()]
        } else {
            vec!["bindings", "assign", "a:UI/Widgets:Alpha", "b:ui/widgets"]
        };
        let before: Vec<_> = ["src/a.yaml", "src/b.yaml"]
            .into_iter()
            .map(|path| (path, fs::read(fixture.modules.join(path)).unwrap()))
            .collect();
        let mut preview = args.clone();
        preview.push("--dry-run");
        assert_eq!(fixture.json(&preview)["action"], "dry-run");
        for (path, bytes) in before {
            assert_eq!(fs::read(fixture.modules.join(path)).unwrap(), bytes);
        }
        assert!(!fixture.modules.join("ui").exists());
        assert_eq!(fixture.json(&args)["moves_applied"], 2);
        for path in ["src/a.yaml", "src/b.yaml", "UI"] {
            assert!(!fixture.modules.join(path).exists());
        }
        let doc = module(&fixture, "ui/widgets.yaml");
        let members = doc["members"].as_sequence().unwrap();
        assert_eq!(members.len(), 2);
        assert!(
            members
                .iter()
                .any(|m| m["selector"]["binding"]["name"] == "a" && m["name"] == "Alpha")
        );
        assert!(
            members
                .iter()
                .any(|m| m["selector"]["binding"]["name"] == "b")
        );
        fixture.assert_runs("3\n");
    }
}

#[test]
fn move_and_unassign_annotations_preserve_only_intentionally_retained_modules() {
    for comment in ["", "comment: keepalive\n"] {
        let yaml = format!(
            "{comment}members: [{{selector: {{binding: {{name: a}}}}}}]\nannotations: {{a: {{note: selector debt}}}}\n"
        );
        let fixture = GraphFixture::new(
            "const a = 1; console.log(a);",
            &[("src.yaml", &yaml), ("unrelated.yaml", "members: []")],
        );
        fixture.json(&["bindings", "assign", "a:dest:Readable"]);
        assert_eq!(
            fixture.modules.join("src.yaml").exists(),
            !comment.is_empty()
        );
        assert!(fixture.modules.join("unrelated.yaml").exists());
        let doc = module(&fixture, "dest.yaml");
        assert!(doc["annotations"]["a"].is_null());
        assert_eq!(doc["annotations"]["Readable"]["note"], "selector debt");
        fixture.json(&["bindings", "unassign", "Readable"]);
        assert!(
            !fixture.modules.join("dest.yaml").exists(),
            "removed annotation must not keep drained module alive"
        );
        assert!(fixture.modules.join("unrelated.yaml").exists());
        fixture.assert_runs("1\n");
    }
}

#[test]
fn positional_readable_name_cannot_contain_a_colon() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_rejected_unchanged(&["bindings", "assign", "alpha:dest:Bad:Name"], &[":"]);
}

#[test]
fn batch_extraction_keeps_unmoved_members_and_does_not_rewrite_default_only_modules() {
    let fixture = GraphFixture::new(
        "const a = 1; const b = 2; const c = 3; const d = 4; const e = 5; console.log(a + b + c + d + e);",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}, {selector: {binding: {name: c}}}, {selector: {binding: {name: d}}}]",
            ),
            ("dest.yaml", "members: [{selector: {binding: {name: e}}}]"),
            (
                "empty.yaml",
                "# untouched default-only module\nmembers: []\nannotations: {}\n",
            ),
        ],
    );
    let empty = fixture.modules.join("empty.yaml");
    let before = fs::read(&empty).unwrap();
    fixture.json(&["bindings", "assign", "a:dest", "c:dest"]);
    for (path, names) in [
        ("home.yaml", vec!["b", "d"]),
        ("dest.yaml", vec!["e", "a", "c"]),
    ] {
        let doc = module(&fixture, path);
        let members = doc["members"].as_sequence().unwrap();
        assert_eq!(members.len(), names.len());
        for (member, name) in members.iter().zip(names) {
            assert_eq!(member["selector"]["binding"]["name"], name);
        }
    }
    fixture.json(&["bindings", "unassign", "b"]);
    let doc = module(&fixture, "home.yaml");
    assert_eq!(doc["members"].as_sequence().unwrap().len(), 1);
    assert_eq!(doc["members"][0]["selector"]["binding"]["name"], "d");
    assert_eq!(fs::read(&empty).unwrap(), before);
    fixture.assert_runs("15\n");
}

#[test]
fn rename_no_verify_explicitly_bypasses_collision_checks() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_rejected_unchanged(
        &["bindings", "rename", "alpha", "beta"],
        &["name collision"],
    );
    let report = fixture.json(&["bindings", "rename", "alpha", "beta", "--no-verify"]);
    assert_eq!(report["new_readable"], "beta");
    assert_eq!(report["gate"], "skipped");
    assert_eq!(module(&fixture, "a.yaml")["members"][0]["name"], "beta");
    // Restore a valid spec before verifying emitted behavior: the bypass is
    // permission to write a conflicting spec, not a promise it will compile.
    fixture.json(&["bindings", "rename", "alpha", "Alpha"]);
    fixture.assert_runs("2\n");
}

#[test]
fn automatic_cleanup_preserves_module_notes_but_explicit_delete_can_remove_them() {
    for edit in [
        vec!["bindings", "assign", "a:dest"],
        vec!["bindings", "unassign", "a"],
    ] {
        for note in ["keep selector investigation", ""] {
            let yaml =
                format!("note: {note:?}\nmembers: [{{selector: {{binding: {{name: a}}}}}}]\n");
            let fixture = GraphFixture::new("const a = 1; console.log(a);", &[("src.yaml", &yaml)]);
            let mut preview = edit.clone();
            preview.push("--dry-run");
            let report = fixture.json(&preview);
            assert!(report["files_deleted"].as_array().unwrap().is_empty());
            assert_eq!(
                fs::read_to_string(fixture.modules.join("src.yaml")).unwrap(),
                yaml
            );
            assert!(!fixture.modules.join("dest.yaml").exists());
            let report = fixture.json(&edit);
            assert!(report["files_deleted"].as_array().unwrap().is_empty());
            assert_eq!(module(&fixture, "src.yaml")["note"], note);
            let empty = fixture.json(&["modules", "list", "--empty"]);
            assert!(
                empty["modules"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|m| m["path"] == "src")
            );
            let sweep = fixture.json(&["modules", "list", "--auto-deletable"]);
            assert!(sweep["modules"].as_array().unwrap().is_empty());
            fixture.assert_runs("1\n");
            fixture.assert_success(&["modules", "delete", "src"]);
            assert!(!fixture.modules.join("src.yaml").exists());
            fixture.assert_runs("1\n");
        }
    }
}
