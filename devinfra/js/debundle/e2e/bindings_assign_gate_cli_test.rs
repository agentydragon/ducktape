//! Real JS/spec workflows for assignment validation and atomic edits.

use debundle_e2e_support::{GraphFixture, run_debundle, write_text_file};
use std::fs;

#[test]
fn bindings_assign_rejects_atom_split_in_both_modes_without_writes() {
    GraphFixture::atomic_pair().assert_rejected_unchanged(
        &["bindings", "assign", "alpha:dogfood/split"],
        &["splits one or more atomic units"],
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
fn bulk_edits_resolve_both_spellings_against_the_pre_edit_spec() {
    let fixture = GraphFixture::new(
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
    let report = fixture.json(&[
        "bindings",
        "assign",
        "Alpha:dest:First",
        "alpha:dest:First",
        "Beta:dest:Second",
    ]);
    assert_eq!(report["moves_applied"], 2);
    assert!(!fixture.modules.join("home.yaml").exists());
    let report = fixture.json(&["bindings", "unassign", "First", "alpha", "Second"]);
    assert_eq!(report["unassigned"], 2);
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
    let report = fixture.json(&["bindings", "assign", "--batch", batch.to_str().unwrap()]);
    assert_eq!(report["action"], "noop");
    assert_eq!(report["gate"], "not_required");
    assert_eq!(report["files_written"].as_array().unwrap().len(), 0);
    assert_eq!(fs::read_to_string(broken).unwrap(), "[invalid yaml");
}
