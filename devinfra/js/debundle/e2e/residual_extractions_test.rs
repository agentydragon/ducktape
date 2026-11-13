//! Hypothetical residual moves use the same gate as actual assignments.

use debundle_e2e_support::*;
use serde_json::Value;
use std::path::Path;

const SOURCE: &str = "const a = console.log('a');\nconst b = console.log('b');\nconst c = console.log('c');\nconst foo = 1;\nconsole.log('tail', foo);\nexport { a, b, c, foo };\n";

fn report(root: &Path, chunk: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(root.join(chunk).join("residual_extractions.json")).unwrap(),
    )
    .unwrap()
}

fn group_for_binding<'a>(report: &'a Value, binding: &str) -> &'a Value {
    report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| {
            group["source_units"]
                .as_array()
                .unwrap()
                .iter()
                .any(|unit| {
                    unit["bindings"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|name| name == binding)
                })
        })
        .unwrap()
}

#[test]
fn extracting_middle_of_residual_order_is_rejected_with_both_directions() {
    let baseline = run_fixture_without_graphs_with_args(
        FixtureOpts::new(SOURCE, vec![]).with_unassigned_mode(unassigned_mode_inline()),
        &["--full-report", "residual_extractions"],
    );
    assert_entry_output(&baseline, "a\nb\nc\ntail 1\n");
    let evidence = report(&baseline.report_root, &baseline.chunk_id);
    assert_eq!(evidence["baseline"]["gate_accepts"], true);
    assert!(
        evidence["baseline"]["cycles"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let b = group_for_binding(&evidence, "b");
    assert_eq!(b["candidate"]["gate_accepts"], false);
    assert_eq!(b["source_units"][0]["source_location"]["start_line"], 2);
    let target = b["candidate"]["proposed_destination"].as_str().unwrap();
    let cycles = b["candidate"]["cycles"].as_array().unwrap();
    let witness = cycles
        .iter()
        .find_map(|cycle| cycle["pass_one_witness"]["steps"].as_array())
        .expect("a real pass-one closed walk");
    assert!(witness.iter().any(|edge| edge["from"] == target));
    assert!(witness.iter().any(|edge| edge["to"] == target));
    assert!(witness.iter().all(|edge| {
        edge["from_source_unit_id"].is_string() && edge["to_source_unit_id"].is_string()
    }));
    let locations = b["candidate"]["blocker_source_units"].as_array().unwrap();
    assert!(
        locations
            .iter()
            .any(|unit| unit["source_location"]["start_line"] == 1)
    );
    assert!(
        locations
            .iter()
            .any(|unit| unit["source_location"]["start_line"] == 2)
    );
    assert!(
        locations
            .iter()
            .any(|unit| unit["source_location"]["start_line"] == 3)
    );
    assert_eq!(
        group_for_binding(&evidence, "foo")["candidate"]["gate_accepts"],
        true
    );
    assert!(
        evidence["groups"]
            .as_array()
            .unwrap()
            .iter()
            .any(|group| { group["unprobed_reason"] == "entry_scaffolding" })
    );
    assert!(evidence["groups"].as_array().unwrap().iter().any(|group| {
        group["source_units"]
            .as_array()
            .unwrap()
            .iter()
            .any(|unit| {
                unit["kind"] == "side_effect" && unit["bindings"].as_array().unwrap().is_empty()
            })
    }));

    let rejected = run_rejection_fixture_without_graphs_with_args(
        FixtureOpts::new(SOURCE, vec![logical_module("b", &[Member::new("b")])])
            .with_unassigned_mode(unassigned_mode_inline()),
        &[],
        1,
    );
    let actual_cycles: Value = serde_json::from_slice(
        &std::fs::read(rejected.report_root.join("static/app/cycles.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(actual_cycles[0]["rejection"], "mutual_constraining_cycle");
    let accepted = run_fixture_without_graphs_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module("a", &[Member::new("a")]),
                logical_module("b", &[Member::new("b")]),
                logical_module("c", &[Member::new("c")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &[],
    );
    assert_entry_output(&accepted, "a\nb\nc\ntail 1\n");
}

#[test]
fn rejected_candidate_with_authored_commuting_premise_serializes_typed_evidence() {
    const SOURCE_WITH_PREMISE: &str = "const a = console.log('a');\nconst b = console.log('b');\nconst c = console.log('c');\nconst bar = console.log('bar');\nconst foo = 1;\nconsole.log('tail', foo);\nexport { a, b, c, bar, foo };\n";
    let fixture = run_fixture_without_graphs_with_args(
        FixtureOpts::new(SOURCE_WITH_PREMISE, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_commuting_statements(
                "static/app",
                vec![commuting_pair(
                    declared_source_unit("const c = console.log('c');", "c"),
                    declared_source_unit("const bar = console.log('bar');", "bar"),
                )],
            ),
        &["--full-report", "residual_extractions"],
    );
    assert_entry_output(&fixture, "a\nb\nc\nbar\ntail 1\n");
    let evidence = report(&fixture.report_root, &fixture.chunk_id);
    assert_eq!(evidence["baseline"]["gate_accepts"], true);
    let b = group_for_binding(&evidence, "b");
    assert_eq!(b["candidate"]["gate_accepts"], false);
    let cycles = b["candidate"]["cycles"].as_array().unwrap();
    let cycle = cycles
        .iter()
        .find(|cycle| cycle["rejection"] == "mutual_constraining_cycle")
        .expect("actual Pass-1 blocker");
    assert!(cycle["pass_one_witness"]["steps"].is_array());
    let locations = cycle["commuting_premise_locations"].as_array().unwrap();
    assert_eq!(locations.len(), 1);
    assert_eq!(locations[0]["premise"]["defining_chunk"], "static/app");
    assert_eq!(locations[0]["premise"]["input_index"], 0);
    assert!(
        locations[0]["spec_location"]
            .as_str()
            .unwrap()
            .contains("commuting_statements[0]")
    );
}

#[test]
fn preexisting_structural_conflict_remains_a_candidate_blocker() {
    let rejected = run_rejection_fixture_without_graphs_with_args(
        FixtureOpts::new(
            "export const a = 1;\nexport let b = 2;\nb += 1;\n",
            vec![logical_module("b", &[Member::new("b")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--full-report", "residual_extractions"],
        1,
    );
    let evidence = report(&rejected.report_root, &rejected.chunk_id);
    assert_eq!(evidence["baseline"]["gate_accepts"], false);
    let a = group_for_binding(&evidence, "a");
    assert_eq!(a["candidate"]["gate_accepts"], false);
    assert!(
        !a["candidate"]["atomic_group_conflicts"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let b = group_for_binding(&evidence, "b");
    assert_eq!(b["unprobed_reason"], "split_atomic_group");
}

#[test]
fn selection_and_ownership_subset_probe_the_exact_current_assignment() {
    let subset = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        subset.path(),
        r#"{"modules":[{"chunk":"static/app","module":"a"}]}"#,
    )
    .unwrap();
    let opts = || {
        FixtureOpts::new(
            "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n",
            vec![
                logical_module("a", &[Member::new("a")]),
                logical_module("b", &[Member::new("b")]),
                logical_module("c", &[Member::new("c")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
    };
    let without = run_fixture_without_graphs_with_args(
        opts(),
        &["--ownership-subset", subset.path().to_str().unwrap()],
    );
    assert!(
        !without
            .report_root
            .join(&without.chunk_id)
            .join("residual_extractions.json")
            .exists()
    );
    let all = run_fixture_without_graphs_with_args(
        opts(),
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual_extractions",
        ],
    );
    let all_report = report(&all.report_root, &all.chunk_id);
    assert!(all_report["ownership_subset"].is_array());
    assert!(
        all_report["groups"]
            .as_array()
            .unwrap()
            .iter()
            .all(|group| {
                !group["source_units"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|unit| {
                        unit["bindings"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .any(|name| name == "a")
                    })
            })
    );
    let selected = group_for_binding(&all_report, "b")["id"].as_str().unwrap();
    let one = run_fixture_without_graphs_with_args(
        opts(),
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual_extractions",
            "--residual-extraction-atom",
            selected,
        ],
    );
    let one_report = report(&one.report_root, &one.chunk_id);
    assert_eq!(
        group_for_binding(&one_report, "b")["candidate"]["gate_accepts"],
        true
    );
    assert_eq!(
        group_for_binding(&one_report, "c")["unprobed_reason"],
        "not_selected"
    );
    assert_eq!(one_report["baseline"]["gate_accepts"], true);
}
