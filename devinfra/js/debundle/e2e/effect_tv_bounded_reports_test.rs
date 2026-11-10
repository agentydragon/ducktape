//! Report-only effect translation validation: bounded examples, full
//! streams and export filters.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;
use serde_json::Value;

#[test]
fn uncovered_modeled_and_havoc_pair_is_a_conflict() {
    let source = "const written = (globalThis.marker = 1);\n\
const read = globalThis.marker;\n\
console.log(written, read);\n";
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("reader", &[Member::new("read")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["decision"]["status"], "conflict", "{report:#}");
    assert_eq!(
        report["decision"]["uncovered_modeled_may_conflict"], true,
        "{report:#}"
    );
    assert_eq!(report["decision"]["havoc"], true, "{report:#}");
    assert_eq!(report["decision"]["uncovered_havoc"], true, "{report:#}");
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    assert_full_rows_match_summary(&report, &rows);
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
        // A static template is pure but outside the source-covered value
        // subset, preserving this control's opaque conflict inventory.
        source.push_str(&format!("const {name} = (``, {index});\n"));
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
        report["legacy_edge_comparison"]["effect_only_pairs"]
            .as_u64()
            .unwrap()
            + report["legacy_edge_comparison"]["both_pairs"]
                .as_u64()
                .unwrap()
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

#[test]
fn bounded_examples_prefer_modeled_rows_after_early_havoc_only_rows() {
    let names = [
        "item0", "item1", "item2", "item3", "item4", "item5", "item6", "item7", "item8", "item9",
        "item10", "item11", "item12", "item13", "item14", "item15", "item16", "item17", "item18",
        "item19", "item20", "item21", "item22", "item23",
    ];
    let mut source = String::new();
    for (index, name) in names.iter().enumerate() {
        // The template is pure but outside the source-covered value subset,
        // retaining an early Havoc-only conflict inventory.
        source.push_str(&format!("const {name} = (``, {index});\n"));
    }
    source.push_str(&format!(
        "const tail = {};\nconst writer = (globalThis.marker = 1);\nconst reader = globalThis.marker;\nconsole.log(tail, writer, reader);\n",
        names.join(" + ")
    ));
    let mut modules = vec![logical_module(
        "outer",
        &[
            Member::new("item0"),
            Member::new("tail"),
            Member::new("reader"),
        ],
    )];
    modules.extend(
        names[1..]
            .iter()
            .map(|name| logical_module(name, &[Member::new(name)])),
    );
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(&source, modules).with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = fixture.effect_translation_validation();
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    let example_limit = report["example_limit"].as_u64().unwrap() as usize;
    let first_modeled_row = rows
        .iter()
        .position(|row| row["cell_count"].as_u64().unwrap() > 0)
        .expect("the full stream contains modeled-cell conflicts");
    assert!(
        first_modeled_row >= example_limit,
        "first modeled row {first_modeled_row} precedes example limit {example_limit}"
    );
    assert!(
        rows[..example_limit]
            .iter()
            .all(|row| row["cell_count"] == 0 && row["havoc"] == true),
        "the fixture begins with {example_limit} Havoc-only rows"
    );
    assert!(
        examples(&report)
            .iter()
            .any(|example| example["cell_count"].as_u64().unwrap() > 0),
        "the bounded report should expose a modeled-cell witness after the initial Havoc-only rows"
    );
    assert_eq!(report["examples_truncated"], true, "{report:#}");
    assert_eq!(report["decision"]["status"], "conflict");
    assert_eq!(report["legacy_gate_accepts"], false);
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
        summary["legacy_edge_comparison"]["both_pairs"]
            .as_u64()
            .unwrap()
    );
    for row in rows {
        assert_eq!(
            row["cell_count"].as_u64().unwrap(),
            row["cells"].as_array().unwrap().len() as u64
        );
        assert!(row["first_opacity"].is_object() && row["later_opacity"].is_object());
    }
    let limit = summary["example_limit"].as_u64().unwrap() as usize;
    let mut selected_indexes = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row["cell_count"].as_u64().unwrap() > 0)
        .take(limit)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let fallback_slots = limit.saturating_sub(selected_indexes.len());
    selected_indexes.extend(
        rows.iter()
            .enumerate()
            .filter(|(_, row)| row["cell_count"].as_u64().unwrap() == 0)
            .take(fallback_slots)
            .map(|(index, _)| index),
    );
    selected_indexes.sort_unstable();
    assert_eq!(examples(summary).len(), selected_indexes.len());
    for (example, full) in examples(summary)
        .iter()
        .zip(selected_indexes.into_iter().map(|i| &rows[i]))
    {
        for field in [
            "runs_first",
            "runs_later",
            "havoc",
            "disposition",
            "premise_locations",
            "premise_references",
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
    assert_eq!(summary["decision"]["status"], "conflict", "{summary:#}");
    let covered = rows
        .iter()
        .find(|row| row["disposition"] == "covered_by_existing_premise")
        .expect("covered writer/reader pair");
    assert_eq!(covered["disposition"], "covered_by_existing_premise");
    assert_eq!(
        covered["premise_references"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the premise remains a typed condition on the covered pair: {covered:#}"
    );
    assert_eq!(
        summary["premise_dependencies"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the full assessment retains the premise dependency: {summary:#}"
    );
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
        (
            vec!["--effect-conflicts-jsonl", "--effect-conflicts-chunk", ""],
            "Expected a non-empty relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "/main",
            ],
            "Expected a non-empty relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "../main",
            ],
            "Invalid relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "main/..",
            ],
            "Invalid relative path",
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
