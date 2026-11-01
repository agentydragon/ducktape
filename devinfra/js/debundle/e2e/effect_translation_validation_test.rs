//! Public-pipeline controls for conservative, report-only effect TV.

use debundle_e2e_support::*;
use serde_json::Value;

fn examples(report: &Value) -> &[Value] {
    report["examples"]
        .as_array()
        .expect("effect conflict examples")
}

#[test]
fn a_pair_premise_does_not_hide_another_reader() {
    // JSON.stringify does not execute these function elements. Its opaque
    // fallback nevertheless contributes peek's read, so an author pair
    // premise is legitimate and the other possible read must remain visible.
    let padding = vec!["() => 3"; 64].join(", ");
    let source = format!(
        "let state = 0;\nfunction peek() {{ return state; }}\nconst first = (state = 1, 3);\nconst second = JSON.stringify([peek, {padding}]);\nconst third = JSON.stringify([peek, {padding}]);\nconst tail = second.length + third.length;\nconsole.log(first, tail, state);\nexport {{ state, peek, first, second, third, tail }};\n"
    );
    // In the rejected cyclic order, a_writer is the first entry import;
    // its reader dependencies therefore evaluate before its own body.
    let fixture = run_rejection_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "a_writer",
                    &[
                        Member::new("state"),
                        Member::new("peek"),
                        Member::new("first"),
                        Member::new("tail"),
                    ],
                ),
                logical_module("b_reader", &[Member::new("second")]),
                logical_module("c_reader", &[Member::new("third")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("a_writer", "first"),
                member_source_unit("c_reader", "third"),
            )],
        ),
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["report_only"], true, "{report:#}");
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    assert_eq!(
        report["examples_truncated"], false,
        "the control must expose every pair: {report:#}"
    );
    for (reader, disposition) in [(3, "uncovered"), (4, "covered_by_existing_premise")] {
        let pair = examples(&report)
            .iter()
            .find(|pair| pair["runs_first"]["unit"] == reader && pair["runs_later"]["unit"] == 2)
            .unwrap_or_else(|| panic!("both readers must run before the writer: {report:#}"));
        assert_eq!(pair["disposition"], disposition, "{pair:#}");
        assert!(pair["cells"].as_array().unwrap().iter().any(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "state"), "{pair:#}");
    }
}

#[test]
fn hoisted_functions_have_no_evaluation_write_but_function_expressions_do() {
    let fixture = run_fixture(FixtureOpts::new(
        "function hoisted() { return 1; }\nconst seen = hoisted();\nconst expr = () => 2;\nconsole.log(seen, expr());\nexport { hoisted, seen, expr };\n",
        vec![
            logical_module("functions", &[Member::new("hoisted"), Member::new("expr")]),
            logical_module("reader", &[Member::new("seen")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    assert!(
        !fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl")
            .exists()
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["full_semantic_precision"], false, "{report:#}");
    assert!(
        examples(&report)
            .iter()
            .all(|pair| pair["runs_first"]["unit"] != 0 && pair["runs_later"]["unit"] != 0),
        "hoisted declaration must have no effects: {report:#}"
    );
    assert!(
        examples(&report)
            .iter()
            .any(|pair| pair["runs_first"]["unit"] == 2
                && pair["runs_later"]["unit"] == 1
                && pair["havoc"] == true),
        "function-expression initialization remains an evaluation effect: {report:#}"
    );
}

#[test]
fn complete_execution_does_not_erase_missing_global_or_host_coverage() {
    let fixture = run_fixture(FixtureOpts::new(
        "function write() { globalThis.marker = 1; }\nconst first = (write(), 1);\nconst second = globalThis.marker;\nconst tail = second;\nconsole.log(first, tail);\nexport { write, first, second, tail };\n",
        vec![logical_module(
            "all",
            &[
                Member::new("write"),
                Member::new("first"),
                Member::new("second"),
                Member::new("tail"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 1\n");
    let graph = fixture.dependency_graph();
    let first = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 1)
        .expect("caller statement");
    assert!(
        first
            .synchronous_execution
            .as_ref()
            .expect("execution summary")
            .complete,
        "control must exercise complete execution: {first:#?}"
    );
    let report = fixture.effect_translation_validation();
    assert!(
        report["coverage"]["unknown_coverage"]["global_properties"]
            .as_u64()
            .unwrap()
            > 0,
        "{report:#}"
    );
    assert!(
        report["coverage"]["unknown_coverage"]["host_effects"]
            .as_u64()
            .unwrap()
            > 0,
        "{report:#}"
    );
}

#[test]
fn default_report_bounds_examples_without_losing_exact_counts() {
    let names = [
        "item0", "item1", "item2", "item3", "item4", "item5", "item6", "item7", "item8", "item9",
        "item10", "item11", "item12", "item13", "item14", "item15", "item16", "item17", "item18",
        "item19", "item20", "item21", "item22", "item23",
    ];
    let mut source = String::new();
    for (index, name) in names.iter().enumerate() {
        source.push_str(&format!("const {name} = {index};\n"));
    }
    source.push_str(&format!(
        "const tail = {};\nconsole.log(tail);\n",
        names.join(" + ")
    ));
    let mut modules = vec![logical_module(
        "outer",
        &[Member::new("item0"), Member::new("tail")],
    )];
    modules.extend(
        names[1..]
            .iter()
            .map(|name| logical_module(name, &[Member::new(name)])),
    );
    // Outer imports the later literals before item0 runs. Other loader
    // ordering remains emitter-owned; every resulting swap must be counted.
    let fixture = run_fixture_with_args(
        FixtureOpts::new(&source, modules),
        &["--effect-conflicts-jsonl"],
    );
    let report = fixture.effect_translation_validation();
    let count = |name| report["counts"][name].as_u64().unwrap();
    assert_eq!(
        count("pairs"),
        report["swapped_pairs"].as_u64().unwrap(),
        "every runtime statement retains Havoc: {report:#}"
    );
    assert!(count("pairs") > 20, "{report:#}");
    assert_eq!(
        count("pairs"),
        count("uncovered_pairs") + count("premise_covered_pairs") + count("premise_contradictions")
    );
    assert_eq!(
        count("pairs"),
        count("effect_only_pairs") + count("both_pairs")
    );
    assert_eq!(examples(&report).len(), 20);
    assert_eq!(report["examples_truncated"], true);
    assert_eq!(report["full_detail_export_available"], true);
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    assert_full_rows_match_summary(&report, &rows);
}

fn assert_full_rows_match_summary(summary: &Value, rows: &[Value]) {
    let count = |name| summary["counts"][name].as_u64().unwrap();
    assert_eq!(rows.len() as u64, count("pairs"));
    assert_eq!(
        rows.iter()
            .map(|row| row["cells"].as_array().unwrap().len() as u64)
            .sum::<u64>(),
        count("cell_incidences")
    );
    assert_eq!(
        rows.iter().filter(|row| row["havoc"] == true).count() as u64,
        count("havoc_pairs")
    );
    for (disposition, category) in [
        ("uncovered", "uncovered_pairs"),
        ("covered_by_existing_premise", "premise_covered_pairs"),
        ("contradicts_existing_premise", "premise_contradictions"),
    ] {
        assert_eq!(
            rows.iter()
                .filter(|row| row["disposition"] == disposition)
                .count() as u64,
            count(category)
        );
    }
    assert_eq!(
        rows.iter()
            .filter(|row| row["legacy_edge_conflict"] == true)
            .count() as u64,
        count("both_pairs")
    );
    for row in rows {
        assert_eq!(
            row["cell_count"].as_u64().unwrap(),
            row["cells"].as_array().unwrap().len() as u64
        );
        assert!(row["first_opacity"].is_object() && row["later_opacity"].is_object());
    }
    for (example, full) in examples(summary).iter().zip(rows) {
        for field in [
            "runs_first",
            "runs_later",
            "havoc",
            "disposition",
            "premise_locations",
            "cell_count",
        ] {
            assert_eq!(example[field], full[field], "{field}");
        }
        let cells = full["cells"].as_array().unwrap();
        let limit = summary["cell_example_limit"].as_u64().unwrap() as usize;
        assert_eq!(
            example["cells"].as_array().unwrap().as_slice(),
            &cells[..cells.len().min(limit)]
        );
        assert_eq!(example["cells_truncated"], cells.len() > limit);
    }
}

#[test]
fn full_stream_retains_cells_beyond_examples_and_pair_dispositions() {
    let slots = [
        "state0", "state1", "state2", "state3", "state4", "state5", "state6", "state7", "state8",
        "state9", "state10", "state11", "state12", "state13", "state14", "state15", "state16",
        "state17", "state18", "state19", "state20", "state21", "state22", "state23",
    ];
    let declarations = slots
        .iter()
        .map(|slot| format!("{slot} = 0"))
        .collect::<Vec<_>>()
        .join(", ");
    let writes = slots
        .iter()
        .map(|slot| format!("{slot} = 1"))
        .collect::<Vec<_>>()
        .join(", ");
    let padding = vec!["() => 3"; 64].join(", ");
    let source = format!(
        "let {declarations};\nfunction peek() {{ return {}; }}\nconst first = ({writes}, 3);\nconst second = JSON.stringify([peek, {padding}]);\nconst third = JSON.stringify([peek, {padding}]);\nconst tail = second.length + third.length;\nconsole.log(first, tail);\nexport {{ first, second, third, tail }};\n",
        slots.join(" + ")
    );
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "a_writer",
                    &[
                        Member::new("state0"),
                        Member::new("peek"),
                        Member::new("first"),
                        Member::new("tail"),
                    ],
                ),
                logical_module("b_reader", &[Member::new("second")]),
                logical_module("c_reader", &[Member::new("third")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("a_writer", "first"),
                member_source_unit("c_reader", "third"),
            )],
        ),
        &["--effect-conflicts-jsonl", "--dry-run"],
        1,
    );
    let summary = fixture.effect_translation_validation();
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    assert_full_rows_match_summary(&summary, &rows);
    assert_eq!(summary["examples_truncated"], true);
    let covered = rows
        .iter()
        .find(|row| row["disposition"] == "covered_by_existing_premise")
        .expect("covered writer/reader pair");
    assert_eq!(covered["disposition"], "covered_by_existing_premise");
    assert_eq!(covered["cells"].as_array().unwrap().len(), slots.len());
    assert!(
        covered["cells"]
            .as_array()
            .unwrap()
            .iter()
            .all(|cell| cell["first_read"]["kind"] == "fallback")
    );
    assert!(
        !covered["first_opacity"]["execution_losses"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        !covered["later_opacity"]["unknown_coverage"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn accepted_dry_run_refuses_requested_export_without_writing_it() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "const value = 1;\nexport { value };\n",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    let result = run_debundle(&[
        "run",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--dry-run",
        "--effect-conflicts-jsonl",
    ]);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("report policy suppresses output"));
    assert!(
        !fixture
            .spec_path
            .parent()
            .unwrap()
            .join("out/reports/tree/static/app/effect_conflicts.jsonl")
            .exists()
    );
}

#[test]
fn export_chunk_filter_requires_export_and_rejects_unknown_chunks() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "const value = 1;\nexport { value };\n",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    for (flags, diagnostic) in [
        (
            vec!["--effect-conflicts-chunk", "static/app"],
            "--effect-conflicts-jsonl",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "static/typo",
            ],
            "unknown --effect-conflicts-chunk",
        ),
    ] {
        let mut args = vec!["run", "--spec", fixture.spec_path.to_str().unwrap()];
        args.extend(flags);
        let result = run_debundle(&args);
        assert!(!result.status.success());
        assert!(
            String::from_utf8_lossy(&result.stderr).contains(diagnostic),
            "{result:?}"
        );
    }
}
