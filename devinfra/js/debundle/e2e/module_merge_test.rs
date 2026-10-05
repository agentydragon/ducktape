//! Real JS/spec → merge CLI → rewritten spec → emitted Node behavior.

use std::fs;

use debundle_e2e_support::{GraphFixture, write_text_file};
use serde_yaml::Value;

#[test]
fn merge_preserves_claims_metadata_and_runtime_with_or_without_target_comments() {
    for metadata in ["", "comment: target overview\nnote: existing debt\n"] {
        let target = format!(
            "{metadata}members: [{{selector: {{binding: {{name: a}}}}}}]\nanonymous_statements: [{{match: 'console.log(\"first\");'}}]"
        );
        let fixture = GraphFixture::new(
            "const a = 1; const b = 2; const c = 3; const d = 4; console.log(\"first\"); console.log(\"second\"); console.log(a + b + c + d);",
            &[
                ("ui/target.yaml", &target),
                (
                    "ui/src1.yaml",
                    "comment: source overview\nmembers: [{selector: {binding: {name: b}}}]\nanonymous_statements: [{match: 'console.log(\"second\");'}]",
                ),
                (
                    "ui/src2.yaml",
                    "members: [{selector: {binding: {name: c}}}]",
                ),
                (
                    "ui/claims.yaml",
                    "members: []\nsource_matches: [{match: 'const d = 4;', bindings: [{local: d, name: Delta}]}]\nannotations: {Delta: {note: binding debt}}",
                ),
                ("ui/empty.yaml", "members: []"),
            ],
        );
        let sibling = fixture.modules.join("ui/target.yaml.tmp");
        write_text_file(&sibling, "unrelated data\n");
        let paths = [
            "ui/target.yaml",
            "ui/src1.yaml",
            "ui/src2.yaml",
            "ui/claims.yaml",
            "ui/empty.yaml",
        ];
        let before: Vec<_> = paths
            .iter()
            .map(|p| fs::read(fixture.modules.join(p)).unwrap())
            .collect();
        // Both suffix forms work, including nested paths. Preview never rewrites
        // the target, deletes sources, or touches someone else's scratch file.
        let args = [
            "modules",
            "merge",
            "--target",
            "ui/target",
            "ui/src1",
            "ui/src2.yaml",
            "ui/claims",
            "ui/empty",
            "--format",
            "text",
        ];
        let mut preview = args.to_vec();
        preview.push("--dry-run");
        let out = fixture.command(&preview);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        for (path, bytes) in paths.iter().zip(before) {
            assert_eq!(fs::read(fixture.modules.join(path)).unwrap(), bytes);
        }
        let out = fixture.command(&args);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let doc: Value =
            serde_yaml::from_slice(&fs::read(fixture.modules.join("ui/target.yaml")).unwrap())
                .unwrap();
        let names: Vec<_> = doc["members"]
            .as_sequence()
            .unwrap()
            .iter()
            .map(|m| m["selector"]["binding"]["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["a", "b", "c"]);
        assert_eq!(doc["source_matches"].as_sequence().unwrap().len(), 1);
        assert_eq!(doc["source_matches"][0]["bindings"][0]["name"], "Delta");
        assert_eq!(doc["annotations"]["Delta"]["note"], "binding debt");
        let statements: Vec<_> = doc["anonymous_statements"]
            .as_sequence()
            .unwrap()
            .iter()
            .map(|s| s["match"].as_str().unwrap())
            .collect();
        assert_eq!(
            statements,
            ["console.log(\"first\");", "console.log(\"second\");"]
        );
        // Provenance names every merged source, even the empty one, after any
        // existing note.
        let note = doc["note"].as_str().unwrap();
        for source in [
            "ui/src1.yaml",
            "ui/src2.yaml",
            "ui/claims.yaml",
            "ui/empty.yaml",
        ] {
            assert!(note.contains(source), "{note}");
        }
        assert_eq!(note.contains("existing debt"), !metadata.is_empty());
        assert!(
            note.find("existing debt") < note.find("ui/src1.yaml"),
            "{note}"
        );
        // Source comments are attributed to their source, after the target's own.
        let comment = doc["comment"].as_str().unwrap();
        assert!(comment.contains("ui/src1.yaml"), "{comment}");
        let target_comment = comment.find("target overview");
        assert_eq!(target_comment.is_some(), !metadata.is_empty());
        assert!(
            comment.find("source overview") > target_comment,
            "{comment}"
        );
        assert!(!comment.contains("src2.yaml") && !comment.contains("empty.yaml"));
        assert_eq!(fs::read_to_string(sibling).unwrap(), "unrelated data\n");
        // Only the target and the preexisting scratch file remain: no leaked
        // replacement tempfile, and even the empty source records provenance.
        assert_eq!(fs::read_dir(fixture.modules.join("ui")).unwrap().count(), 2);
        fixture.assert_runs("first\nsecond\n10\n");
    }
}

#[test]
fn merge_preserves_anonymous_only_modules_and_runtime_order() {
    let fixture = GraphFixture::new(
        "console.log(\"first\"); console.log(\"second\");",
        &[
            (
                "target.yaml",
                "members: []\nanonymous_statements: [{match: 'console.log(\"first\");'}]",
            ),
            (
                "source.yaml",
                "members: []\nanonymous_statements: [{match: 'console.log(\"second\");'}]",
            ),
        ],
    );
    fixture.assert_success(&["modules", "merge", "--target", "target", "source"]);
    assert!(!fixture.modules.join("source.yaml").exists());
    let doc: Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("target.yaml")).unwrap()).unwrap();
    let statements: Vec<_> = doc["anonymous_statements"]
        .as_sequence()
        .unwrap()
        .iter()
        .map(|s| s["match"].as_str().unwrap())
        .collect();
    assert_eq!(
        statements,
        ["console.log(\"first\");", "console.log(\"second\");"]
    );
    fixture.assert_runs("first\nsecond\n");
}

#[test]
fn merge_rejects_document_conflicts_before_writing_in_both_modes() {
    for (target, first, second, diagnostic) in [
        (
            "members: [{selector: {binding: {name: a}}}]",
            "members: [{name: Clash, selector: {binding: {name: b}}}]",
            "members: [{name: Clash, selector: {binding: {name: c}}}]",
            "duplicate member name \"Clash\"",
        ),
        (
            "members: [{selector: {binding: {name: a}}}]",
            "members: [{selector: {binding: {name: a}}}]",
            "members: []",
            "duplicate member name \"a\"",
        ),
        (
            "members: [{name: Widget, selector: {binding: {name: a}}}]",
            "source_matches: [{match: 'const b = 2;', bindings: [{local: b, name: Widget}]}]",
            "members: []",
            "duplicate member name \"Widget\"",
        ),
        (
            "members: [{selector: {binding: {name: a}}}]\nannotations: {a: {note: first}}",
            "members: [{selector: {binding: {name: b}}}]\nannotations: {a: {note: second}}",
            "members: []",
            "conflicting annotation",
        ),
    ] {
        let fixture = GraphFixture::new(
            "const a = 1; const b = 2; const c = 3; console.log(a + b + c);",
            &[
                ("target.yaml", "members: [{selector: {binding: {name: a}}}]"),
                ("first.yaml", "members: [{selector: {binding: {name: b}}}]"),
                ("second.yaml", "members: [{selector: {binding: {name: c}}}]"),
            ],
        );
        // Start with real graph artifacts, then model an author's conflicting
        // edits. Invalid claims must not be silently repaired by a merge.
        for (path, yaml) in [
            ("target.yaml", target),
            ("first.yaml", first),
            ("second.yaml", second),
        ] {
            write_text_file(&fixture.modules.join(path), yaml);
        }
        for no_verify in [false, true] {
            let mut args = vec!["modules", "merge", "--target", "target", "first", "second"];
            if no_verify {
                args.push("--no-verify");
            }
            fixture.assert_rejected_unchanged(&args, &[diagnostic]);
        }
    }
}
