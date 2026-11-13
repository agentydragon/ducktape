//! Residual inventory is derived from the actual subset assignment.

use debundle_e2e_support::*;
use serde_json::Value;
use std::path::Path;

fn subset() -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        file.path(),
        r#"{"modules":[{"chunk":"static/app","module":"a"}]}"#,
    )
    .unwrap();
    file
}

fn report(root: &Path, chunk: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(root.join(chunk).join("residual_atomic_groups.json")).unwrap(),
    )
    .unwrap()
}

fn has_binding(group: &Value, name: &str) -> bool {
    group["source_units"]
        .as_array()
        .unwrap()
        .iter()
        .any(|unit| {
            unit["bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == name)
        })
}

#[test]
fn subset_reports_residual_binding_and_anonymous_effect_as_one_atom() {
    let source = "export const a = 1;\nexport let b = a + 1;\nb += 1;\nconsole.log('done');\n";
    let modules = vec![
        logical_module("a", &[Member::new("a")]),
        logical_module("b", &[Member::new("b")]),
    ];
    let subset = subset();
    let opts = FixtureOpts::new(source, modules).with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture_without_graphs_with_args(
        opts,
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual-atomic-groups",
        ],
    );
    assert_entry_output(&fixture, "done\n");
    let report = report(&fixture.report_root, &fixture.chunk_id);
    assert_eq!(report["gate_accepts"], true);
    assert_eq!(report["chunk_id"], "static/app");
    assert!(report["ownership_subset"].is_array());
    let groups = report["groups"].as_array().unwrap();
    assert!(!groups.iter().any(|group| has_binding(group, "a")));
    let group = groups.iter().find(|group| has_binding(group, "b")).unwrap();
    assert_eq!(group["fully_residual"], true);
    let units = group["source_units"].as_array().unwrap();
    assert!(units.iter().any(
        |unit| unit["bindings"].as_array().unwrap().is_empty() && unit["kind"] == "side_effect"
    ));
    assert!(
        units
            .iter()
            .all(|unit| unit["destination"]["residual"] == true)
    );
    assert!(
        units
            .iter()
            .all(|unit| unit["source_location"]["start_line"].is_number())
    );
    let dependencies = group["outgoing"]["eager_use"]["examples"]
        .as_array()
        .unwrap();
    assert!(
        dependencies
            .iter()
            .any(|edge| { edge["other_destination"]["module"] == "a" && edge["binding"] == "a" })
    );
}

#[test]
fn generated_residual_modules_are_reported_and_report_is_opt_in() {
    let source = "export const a = 1;\nexport const b = 2;\n";
    let modules = || {
        vec![
            logical_module("a", &[Member::new("a")]),
            logical_module("b", &[Member::new("b")]),
        ]
    };
    let subset = subset();
    let subset_path = subset.path().to_str().unwrap();
    let opts = || {
        FixtureOpts::new(source, modules()).with_unassigned_mode(unassigned_mode_per_atomic_group())
    };
    let without =
        run_fixture_without_graphs_with_args(opts(), &["--ownership-subset", subset_path]);
    assert!(
        !without
            .report_root
            .join(&without.chunk_id)
            .join("residual_atomic_groups.json")
            .exists()
    );
    let fixture = run_fixture_without_graphs_with_args(
        opts(),
        &[
            "--ownership-subset",
            subset_path,
            "--full-report",
            "residual-atomic-groups",
        ],
    );
    let report = report(&fixture.report_root, &fixture.chunk_id);
    let group = report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| has_binding(group, "b"))
        .unwrap();
    let destination = &group["source_units"][0]["destination"];
    assert_eq!(destination["residual"], true);
    assert!(
        destination["module"]
            .as_str()
            .unwrap()
            .starts_with("__auto/atomic_groups/")
    );
}

#[test]
fn selected_and_omitted_anonymous_statements_follow_the_subset() {
    let source = "export const a = 1;\nconsole.log('selected');\nconsole.log('omitted');\n";
    let modules = vec![
        logical_module_with_anon("a", &[Member::new("a")], &["console.log('selected');"]),
        logical_module_with_anon("b", &[], &["console.log('omitted');"]),
    ];
    let subset = subset();
    let fixture = run_fixture_without_graphs_with_args(
        FixtureOpts::new(source, modules).with_unassigned_mode(unassigned_mode_inline()),
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual-atomic-groups",
        ],
    );
    assert_entry_output(&fixture, "selected\nomitted\n");
    let report = report(&fixture.report_root, &fixture.chunk_id);
    let lines: Vec<u64> = report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|group| group["source_units"].as_array().unwrap())
        .filter(|unit| unit["bindings"].as_array().unwrap().is_empty())
        .filter_map(|unit| unit["source_location"]["start_line"].as_u64())
        .collect();
    assert!(
        !lines.contains(&2),
        "selected anonymous statement appears residual"
    );
    assert!(lines.contains(&3), "omitted anonymous statement missing");
}

#[test]
fn rejected_split_atom_reports_both_destinations() {
    let source = "export let b = 2;\nb += 1;\n";
    let module = logical_module_with_anon("b", &[Member::new("b")], &["b += 1;"]);
    let subset = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        subset.path(),
        r#"{"modules":[{"chunk":"static/app","module":"b","anonymous_statements":[]}]}"#,
    )
    .unwrap();
    let fixture = run_rejection_fixture_without_graphs_with_args(
        FixtureOpts::new(source, vec![module]).with_unassigned_mode(unassigned_mode_inline()),
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual-atomic-groups",
        ],
        3,
    );
    let report = report(&fixture.report_root, &fixture.chunk_id);
    assert_eq!(report["gate_accepts"], false);
    let group = report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| has_binding(group, "b"))
        .unwrap();
    assert_eq!(group["fully_residual"], false);
    let units = group["source_units"].as_array().unwrap();
    assert!(
        units
            .iter()
            .any(|unit| unit["destination"]["residual"] == false)
    );
    assert!(
        units
            .iter()
            .any(|unit| unit["destination"]["residual"] == true)
    );
}

#[test]
fn boundary_examples_are_bounded_and_marked_when_truncated() {
    let source = "const a = 1, b = 2, c = 3, d = 4, e = 5, f = 6;\nconst sum = a + b + c + d + e + f;\nconsole.log(sum);\n";
    let subset = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(subset.path(), r#"{"modules":[]}"#).unwrap();
    let fixture = run_fixture_without_graphs_with_args(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
        &[
            "--ownership-subset",
            subset.path().to_str().unwrap(),
            "--full-report",
            "residual-atomic-groups",
        ],
    );
    assert_entry_output(&fixture, "21\n");
    let report = report(&fixture.report_root, &fixture.chunk_id);
    let group = report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| has_binding(group, "sum"))
        .unwrap();
    let edges = &group["outgoing"]["eager_use"];
    assert!(edges["total"].as_u64().unwrap() > 4);
    assert_eq!(edges["examples"].as_array().unwrap().len(), 4);
    assert_eq!(edges["truncated"], true);
}
