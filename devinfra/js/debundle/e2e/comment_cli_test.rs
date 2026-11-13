//! Real CLI comment workflows; editor environment belongs to the child, not the test process.

use debundle_e2e_graph_support::GraphFixture;
use std::fs;
use std::os::unix::fs::PermissionsExt;

use debundle_e2e_support::parse_stdout_json;
use serde_yaml::Value;

fn fixture() -> GraphFixture {
    GraphFixture::new(
        "const a = 1; console.log(a);",
        &[(
            "runtime/plugin.yaml",
            "members: [{selector: {binding: {name: a}}}]",
        )],
    )
}

#[test]
fn binding_and_module_comments_set_read_clear_and_dry_run() {
    for (namespace, locator) in [("bindings", "a"), ("modules", "runtime/plugin")] {
        let fixture = fixture();
        let file = fixture.modules.join("runtime/plugin.yaml");
        let before = fs::read(&file).unwrap();
        let dry = fixture.json(&[namespace, "comment", locator, "not written", "--dry-run"]);
        assert_eq!(dry["action"], "dry-run");
        assert_eq!(fs::read(&file).unwrap(), before);
        let set = fixture.json(&[namespace, "comment", locator, "plugin glue"]);
        assert_eq!(set["action"], "set");
        assert_eq!(set["comment"], "plugin glue");
        let read = fixture.json(&[namespace, "comment", locator]);
        assert_eq!(read["action"], "read");
        let key = if namespace == "bindings" {
            "binding"
        } else {
            "module"
        };
        assert_eq!(read[key], locator);
        assert!(read.get("sym").is_none());
        assert_eq!(read["comment"], "plugin glue");
        let saved = fs::read(&file).unwrap();
        assert_eq!(
            fixture.json(&[namespace, "comment", locator, "plugin glue"])["action"],
            "unchanged"
        );
        assert_eq!(fs::read(&file).unwrap(), saved);
        let doc: Value = serde_yaml::from_slice(&fs::read(&file).unwrap()).unwrap();
        let location = if namespace == "bindings" {
            &doc["annotations"]["a"]
        } else {
            &doc
        };
        assert_eq!(location["comment"], "plugin glue");
        fixture.assert_runs("1\n");
        fixture.json(&[namespace, "comment", locator, ""]);
        assert_eq!(
            fixture.json(&[namespace, "comment", locator])["comment"],
            ""
        );
        let clear = fixture.json(&[namespace, "comment", locator, "--clear"]);
        assert_eq!(clear["action"], "cleared");
        let doc: Value = serde_yaml::from_slice(&fs::read(&file).unwrap()).unwrap();
        let location = if namespace == "bindings" {
            &doc["annotations"]["a"]
        } else {
            &doc
        };
        assert!(location["comment"].is_null());
        assert!(fixture.json(&[namespace, "comment", locator])["comment"].is_null());
    }
}

#[test]
fn ambiguous_binding_comment_refuses_with_locations_without_writes() {
    let fixture = GraphFixture::new(
        "const a = 1; const b = 2; console.log(a + b);",
        &[
            ("a.yaml", "members: [{selector: {binding: {name: a}}}]"),
            (
                "b.yaml",
                "members: [{name: a, selector: {binding: {name: b}}}]",
            ),
        ],
    );
    fixture.assert_rejected_unchanged(
        &["bindings", "comment", "a", "must not stick"],
        &["ambiguous", "a.yaml", "b.yaml"],
    );
}

#[test]
fn edit_mode_replaces_or_clears_the_prepopulated_comment() {
    for (replacement, action) in [("from-fake-editor", "set"), ("", "cleared")] {
        let fixture = fixture();
        fixture.json(&["bindings", "comment", "a", "existing"]);
        let editor = fixture.graph.with_file_name("fake-editor.sh");
        // The editor must receive the existing text, not an empty buffer.
        fs::write(&editor, "#!/bin/sh\n[ \"$(cat \"$1\")\" = existing ] || exit 1\nprintf %s \"$REPLACEMENT\" > \"$1\"\n").unwrap();
        fs::set_permissions(&editor, fs::Permissions::from_mode(0o755)).unwrap();
        let out = fixture
            .process(&["bindings", "comment", "a", "--edit", "--format", "json"])
            .env("EDITOR", &editor)
            .env_remove("VISUAL")
            .env("REPLACEMENT", replacement)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(parse_stdout_json(&out)["action"], action);
        let read = fixture.json(&["bindings", "comment", "a"]);
        if replacement.is_empty() {
            assert!(read["comment"].is_null());
        } else {
            assert_eq!(read["comment"], replacement);
        }
        fixture.assert_runs("1\n");
    }
}

#[test]
fn binding_comments_use_readable_annotations_and_keep_other_metadata() {
    for claim in [
        "members: [{name: Alpha, selector: {binding: {name: a}}}]",
        "source_matches: [{match: 'const a = 1;', bindings: [{local: a, name: Alpha}]}]",
    ] {
        let yaml = format!("{claim}\nannotations: {{Alpha: {{note: selector debt}}}}");
        let fixture = GraphFixture::new("const a = 1; console.log(a);", &[("m.yaml", &yaml)]);
        fixture.json(&["bindings", "comment", "a", "emitted comment"]);
        assert_eq!(
            fixture.json(&["bindings", "comment", "Alpha"])["comment"],
            "emitted comment"
        );
        fixture.assert_runs("1\n");
        fixture.json(&["bindings", "comment", "Alpha", "--clear"]);
        let doc: Value =
            serde_yaml::from_slice(&fs::read(fixture.modules.join("m.yaml")).unwrap()).unwrap();
        assert!(doc["annotations"]["Alpha"]["comment"].is_null());
        assert_eq!(doc["annotations"]["Alpha"]["note"], "selector debt");
    }
}

#[test]
fn missing_comment_targets_are_errors_without_writes() {
    let fixture = fixture();
    for (namespace, diagnostic) in [
        ("bindings", "no binding named"),
        ("modules", "module YAML not found"),
    ] {
        fixture.assert_rejected_unchanged(&[namespace, "comment", "missing"], &[diagnostic]);
    }
}

#[test]
fn comment_edit_modes_are_mutually_exclusive_in_both_namespaces() {
    let fixture = fixture();
    for (namespace, locator) in [("bindings", "a"), ("modules", "runtime/plugin")] {
        for modes in [
            vec!["--edit", "--clear"],
            vec!["text", "--edit"],
            vec!["text", "--clear"],
        ] {
            let mut args = vec![namespace, "comment", locator];
            args.extend(modes);
            fixture.assert_rejected_unchanged(&args, &["cannot be used with"]);
        }
    }
}

#[test]
fn closed_stdout_does_not_replay_the_comment_editor_or_lose_the_written_comment() {
    use std::process::Stdio;
    let fixture = fixture();
    let counter = fixture.root().join("editor-invocations");
    let editor = fixture.root().join("counting-editor");
    fs::write(
        &editor,
        format!(
            "#!/bin/sh\nprintf 'called\\n' >> '{}'\nprintf 'written once' > \"$1\"\n",
            counter.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&editor, fs::Permissions::from_mode(0o755)).unwrap();
    let (reader, writer) = std::io::pipe().unwrap();
    drop(reader);
    let output = fixture
        .process(&[
            "modules",
            "comment",
            "runtime/plugin",
            "--edit",
            "--format",
            "json",
        ])
        .env("EDITOR", editor)
        .stdout(Stdio::from(writer))
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(fs::read_to_string(counter).unwrap().lines().count(), 1);
    let saved: Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("runtime/plugin.yaml")).unwrap())
            .unwrap();
    assert_eq!(saved["comment"], "written once");
}
