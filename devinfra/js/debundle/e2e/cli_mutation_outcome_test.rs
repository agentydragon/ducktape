//! Mutation JSON, diagnostic artifacts, and recovery over real JS/spec inputs.

use debundle_e2e_support::{
    GraphFixture, parse_stdout_json, read_json, run_debundle, write_text_file,
};
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
        let fixture = GraphFixture::acyclic_pair();
        let report = fixture.json(&args);
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
    assert_eq!(report["binding"], "alpha");
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
    assert_core(&report, "merge", "dry-run", "passed");
    assert!(fixture.modules.join("b.yaml").exists());
    write_text_file(&fixture.modules.join("ui/empty.yaml"), "members: []\n");
    let report = fixture.json(&["modules", "delete", "ui/empty.yaml"]);
    assert_core(&report, "delete", "applied", "not_required");
    assert_eq!(report["files_written"].as_array().unwrap().len(), 0);
    assert_eq!(report["files_deleted"].as_array().unwrap().len(), 1);
    assert!(has_file(&report, "files_deleted", "ui/empty.yaml"));
    fixture.assert_runs("2\n");
}

#[test]
fn atom_split_reports_both_destinations_and_writes_the_same_artifact() {
    let fixture = GraphFixture::atomic_pair();
    let out = fixture.command(&[
        "bindings",
        "assign",
        "alpha:dogfood/split",
        "--format",
        "json",
    ]);
    assert!(!out.status.success());
    let report = parse_stdout_json(&out);
    assert_eq!(report["verb"], "assign");
    assert_eq!(report["action"], "rejected");
    assert_eq!(report["rejection"]["kind"], "atom_split");
    let conflicts = report["rejection"]["conflicts"].as_array().unwrap();
    assert_eq!(conflicts.len(), 1);
    let modules: Vec<_> = conflicts[0]["claims"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["module"].as_str().unwrap())
        .collect();
    assert!(modules.contains(&"dogfood/split") && modules.contains(&"home/atom"));
    let artifact: Value = read_json(&fixture.graph.with_file_name("atomic_unit_conflicts.json"));
    assert_eq!(artifact.as_array().unwrap().len(), 1);
}

#[test]
fn merge_cycle_report_drives_gate_list_then_a_passing_edit_clears_it() {
    let fixture = GraphFixture::dependency_chain();
    let out = fixture.command(&[
        "modules", "merge", "--target", "a.yaml", "b.yaml", "--format", "json",
    ]);
    assert!(!out.status.success());
    let report = parse_stdout_json(&out);
    assert_eq!(report["verb"], "merge");
    assert_eq!(report["action"], "rejected");
    assert_eq!(report["rejection"]["kind"], "unrealizable_cycles");
    let sccs = report["rejection"]["blocking_sccs"].as_array().unwrap();
    assert_eq!(sccs.len(), 1);
    let modules: Vec<_> = sccs[0]["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert!(modules.contains(&"a") && modules.contains(&"c"));
    assert!(!sccs[0]["cut"].as_array().unwrap().is_empty());
    let gate_list = || {
        let out = run_debundle(&[
            "gate",
            "list",
            "--graph",
            fixture.graph.to_str().unwrap(),
            "--format",
            "json",
        ]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        parse_stdout_json(&out)
    };
    let report = gate_list();
    assert_eq!(report["blocking_sccs"].as_array().unwrap().len(), 1);
    assert_eq!(report["blocking_sccs"][0]["module_count"], 2);
    assert!(fixture.graph.with_file_name("cycles.json").exists());
    fixture.assert_success(&["modules", "merge", "--target", "a.yaml", "c.yaml"]);
    assert!(!fixture.graph.with_file_name("cycles.json").exists());
    fixture.assert_runs("3\n");
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
