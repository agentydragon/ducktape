//! Constraint-driven construction must derive graph-determined peels without
//! exploring their power set, and repair import obstructions by co-location.

use std::fs;

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};

fn save(fixture: &GraphFixture, name: &str, value: &Value) -> String {
    let path = fixture.root().join(name);
    fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_owned()
}

fn apply_and_run(fixture: &GraphFixture, report: &Value, expected: &str) {
    assert_eq!(report["work"]["final_certifications"], 1, "{report}");
    assert_eq!(
        report["work"]["partition_states"], report["checks"],
        "{report}"
    );
    assert!(
        report["work"]["quotient_projections"].as_u64().unwrap()
            <= report["checks"].as_u64().unwrap(),
        "{report}"
    );
    let before = read_tree(&fixture.modules);
    let plan = save(fixture, "plan.json", &report["plan"]);
    fixture.assert_success(&["modules", "check", "--plan", &plan]);
    fixture.assert_success(&["modules", "apply", "--plan", &plan, "--dry-run"]);
    assert_eq!(read_tree(&fixture.modules), before);
    fixture.assert_success(&["modules", "apply", "--plan", &plan]);
    fixture.assert_runs(expected);
}

#[test]
fn a_large_required_chain_is_constructed_with_bounded_graph_work() {
    let mut source = "const value0 = 0;\n".to_owned();
    for index in 1..32 {
        source.push_str(&format!(
            "const value{index} = value{} + {index};\n",
            index - 1
        ));
    }
    source.push_str("console.log(value31);\n");
    let fixture = GraphFixture::new(&source, &[]);
    let required = fixture.source_unit_for_binding("value31");
    let report = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "new",
        "--max-new-modules",
        "1",
        "--require",
        &required,
        "--max-checks",
        "200",
    ]);
    assert_eq!(report["status"], "found", "{report}");
    assert!(
        report["residual_units_extracted"].as_u64().unwrap() >= 32,
        "{report}"
    );
    assert!(report["checks"].as_u64().unwrap() <= 100, "{report}");
    assert_eq!(report["existing_units_moved"], 0);
    apply_and_run(&fixture, &report, "496\n");
}

#[test]
fn a_large_fanout_needs_one_certified_witness() {
    let mut source = "const seed = 7;\n".to_owned();
    for index in 0..32 {
        source.push_str(&format!("const item{index} = seed + {index};\n"));
    }
    source.push_str("console.log(item0, item31);\n");
    let fixture = GraphFixture::new(&source, &[]);
    let report = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "new",
        "--max-new-modules",
        "1",
        "--max-checks",
        "200",
    ]);
    assert_eq!(report["status"], "found", "{report}");
    assert!(
        report["residual_units_extracted"].as_u64().unwrap() >= 33,
        "{report}"
    );
    assert!(report["checks"].as_u64().unwrap() <= 100, "{report}");
    apply_and_run(&fixture, &report, "7 38\n");
}

#[test]
fn lazy_import_star_requires_a_merge_even_though_eager_graph_is_acyclic() {
    let fixture = GraphFixture::new(
        "const base = {left: () => left, right: () => right};\n\
         const left = base;\n\
         const right = base;\n\
         console.log(left === base, right === base);\n",
        &[],
    );
    let units: Vec<_> = ["base", "left", "right"]
        .iter()
        .map(|binding| fixture.source_unit_for_binding(binding))
        .collect();
    let only = save(&fixture, "only.json", &json!(units));
    let finest = save(
        &fixture,
        "finest.json",
        &json!([
            {"unit_id": units[0], "module": "a_base"},
            {"unit_id": units[1], "module": "b_left"},
            {"unit_id": units[2], "module": "c_right"},
        ]),
    );
    let rejected = fixture.command(&["modules", "check", "--batch", &finest, "--format", "json"]);
    assert!(!rejected.status.success(), "{rejected:?}");
    let rejection = parse_stdout_json(&rejected);
    assert!(
        rejection["validation"]["cycles"]
            .as_array()
            .unwrap()
            .iter()
            .any(|cycle| cycle["rejection"] == "esm_evaluation_tdz"),
        "{rejection}"
    );

    let report = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "new",
        "--only-units",
        &only,
        "--require",
        &units[0],
        "--require",
        &units[1],
        "--require",
        &units[2],
        "--max-new-modules",
        "3",
    ]);
    assert_eq!(report["status"], "found", "{report}");
    assert_eq!(report["residual_units_extracted"], 3, "{report}");
    assert!(
        report["work"]["branch_clauses"]["schedule_closure"]
            .as_u64()
            .unwrap()
            > 0,
        "{report}"
    );
    let owners: std::collections::BTreeSet<_> = report["plan"]["changes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|change| change["module"].as_str().unwrap())
        .collect();
    assert!(
        owners.len() < 3,
        "intrinsically unschedulable finest split escaped: {report}"
    );
    apply_and_run(&fixture, &report, "true true\n");
}

#[test]
fn a_dependency_in_generated_residual_is_not_a_fixed_entry_blocker() {
    let fixture = GraphFixture::new(
        "const dep = 2;\nconst consumer = dep + 1;\nconsole.log(consumer);\n",
        &[],
    );
    let config_path = fixture.root().join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    config["unassigned_mode"]["main"] = serde_yaml::from_str("{kind: per_atomic_group}").unwrap();
    fs::write(&config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    fixture.refresh_graph();
    let baseline = fixture.dependency_graph();
    let consumer = fixture.source_unit_for_binding("consumer");
    let only = save(&fixture, "only.json", &json!([consumer]));
    let report = fixture.json(&[
        "modules",
        "propose",
        "--only-units",
        &only,
        "--require",
        &consumer,
        "--into",
        "extracted_consumer",
    ]);
    assert_eq!(report["status"], "found", "{report}");
    assert_eq!(report["residual_units_extracted"], 1, "{report}");
    apply_and_run(&fixture, &report, "3\n");
    fixture.refresh_graph();
    let actual = fixture.dependency_graph();
    // Generated filenames enumerate the remaining groups and can renumber.
    // Preserve each omitted group's identity up to a label bijection, as well
    // as its unassigned provenance; authored owner names must remain exact.
    let mut residual_labels = std::collections::BTreeMap::new();
    let mut used_labels = std::collections::BTreeSet::new();
    for node in &actual.nodes {
        let destination = actual.module(&node.destination).unwrap();
        let owner = destination.path.as_str();
        if node.id == consumer {
            assert_eq!(owner, "extracted_consumer");
        } else {
            let previous = baseline.nodes.iter().find(|old| old.id == node.id).unwrap();
            let original = baseline.module(&previous.destination).unwrap();
            assert_eq!(destination.kind, original.kind);
            if owner.starts_with("__auto/") {
                if let Some(mapped) = residual_labels.get(original.path.as_str()) {
                    assert_eq!(*mapped, owner);
                } else {
                    assert!(used_labels.insert(owner));
                    residual_labels.insert(original.path.as_str(), owner);
                }
            } else {
                assert_eq!(owner, original.path.as_str());
            }
        }
    }
}
