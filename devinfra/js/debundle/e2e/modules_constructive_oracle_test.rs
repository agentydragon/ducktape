//! Exhaust the tiny declared append-one-owner family with exact CLI checks.
//!
//! The oracle enumerates source-unit subsets directly and asks the independent
//! complete assignment checker under the explicit old-priority-plus-new-owner
//! order. It does not use proposal closures, SCC summaries, or scheduling code.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::fs;

fn write_json(fixture: &GraphFixture, name: &str, value: &Value) -> String {
    let path = fixture.root().join(name);
    fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_owned()
}

#[test]
fn seed_feasibility_matches_every_raw_subset_under_the_append_last_priority() {
    let source = "const anchor = 99;\nconst first = 1;\nconst second = first + 10;\nconst third = second + 100;\nconsole.log(anchor, first, second, third);\n";
    let fixture = GraphFixture::new(
        source,
        &[(
            "middle.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let import_order: Vec<_> = fixture
        .dependency_graph()
        .import_order
        .into_iter()
        .map(|module| module.to_string())
        .collect();
    assert_eq!(
        import_order,
        vec!["middle".to_owned()],
        "GraphFixture::new should use authored module-file order"
    );
    let baseline = read_tree(&fixture.modules);
    let units = fixture.source_units_for_bindings(["first", "second", "third"]);
    let only = write_json(&fixture, "only.json", &json!(units));
    let old_order = write_json(&fixture, "old-order.json", &json!(["middle"]));
    let appended_order = write_json(&fixture, "appended-order.json", &json!(["middle", "fresh"]));
    let tree_config = fixture.root().join("spec_config.yaml");

    // Each bit says whether this exact source unit is assigned to the one new
    // owner. The ordinary check command is the sole acceptance oracle.
    let mut accepted_masks = BTreeSet::new();
    for mask in 0u8..(1 << units.len()) {
        let changes: Vec<_> = units
            .iter()
            .enumerate()
            .filter(|(index, _)| mask & (1 << index) != 0)
            .map(|(_, unit_id)| json!({"unit_id": unit_id, "module": "fresh"}))
            .collect();
        let batch = write_json(&fixture, &format!("batch-{mask}.json"), &json!(changes));
        let order = if mask == 0 {
            &old_order
        } else {
            &appended_order
        };
        let output = fixture.command(&[
            "modules",
            "check",
            "--graph-only",
            "--batch",
            &batch,
            "--import-order",
            order,
            "--format",
            "json",
        ]);
        let report = parse_stdout_json(&output);
        assert_eq!(
            report["accepted"].as_bool(),
            Some(output.status.success()),
            "mask={mask:03b}: {report}"
        );
        if output.status.success() {
            accepted_masks.insert(mask);
        }
        assert_eq!(
            read_tree(&fixture.modules),
            baseline,
            "check wrote spec for mask={mask:03b}"
        );
    }

    let proposal = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--into",
        "fresh",
        "--only-units",
        &only,
    ]);
    let seed_rows = proposal["seed_atoms"].as_array().unwrap();
    for row in seed_rows {
        let row_units: Vec<_> = row["source_units"]
            .as_array()
            .unwrap()
            .iter()
            .map(|unit| unit.as_str().unwrap())
            .collect();
        let required_mask = row_units.iter().fold(0u8, |mask, unit| {
            let index = units
                .iter()
                .position(|candidate| candidate == unit)
                .unwrap();
            mask | (1 << index)
        });
        let expected_feasible = accepted_masks
            .iter()
            .any(|mask| mask & required_mask == required_mask && *mask != 0);
        assert_eq!(
            row["feasible"].as_bool(),
            Some(expected_feasible),
            "seed={row_units:?} oracle={accepted_masks:?}: {proposal}"
        );
    }
    assert_eq!(proposal["status"], "found", "{proposal}");
    let plan = &proposal["plan"];
    assert_eq!(plan["import_order"][0], "middle");
    assert_eq!(plan["import_order"][1], "fresh");
    let plan_mask = plan["changes"]
        .as_array()
        .unwrap()
        .iter()
        .fold(0u8, |mask, change| {
            let unit = change["unit_id"].as_str().unwrap();
            let index = units
                .iter()
                .position(|candidate| candidate == unit)
                .unwrap();
            assert_eq!(change["module"], "fresh");
            mask | (1 << index)
        });
    assert!(
        accepted_masks.contains(&plan_mask),
        "plan outside exact oracle: {plan}"
    );

    let plan_path = write_json(&fixture, "plan.json", plan);
    fixture.assert_success(&[
        "modules",
        "check",
        "--plan",
        &plan_path,
        "--tree-config",
        tree_config.to_str().unwrap(),
    ]);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        &plan_path,
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--dry-run",
    ]);
    assert_eq!(read_tree(&fixture.modules), baseline);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        &plan_path,
        "--tree-config",
        tree_config.to_str().unwrap(),
    ]);
    fixture.assert_runs("99 1 11 111\n");
}
