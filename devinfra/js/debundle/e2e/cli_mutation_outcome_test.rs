//! Mutation JSON, diagnostic artifacts, and recovery over real JS/spec inputs.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, run_debundle, write_text_file};
use serde_json::Value;

fn assert_core(report: &Value, verb: &str, action: &str, gate: &str) {
    assert_eq!(report["verb"], verb, "{report}");
    assert_eq!(report["action"], action, "{report}");
    assert_eq!(report["gate"], gate, "{report}");
}

fn has_file(report: &Value, field: &str, path: &str) -> bool {
    report[field]
        .as_array()
        .unwrap()
        .iter()
        .any(|p| p.as_str().unwrap().ends_with(path))
}

#[test]
fn assign_unassign_and_merge_report_the_edits_that_still_run() {
    for (args, count, written, deleted) in [
        (
            vec!["bindings", "assign", "beta:c"],
            Some("moves_applied"),
            "c.yaml",
            "b.yaml",
        ),
        (
            vec!["bindings", "unassign", "alpha"],
            Some("unassigned"),
            "",
            "a.yaml",
        ),
        (
            vec!["modules", "merge", "--target", "a.yaml", "b.yaml"],
            None,
            "a.yaml",
            "b.yaml",
        ),
    ] {
        let fixture = GraphFixture::acyclic_pair_full_tree();
        let analysis = fixture.stage_analysis();
        let report = fixture.json_with_assessment(&args, &analysis);
        assert_core(&report, args[1], "applied", "passed");
        if let Some(count) = count {
            assert_eq!(report[count], 1);
        }
        if !written.is_empty() {
            assert!(has_file(&report, "files_written", written), "{report}");
        }
        assert!(has_file(&report, "files_deleted", deleted), "{report}");
        if args[1] == "merge" {
            assert_eq!(report["files_deleted"].as_array().unwrap().len(), 1);
            assert!(report["target"].as_str().unwrap().ends_with("a.yaml"));
        }
        fixture.assert_runs("2\n");
    }
}

#[test]
fn names_only_rename_reports_its_file_and_binding() {
    let fixture = GraphFixture::acyclic_pair();
    let out = run_debundle(&[
        "bindings",
        "rename",
        "--modules",
        fixture.modules.to_str().unwrap(),
        "--format",
        "json",
        "alpha",
        "ReadableAlpha",
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let report = parse_stdout_json(&out);
    assert_core(&report, "rename", "applied", "names_only");
    assert_eq!(report["identity"]["kind"], "direct_binding_identifier");
    assert_eq!(report["identity"]["name"], "alpha");
    assert!(report.get("binding").is_none());
    assert_eq!(report["new_readable"], "ReadableAlpha");
    assert_eq!(report["files_written"].as_array().unwrap().len(), 1);
    assert_eq!(report["files_deleted"].as_array().unwrap().len(), 0);
    fixture.assert_runs("2\n");
}

#[test]
fn dry_run_merge_and_empty_delete_report_their_gate_contract() {
    let fixture = GraphFixture::acyclic_pair();
    let report = fixture.json(&[
        "modules",
        "merge",
        "--dry-run",
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert_core(&report, "merge", "dry-run", "claims_checked");
    assert_eq!(report["needs_full_assessment"], true);
    assert!(fixture.modules.join("b.yaml").exists());
    write_text_file(&fixture.modules.join("ui/empty.yaml"), "members: []\n");
    fixture.assert_rejected_unchanged(
        &["modules", "delete", "ui/empty.yaml"],
        &["graph compatibility mismatch"],
    );
    fixture.refresh_graph();
    let report = fixture.json(&["modules", "delete", "ui/empty.yaml"]);
    assert_core(&report, "delete", "applied", "not_required");
    assert_eq!(report["files_written"].as_array().unwrap().len(), 0);
    assert_eq!(report["files_deleted"].as_array().unwrap().len(), 1);
    assert!(has_file(&report, "files_deleted", "ui/empty.yaml"));
    fixture.assert_runs("2\n");
}

#[test]
fn graph_only_atom_split_reports_a_candidate_preview_without_legacy_artifacts() {
    let fixture = GraphFixture::atomic_pair();
    let preview = fixture.json(&["bindings", "assign", "alpha:dogfood/split", "--dry-run"]);
    assert_core(&preview, "assign", "dry-run", "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    assert!(
        has_file(&preview, "files_written", "dogfood/split.yaml"),
        "{preview}"
    );
    let original = std::fs::read(fixture.modules.join("home/atom.yaml")).unwrap();
    let output = fixture.command(&[
        "bindings",
        "assign",
        "alpha:dogfood/split",
        "--format",
        "json",
    ]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    assert_eq!(
        std::fs::read(fixture.modules.join("home/atom.yaml")).unwrap(),
        original
    );
    assert!(!fixture.modules.join("dogfood/split.yaml").exists());
    assert!(
        !fixture
            .graph
            .with_file_name("atomic_group_conflicts.json")
            .exists()
    );
}

#[test]
fn graph_only_cycle_candidate_needs_full_assessment_and_preserves_modules() {
    let fixture = GraphFixture::dependency_chain();
    let before = std::fs::read(fixture.modules.join("b.yaml")).unwrap();
    let preview = fixture.json(&[
        "modules",
        "merge",
        "--target",
        "a.yaml",
        "b.yaml",
        "--dry-run",
    ]);
    assert_core(&preview, "merge", "dry-run", "claims_checked");
    assert_eq!(preview["needs_full_assessment"], true);
    let output = fixture.command(&[
        "modules", "merge", "--target", "a.yaml", "b.yaml", "--format", "json",
    ]);
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs_full_assessment"));
    assert_eq!(
        std::fs::read(fixture.modules.join("b.yaml")).unwrap(),
        before
    );
    assert!(!fixture.graph.with_file_name("cycles.json").exists());
}

#[test]
fn text_rejection_does_not_emit_json() {
    let out = GraphFixture::dependency_chain().command(&[
        "modules", "merge", "--target", "a.yaml", "b.yaml", "--format", "text",
    ]);
    assert!(!out.status.success());
    assert!(!out.stderr.is_empty());
    assert!(serde_json::from_slice::<Value>(&out.stdout).is_err());
}

#[test]
fn no_verify_assignment_is_reported_as_unvalidated() {
    let fixture = GraphFixture::atomic_pair();
    let output = fixture.command(&[
        "bindings",
        "assign",
        "alpha:dogfood/split",
        "--no-verify",
        "--format",
        "json",
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = parse_stdout_json(&output);
    assert_eq!(report["gate"], "skipped");
    assert!(report.get("needs_full_assessment").is_none());
    assert!(String::from_utf8_lossy(&output.stderr).contains("this assignment is unvalidated"));
    assert!(fixture.modules.join("dogfood/split.yaml").is_file());
}
