//! Mutation JSON, diagnostic artifacts, and recovery over real JS/spec inputs.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_json, run_debundle, write_text_file};
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
        let mut json_args = args.clone();
        json_args.extend(["--format", "json"]);
        let out = fixture.command(&json_args);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(out.status.success(), "{args:?}: {stderr}");
        let report = parse_stdout_json(&out);
        // Graph-only writes stay permitted under the legacy gate, labelled as
        // unassessed and warned about on stderr.
        assert_core(&report, args[1], "applied", "claims_checked");
        assert_eq!(report["needs_full_assessment"], true, "{report}");
        assert!(stderr.contains("needs_full_assessment"), "{stderr}");
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
    assert!(report.get("needs_full_assessment").is_none(), "{report}");
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
    assert_eq!(report["needs_full_assessment"], true, "{report}");
    assert!(fixture.modules.join("b.yaml").exists());
    write_text_file(&fixture.modules.join("ui/empty.yaml"), "members: []\n");
    fixture.assert_rejected_unchanged(
        &["modules", "delete", "ui/empty.yaml"],
        &["graph compatibility mismatch"],
    );
    fixture.refresh_graph();
    let report = fixture.json(&["modules", "delete", "ui/empty.yaml"]);
    assert_core(&report, "delete", "applied", "not_required");
    assert!(report.get("needs_full_assessment").is_none(), "{report}");
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
    let artifact: Value = read_json(&fixture.graph.with_file_name("atomic_group_conflicts.json"));
    assert_eq!(conflicts, artifact.as_array().unwrap());
    let labels: Vec<_> = conflicts[0]["claims"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|claim| claim["binding_names"].as_array().unwrap())
        .map(|name| name.as_str().unwrap())
        .collect();
    assert!(
        labels.contains(&"alpha") && labels.contains(&"beta"),
        "{conflicts:#?}"
    );

    let ordinary_run = GraphFixture::rejected(
        "let alpha = 0;\nfunction beta() { alpha = 1; }\nconsole.log(alpha, typeof beta);\n",
        &[
            (
                "dogfood/split.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            (
                "home/atom.yaml",
                "members: [{selector: {binding: {name: beta}}}]\n",
            ),
        ],
    );
    let ordinary_run_artifact: Value = read_json(
        &ordinary_run
            .graph
            .with_file_name("atomic_group_conflicts.json"),
    );
    assert_eq!(artifact, ordinary_run_artifact);
}

#[test]
fn atom_split_also_writes_current_cycles_for_gate_queries() {
    let fixture = GraphFixture::new(
        "const alpha = beta;\nconst beta = alpha;\n",
        &[(
            "home/atom.yaml",
            "members: [{selector: {binding: {name: alpha}}}, {selector: {binding: {name: beta}}}]\n",
        )],
    );
    let out = fixture.command(&[
        "bindings",
        "assign",
        "alpha:dogfood/split",
        "--format",
        "json",
    ]);
    assert!(!out.status.success());
    let report = parse_stdout_json(&out);
    assert_eq!(report["rejection"]["kind"], "atom_split");

    let atomic_path = fixture.graph.with_file_name("atomic_group_conflicts.json");
    let cycles_path = fixture.graph.with_file_name("cycles.json");
    let atomic: Value = read_json(&atomic_path);
    let cycles: Value = read_json(&cycles_path);
    assert_eq!(atomic.as_array().unwrap().len(), 1, "{atomic}");
    assert_eq!(cycles.as_array().unwrap().len(), 1, "{cycles}");

    let listed = run_debundle(&[
        "gate",
        "list",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        listed.status.success(),
        "{}",
        String::from_utf8_lossy(&listed.stderr)
    );
    let listed = parse_stdout_json(&listed);
    assert_eq!(
        listed["blocking_sccs"].as_array().unwrap().len(),
        1,
        "{listed}"
    );

    let described = run_debundle(&[
        "gate",
        "describe",
        "0",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        described.status.success(),
        "{}",
        String::from_utf8_lossy(&described.stderr)
    );
    let described = parse_stdout_json(&described);
    assert_eq!(
        described["modules"].as_array().unwrap().len(),
        2,
        "{described}"
    );
    assert!(
        described["evidence"].as_array().unwrap().is_empty(),
        "the stored graph still has the original co-located assignment; the candidate cut is stored in cycles.json: {described}"
    );
    assert_eq!(described["modules"], cycles[0]["modules"], "{described}");
    assert_eq!(described["cut"], cycles[0]["cut"], "{described}");
    assert!(
        !described["cut"].as_array().unwrap().is_empty(),
        "{described}"
    );
    assert!(
        described["modules"]
            .as_array()
            .unwrap()
            .iter()
            .any(|module| module == "dogfood/split"),
        "describe must expose the rejected candidate's destination: {described}"
    );
}

#[test]
fn completed_edit_results_replace_both_rejection_artifacts() {
    let fixture = GraphFixture::new(
        "const beta = 1;\nconst gamma = beta + 1;\nconst alpha = gamma + 1;\nconst left = right;\nconst right = left;\n",
        &[
            (
                "a.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
            (
                "c.yaml",
                "members: [{selector: {binding: {name: gamma}}}]\n",
            ),
            (
                "home/atom.yaml",
                "members: [{selector: {binding: {name: left}}}, {selector: {binding: {name: right}}}]\n",
            ),
        ],
    );
    let cycles_path = fixture.graph.with_file_name("cycles.json");
    let atomic_path = fixture.graph.with_file_name("atomic_group_conflicts.json");

    let atom_split = fixture.command(&[
        "bindings",
        "assign",
        "left:dogfood/split",
        "--format",
        "json",
    ]);
    assert!(!atom_split.status.success());
    assert_eq!(
        parse_stdout_json(&atom_split)["rejection"]["kind"],
        "atom_split"
    );
    assert!(cycles_path.exists());
    assert!(atomic_path.exists());
    let atom_cycle: Value = read_json(&cycles_path);

    let cycle_only = fixture.command(&[
        "modules", "merge", "--target", "a.yaml", "b.yaml", "--format", "json",
    ]);
    assert!(!cycle_only.status.success());
    assert_eq!(
        parse_stdout_json(&cycle_only)["rejection"]["kind"],
        "unrealizable_cycles"
    );
    assert!(cycles_path.exists());
    assert!(
        !atomic_path.exists(),
        "the completed cycle-only result must replace the earlier atom-split report"
    );
    let cycle_only_report: Value = read_json(&cycles_path);
    assert_ne!(
        atom_cycle[0]["modules"], cycle_only_report[0]["modules"],
        "the second report must describe its new assignment"
    );

    fixture.assert_success(&["modules", "merge", "--target", "a.yaml", "c.yaml"]);
    assert!(!cycles_path.exists());
    assert!(!atomic_path.exists());
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

#[test]
fn closed_mutation_output_pipes_preserve_applied_state_and_stderr_failures_are_nonzero() {
    use std::process::Stdio;
    for assign in [false, true] {
        for (close_stdout, close_stderr) in [(true, false), (false, true), (true, true)] {
            let fixture = GraphFixture::acyclic_pair();
            let args = if assign {
                vec!["bindings", "assign", "beta:c", "--format", "json"]
            } else {
                // This edit has no pre-outcome warning: only the command boundary's
                // saved-graph diagnostic must still run after stdout closes.
                write_text_file(&fixture.modules.join("empty.yaml"), "{}\n");
                fixture.refresh_graph();
                vec!["modules", "delete", "empty.yaml", "--format", "json"]
            };
            let mut command = fixture.process(&args);
            if close_stdout {
                let (reader, writer) = std::io::pipe().unwrap();
                drop(reader);
                command.stdout(Stdio::from(writer));
            }
            if close_stderr {
                let (reader, writer) = std::io::pipe().unwrap();
                drop(reader);
                command.stderr(Stdio::from(writer));
            }
            let result = command.output().unwrap();
            assert_eq!(
                result.status.success(),
                !close_stderr,
                "assign={assign}, stdout={close_stdout}, stderr={close_stderr}: {result:?}"
            );
            if assign {
                assert!(!fixture.modules.join("b.yaml").exists());
                let destination: serde_yaml::Value =
                    serde_yaml::from_slice(&std::fs::read(fixture.modules.join("c.yaml")).unwrap())
                        .unwrap();
                assert_eq!(destination["members"].as_sequence().unwrap().len(), 1);
            } else {
                assert!(!fixture.modules.join("empty.yaml").exists());
            }
            fixture.assert_runs("2\n");
        }
    }
}

#[test]
fn failed_queries_stay_nonzero_when_stdout_diagnostic_pipe_is_closed() {
    use std::process::Stdio;
    let fixture = GraphFixture::acyclic_pair();
    let (reader, writer) = std::io::pipe().unwrap();
    drop(reader);
    let output = fixture
        .process(&["describe", "nonexistent", "--format", "json"])
        .stdout(Stdio::from(writer))
        .output()
        .unwrap();
    assert!(!output.status.success());
}
