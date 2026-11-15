//! Explicit batches are checked against the whole accepted assignment.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};
use std::fs;

fn check(fixture: &GraphFixture, batch: Value, accepted: bool) -> Value {
    let before = read_tree(&fixture.modules);
    let path = fixture.root().join("check.json");
    fs::write(&path, serde_json::to_vec(&batch).unwrap()).unwrap();
    let output = fixture.command(&[
        "modules",
        "check",
        "--batch",
        path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(
        output.status.code(),
        Some(if accepted { 0 } else { 1 }),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = parse_stdout_json(&output);
    assert_eq!(result["accepted"], accepted, "{result}");
    assert_eq!(
        read_tree(&fixture.modules),
        before,
        "read-only check modified the spec"
    );
    result
}

#[test]
fn retains_an_accepted_three_unit_atom_without_pairwise_reconstruction() {
    let fixture = GraphFixture::new(
        "let first = 0;\nlet second = 0;\nfunction update() { first += 1; second += 1; }\nconsole.log(first, second);\nexport { update };\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: second}}}, {selector: {binding: {name: update}}}]\n",
        )],
    );
    let result = check(&fixture, json!([]), true);
    assert_eq!(result["residual_units_extracted"], 0);
    assert_eq!(result["existing_units_moved"], 0);
    let first = fixture.source_unit_for_binding("first");
    let split = check(
        &fixture,
        json!([{"unit_id": first, "module": "other"}]),
        false,
    );
    assert!(
        !split["validation"]["atomic_group_conflicts"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn checks_residual_dependencies_and_their_joint_move() {
    let fixture = GraphFixture::new(
        "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nexport { anchor, consumer };\n",
        &[(
            "anchor.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let consumer = fixture.source_unit_for_binding("consumer");
    let dep = fixture.source_unit_for_binding("dep");
    check(
        &fixture,
        json!([{"unit_id": consumer, "module": "chain"}]),
        false,
    );
    let joint = check(
        &fixture,
        json!([
            {"unit_id": consumer, "module": "chain"},
            {"unit_id": dep, "module": "chain"},
        ]),
        true,
    );
    assert_eq!(joint["residual_units_extracted"], 2);
    assert_eq!(joint["existing_units_moved"], 0);
}

#[test]
fn checks_the_actual_existing_destination_and_complete_batch() {
    let fixture = GraphFixture::dependency_chain();
    let alpha = fixture.source_unit_for_binding("alpha");
    let gamma = fixture.source_unit_for_binding("gamma");
    check(&fixture, json!([{"unit_id": alpha, "module": "b"}]), false);
    let joint = check(
        &fixture,
        json!([
            {"unit_id": alpha, "module": "b"},
            {"unit_id": gamma, "module": "b"},
        ]),
        true,
    );
    assert_eq!(joint["existing_units_moved"], 2);
}

#[test]
fn supports_anonymous_units_in_a_whole_atom() {
    let fixture = GraphFixture::new("let value = 0;\nvalue += 1;\nconsole.log(value);\n", &[]);
    let value = fixture.source_unit_for_binding("value");
    let write = fixture.source_unit_for_line(2);
    check(
        &fixture,
        json!([{"unit_id": value, "module": "counter"}]),
        false,
    );
    let joint = check(
        &fixture,
        json!([
            {"unit_id": value, "module": "counter"},
            {"unit_id": write, "module": "counter"},
        ]),
        true,
    );
    assert_eq!(joint["residual_units_extracted"], 2);
}

#[test]
fn preserves_distinct_generated_residual_destinations() {
    let fixture = GraphFixture::new(
        "const dep = 2;\nconst consumer = dep + 1;\nexport { consumer };\n",
        &[],
    );
    let path = fixture.root().join("spec_config.yaml");
    let mut config: serde_yaml::Value = serde_yaml::from_slice(&fs::read(&path).unwrap()).unwrap();
    config["unassigned_mode"]["main"] = serde_yaml::from_str("{kind: per_atomic_group}").unwrap();
    fs::write(&path, serde_yaml::to_string(&config).unwrap()).unwrap();
    fixture.refresh_graph();
    check(&fixture, json!([]), true);
    let consumer = fixture.source_unit_for_binding("consumer");
    let result = check(
        &fixture,
        json!([{"unit_id": consumer, "module": "consumer"}]),
        true,
    );
    assert_eq!(result["residual_units_extracted"], 1);
    assert_eq!(result["existing_units_moved"], 0);
}

#[test]
fn malformed_changes_and_scaffolding_are_refused_without_spec_edits() {
    let fixture = GraphFixture::new("const value = 1;\nexport { value };\n", &[]);
    let value = fixture.source_unit_for_binding("value");
    let export = fixture.source_unit_for_line(2);
    let before = read_tree(&fixture.modules);
    for batch in [
        json!([{"unit_id": "unit:999999", "module": "new"}]),
        json!([{"unit_id": value, "module": "first"}, {"unit_id": value, "module": "second"}]),
        json!([{"unit_id": value, "module": "/absolute"}]),
        json!([{"unit_id": value, "module": "destination.YAML"}]),
        json!([{"unit_id": export, "module": "new"}]),
    ] {
        let path = fixture.root().join("invalid.json");
        fs::write(&path, serde_json::to_vec(&batch).unwrap()).unwrap();
        let output = fixture.command(&[
            "modules",
            "check",
            "--batch",
            path.to_str().unwrap(),
            "--format",
            "json",
        ]);
        assert_eq!(
            output.status.code(),
            Some(1),
            "batch={batch}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(read_tree(&fixture.modules), before);
    }
}
