//! Real JS/spec → merge CLI → rewritten spec → emitted Node behavior.

use std::fs;

use debundle_e2e_support::{GraphFixture, write_text_file};
use serde_yaml::Value;

#[test]
fn merge_batch_preserves_exact_filenames_claims_and_runtime() {
    let fixture = GraphFixture::new(
        "const a = 1; const b = 2; console.log(a + b);",
        &[
            (
                "old-a.yaml",
                "comment: source overview\nnote: source debt\nsource_matches: [{match: 'const a = 1;', bindings: [{local: a, name: Alpha}]}]\nannotations: {Alpha: {note: binding debt}}",
            ),
            (
                "old-b.yaml",
                "members: [{name: Beta, selector: {binding: {name: b}}}]\nanonymous_statements: [{match: 'console.log(a + b);', note: startup output}]",
            ),
            (
                "models/Alpha.yaml",
                "comment: target overview\nnote: target debt\nmembers: []",
            ),
        ],
    );
    let batch = fixture.modules.join("merge.json");
    write_text_file(
        &batch,
        r#"[
          {"target":"models/Alpha.yaml","sources":["old-a.yaml"]},
          {"target":"models/Beta.yaml","sources":["old-b.yaml"]}
        ]"#,
    );
    let args = ["modules", "merge", "--batch", batch.to_str().unwrap()];
    let paths = ["old-a.yaml", "old-b.yaml", "models/Alpha.yaml"];
    let before: Vec<_> = paths
        .iter()
        .map(|path| fs::read(fixture.modules.join(path)).unwrap())
        .collect();
    let mut preview = args.to_vec();
    preview.push("--dry-run");
    assert_eq!(fixture.json(&preview)["gate"], "passed");
    for (path, bytes) in paths.iter().zip(before) {
        assert_eq!(fs::read(fixture.modules.join(path)).unwrap(), bytes);
    }
    assert!(!fixture.modules.join("models/Beta.yaml").exists());
    let outcome = fixture.json(&args);
    assert_eq!(outcome["files_written"].as_array().unwrap().len(), 2);
    assert_eq!(outcome["files_deleted"].as_array().unwrap().len(), 2);
    assert_eq!(outcome["targets"].as_array().unwrap().len(), 2);
    for path in [
        "old-a.yaml",
        "old-b.yaml",
        "models/alpha.yaml",
        "models/beta.yaml",
    ] {
        assert!(!fixture.modules.join(path).exists(), "{path}");
    }
    let alpha: Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("models/Alpha.yaml")).unwrap())
            .unwrap();
    assert_eq!(alpha["source_matches"][0]["bindings"][0]["name"], "Alpha");
    assert_eq!(alpha["annotations"]["Alpha"]["note"], "binding debt");
    assert_eq!(
        alpha["comment"],
        "target overview\n--- from old-a.yaml:\nsource overview"
    );
    assert_eq!(
        alpha["note"],
        "target debt\n--- from old-a.yaml:\nsource debt\nmerged from: old-a.yaml"
    );
    let beta: Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("models/Beta.yaml")).unwrap())
            .unwrap();
    assert_eq!(beta["members"][0]["name"], "Beta");
    assert_eq!(beta["anonymous_statements"][0]["note"], "startup output");
    fixture.assert_runs("3\n");
}

#[test]
fn merge_batch_gate_failure_leaves_every_target_and_source_unchanged() {
    let fixture = GraphFixture::dependency_chain();
    let batch = fixture.modules.join("merge.json");
    write_text_file(
        &batch,
        r#"[
      {"target":"models/Middle.yaml","sources":["c.yaml"]},
      {"target":"a.yaml","sources":["b.yaml"]}
    ]"#,
    );
    fixture.assert_rejected_unchanged(
        &["modules", "merge", "--batch", batch.to_str().unwrap()],
        &["unrealizable"],
    );
    fixture.assert_runs("3\n");
}

#[test]
fn merge_batch_rejects_ambiguous_plans_without_writes() {
    let fixture = GraphFixture::acyclic_pair();
    let batch = fixture.modules.join("merge.json");
    let absolute_source = fixture.modules.join("a.yaml");
    let alias_batch = serde_json::json!([
        {"target":"models/Alpha.yaml","sources":["a"]},
        {"target":"models/Beta.yaml","sources":[absolute_source]},
    ])
    .to_string();
    for (plan, diagnostic) in [
        ("[]", "merge batch is empty"),
        (r#"[{"target":"New.yaml","sources":[]}]"#, "has no sources"),
        (
            r#"[{"target":"models/State.json","sources":["a"]}]"#,
            "module path must end in .yaml",
        ),
        (
            r#"[{"target":"New.yaml","sources":["merge.json"]}]"#,
            "module path must end in .yaml",
        ),
        (
            r#"[{"target":"New.yaml","sources":["a"]},{"target":"New.yaml","sources":["b"]}]"#,
            "duplicate merge batch target",
        ),
        (
            r#"[{"target":"New.yaml","sources":["a","a.yaml"]}]"#,
            "reused merge batch source",
        ),
        (alias_batch.as_str(), "reused merge batch source"),
        (
            r#"[{"target":"a.yaml","sources":["a"]}]"#,
            "source-target overlap",
        ),
        (
            r#"[{"target":"a.yaml","sources":["b"]},{"target":"New.yaml","sources":["a"]}]"#,
            "source-target overlap",
        ),
        (
            r#"[{"target":"New.yaml","sources":["a"]},{"target":"new.yaml","sources":["b"]}]"#,
            "logical destination collision",
        ),
        (
            r#"[{"target":"B.yaml","sources":["a"]}]"#,
            "logical destination collision",
        ),
        (
            r#"[{"target":"B.yaml","sources":["a"]},{"target":"New.yaml","sources":["b"]}]"#,
            "logical source-target overlap",
        ),
        (
            r#"[{"target":"New.yaml","sources":["a"],"extra":true}]"#,
            "unknown field",
        ),
    ] {
        eprintln!("merge batch under test: {plan}");
        write_text_file(&batch, plan);
        fixture.assert_rejected_unchanged(
            &["modules", "merge", "--batch", batch.to_str().unwrap()],
            &[diagnostic],
        );
    }
    fixture.assert_runs("2\n");
}

#[test]
fn merge_batch_allows_case_only_relocation() {
    let fixture = GraphFixture::acyclic_pair();
    let batch = fixture.modules.join("merge.json");
    write_text_file(
        &batch,
        r#"[
      {"target":"A.yaml","sources":["a.yaml"]},
      {"target":"models/Beta.yaml","sources":["b.yaml"]}
    ]"#,
    );
    fixture.assert_success(&["modules", "merge", "--batch", batch.to_str().unwrap()]);
    assert!(fixture.modules.join("A.yaml").exists());
    assert!(!fixture.modules.join("a.yaml").exists());
    fixture.assert_runs("2\n");
}

#[test]
fn merge_batch_rejects_untouched_symlink_aliases_without_writes() {
    for (alias, target) in [("alias.yaml", "target.yaml"), ("alias-dir", "nested")] {
        let fixture = GraphFixture::acyclic_pair();
        write_text_file(&fixture.modules.join("target.yaml"), "members: []");
        write_text_file(&fixture.modules.join("nested/empty.yaml"), "members: []");
        std::os::unix::fs::symlink(target, fixture.modules.join(alias)).unwrap();
        let batch = fixture.modules.join("merge.json");
        let destination = if alias == "alias.yaml" {
            "alias.yaml"
        } else {
            "New.yaml"
        };
        write_text_file(
            &batch,
            &serde_json::json!([
                {"target":destination,"sources":["a"]}
            ])
            .to_string(),
        );
        fixture.assert_rejected_unchanged(
            &["modules", "merge", "--batch", batch.to_str().unwrap()],
            &["graph compatibility mismatch"],
        );
        fixture.refresh_graph();
        fixture.assert_rejected_unchanged(
            &["modules", "merge", "--batch", batch.to_str().unwrap()],
            &["does not support symlink entries"],
        );
        fixture.assert_runs("2\n");
    }
}

#[test]
fn merge_batch_and_single_target_arguments_are_exclusive() {
    let fixture = GraphFixture::acyclic_pair();
    let batch = fixture.modules.join("merge.json");
    write_text_file(&batch, r#"[{"target":"New.yaml","sources":["a"]}]"#);
    for arguments in [
        vec!["--batch", batch.to_str().unwrap(), "--target", "New", "a"],
        vec!["--batch", batch.to_str().unwrap(), "a"],
        vec!["--batch", batch.to_str().unwrap(), "--no-verify"],
        vec!["--target", "New"],
        vec!["a"],
        vec![],
    ] {
        let mut args = vec!["modules", "merge"];
        args.extend(arguments);
        fixture.assert_rejected_unchanged(&args, &["error:"]);
    }
    fixture.assert_runs("2\n");
}

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
                    "comment: source overview\nnote: source debt\nmembers: [{selector: {binding: {name: b}}}]\nanonymous_statements: [{match: 'console.log(\"second\");'}]",
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
        assert_eq!(note.matches("source debt").count(), 1);
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
    // The refusal names the clashing name and the source file whose claims clash.
    for (target, first, second, diagnostics) in [
        (
            "members: [{selector: {binding: {name: a}}}]",
            "members: [{name: Clash, selector: {binding: {name: b}}}]",
            "members: [{name: Clash, selector: {binding: {name: c}}}]",
            ["Clash", "second.yaml"],
        ),
        (
            "members: [{selector: {binding: {name: shared_binding}}}]",
            "members: [{selector: {binding: {name: shared_binding}}}]",
            "members: []",
            ["shared_binding", "first.yaml"],
        ),
        (
            "members: [{name: Widget, selector: {binding: {name: a}}}]",
            "source_matches: [{match: 'const b = 2;', bindings: [{local: b, name: Widget}]}]",
            "members: []",
            ["Widget", "first.yaml"],
        ),
        (
            "members: [{selector: {binding: {name: annotated}}}]\nannotations: {annotated: {note: first}}",
            "members: [{selector: {binding: {name: b}}}]\nannotations: {annotated: {note: second}}",
            "members: []",
            ["annotated", "first.yaml"],
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
        // The edited spec makes the saved graph stale. Verified edits reject
        // that pair first; an explicit edit without a graph must still refuse
        // conflicting documents rather than silently repair their claims.
        for (path, yaml) in [
            ("target.yaml", target),
            ("first.yaml", first),
            ("second.yaml", second),
        ] {
            write_text_file(&fixture.modules.join(path), yaml);
        }
        let mut args = vec!["modules", "merge", "--target", "target", "first", "second"];
        fixture.assert_rejected_unchanged(&args, &["graph compatibility mismatch"]);
        args.push("--no-verify");
        fixture.assert_rejected_unchanged_without_graph(&args, &diagnostics);
    }
}
