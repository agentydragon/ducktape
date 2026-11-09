//! Real JS/spec workflows for assignment validation and atomic edits.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, run_debundle, write_text_file};
use std::fs;

#[test]
fn graph_only_atom_split_cannot_be_written_without_full_assessment() {
    let fixture = GraphFixture::atomic_pair();
    let preview = fixture.json(&["bindings", "assign", "alpha:dogfood/split", "--dry-run"]);
    assert_eq!(preview["needs_full_assessment"], true);
    let before = fs::read(fixture.modules.join("home/atom.yaml")).unwrap();
    let output = fixture.command(&["bindings", "assign", "alpha:dogfood/split"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    assert_eq!(
        fs::read(fixture.modules.join("home/atom.yaml")).unwrap(),
        before
    );
}

#[test]
fn bindings_assign_and_unassign_require_graph_or_no_verify() {
    let fixture = GraphFixture::acyclic_pair();
    let before = fs::read(fixture.modules.join("a.yaml")).unwrap();
    for (verb, binding) in [("assign", "alpha:c"), ("unassign", "alpha")] {
        // Deliberately bypass GraphFixture::command, which supplies --graph.
        let out = run_debundle(&[
            "bindings",
            verb,
            "--modules",
            fixture.modules.to_str().unwrap(),
            binding,
        ]);
        assert!(!out.status.success());
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains("--graph") || stderr.contains("--no-verify"),
            "{stderr}"
        );
        assert_eq!(fs::read(fixture.modules.join("a.yaml")).unwrap(), before);
        assert!(!fixture.modules.join("c.yaml").exists());
    }
}

#[test]
fn graph_only_assignment_is_a_preview_and_cannot_be_written_as_verified() {
    let fixture = GraphFixture::acyclic_pair();
    let original = fs::read(fixture.modules.join("a.yaml")).unwrap();
    let preview = fixture.json(&["bindings", "assign", "--dry-run", "alpha:c"]);
    assert_eq!(preview["gate"], "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&["bindings", "assign", "alpha:c"]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    assert_eq!(fs::read(fixture.modules.join("a.yaml")).unwrap(), original);
    assert!(!fixture.modules.join("c.yaml").exists());
}

#[test]
fn bulk_edits_resolve_both_spellings_against_the_pre_edit_spec() {
    let fixture = GraphFixture::new_full_tree(
        "const alpha = 1; const beta = 2; console.log(alpha + beta);",
        &[(
            "home.yaml",
            "members: [{name: Alpha, selector: {binding: {name: alpha}}}, {name: Beta, selector: {binding: {name: beta}}}]",
        )],
    );
    fixture.assert_rejected_unchanged(
        &["bindings", "assign", "Alpha:first", "alpha:second"],
        &["contradictory destinations"],
    );
    let analysis = fixture.stage_analysis();
    let report = fixture.json_with_assessment(
        &[
            "bindings",
            "assign",
            "Alpha:dest:First",
            "alpha:dest:First",
            "Beta:dest:Second",
        ],
        &analysis,
    );
    assert_eq!(report["moves_applied"], 2);
    assert_eq!(report["gate"], "passed");
    assert!(!fixture.modules.join("home.yaml").exists());
    fixture.refresh_graph();
    let analysis = fixture.stage_analysis();
    let report = fixture.json_with_assessment(
        &["bindings", "unassign", "First", "alpha", "Second"],
        &analysis,
    );
    assert_eq!(report["unassigned"], 2);
    assert_eq!(report["gate"], "passed");
    assert!(!fixture.modules.join("dest.yaml").exists());
    fixture.assert_runs("3\n");
}

#[test]
fn empty_assign_batch_does_not_parse_or_change_the_tree() {
    let fixture = GraphFixture::acyclic_pair();
    let batch = fixture.graph.with_file_name("empty-batch.json");
    write_text_file(&batch, "[]");
    let broken = fixture.modules.join("broken.yaml");
    write_text_file(&broken, "[invalid yaml");
    fixture.assert_rejected_unchanged(
        &["bindings", "assign", "--batch", batch.to_str().unwrap()],
        &["graph compatibility mismatch"],
    );

    // The new malformed file correctly invalidates the saved graph. The
    // empty-batch no-op contract is independent of graph validation, so test
    // it explicitly with --no-verify and no ambient graph.
    let before = fs::read(&broken).unwrap();
    let mut command = fixture.process(&[
        "bindings",
        "assign",
        "--batch",
        batch.to_str().unwrap(),
        "--no-verify",
        "--format",
        "json",
    ]);
    command.env_remove("DEBUNDLE_GRAPH");
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = parse_stdout_json(&output);
    assert_eq!(report["action"], "noop");
    assert_eq!(report["gate"], "not_required");
    assert_eq!(report["files_written"].as_array().unwrap().len(), 0);
    assert_eq!(fs::read(&broken).unwrap(), before);
}
