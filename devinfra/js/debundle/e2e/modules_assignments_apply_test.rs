//! Checked source-unit batches realize the exact complete assignment.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};
use std::fs;

fn batch(fixture: &GraphFixture, changes: Value) -> String {
    let path = fixture.root().join("assignments.json");
    fs::write(&path, serde_json::to_vec(&changes).unwrap()).unwrap();
    path.to_str().unwrap().to_string()
}

#[test]
fn joint_multi_destination_batch_claims_anonymous_units_and_preserves_omitted_units() {
    let fixture = GraphFixture::new(
        "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nlet count = 0;\ncount += 1;\nconsole.log(anchor, consumer, count);\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let dep = fixture.source_unit_for_binding("dep");
    let consumer = fixture.source_unit_for_binding("consumer");
    let count = fixture.source_unit_for_binding("count");
    let write = fixture.source_unit_for_line(5);
    let before = read_tree(&fixture.modules);
    for changes in [
        json!([{"unit_id": consumer, "module": "chain"}]),
        json!([{"unit_id": count, "module": "counter"}]),
    ] {
        let path = batch(&fixture, changes);
        fixture.assert_rejected_unchanged(
            &["modules", "apply", "--assignments", &path],
            &["complete assignment gate rejected"],
        );
    }
    let changes = json!([
        {"unit_id": dep, "module": "chain"},
        {"unit_id": consumer, "module": "chain"},
        {"unit_id": count, "module": "counter"},
        {"unit_id": write, "module": "counter"},
    ]);
    let path = batch(&fixture, changes.clone());
    fixture.assert_success(&["modules", "check", "--batch", &path]);
    let preview = fixture.json(&["modules", "apply", "--assignments", &path, "--dry-run"]);
    assert_eq!(preview["residual_source_units_claimed"], 4);
    assert_eq!(preview["source_units_reassigned"], 0);
    assert_eq!(read_tree(&fixture.modules), before);
    let applied = fixture.json(&["modules", "apply", "--assignments", &path]);
    assert_eq!(preview["synthesized_claims"], applied["synthesized_claims"]);
    assert_eq!(preview["files_written"], applied["files_written"]);
    fixture.assert_runs("1 3 1\n");
    let old = fixture.dependency_graph();
    fixture.refresh_graph();
    let actual = fixture.dependency_graph();
    for node in &actual.nodes {
        let path = actual.module(&node.destination).unwrap().path.as_str();
        let requested = changes
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["unit_id"] == node.id);
        if let Some(requested) = requested {
            assert_eq!(path, requested["module"].as_str().unwrap());
        } else {
            let previous = old.nodes.iter().find(|old| old.id == node.id).unwrap();
            assert_eq!(
                path,
                old.module(&previous.destination).unwrap().path.as_str()
            );
        }
    }
}

#[test]
fn moves_existing_metadata_while_claiming_residual_and_keeps_empty_owner_shell() {
    let fixture = GraphFixture::new(
        "const anchor = 1;\nconst remaining = 2;\nconsole.log(anchor, remaining);\n",
        &[(
            "home.yaml",
            "comment: Home module\nnote: Keep this shell\nmembers: [{name: readableAnchor, selector: {binding: {name: anchor}}}]\nannotations:\n  readableAnchor:\n    comment: Anchor documentation\n    note: Anchor note\n",
        )],
    );
    let path = batch(
        &fixture,
        json!([
            {"unit_id": fixture.source_unit_for_binding("anchor"), "module": "destination"},
            {"unit_id": fixture.source_unit_for_binding("remaining"), "module": "destination"},
        ]),
    );
    let names = fixture.root().join("names.json");
    fs::write(&names, "{\"remaining\":\"readableRemaining\"}").unwrap();
    let result = fixture.json(&[
        "modules",
        "apply",
        "--assignments",
        &path,
        "--readable-names",
        names.to_str().unwrap(),
    ]);
    assert_eq!(result["source_units_reassigned"], 1);
    assert_eq!(result["residual_source_units_claimed"], 1);
    let home: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("home.yaml")).unwrap()).unwrap();
    assert_eq!(home["comment"], "Home module");
    assert_eq!(home["note"], "Keep this shell");
    let destination: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("destination.yaml")).unwrap())
            .unwrap();
    assert_eq!(
        destination["annotations"]["readableAnchor"]["comment"],
        "Anchor documentation"
    );
    assert_eq!(
        destination["annotations"]["readableAnchor"]["note"],
        "Anchor note"
    );
    fixture.assert_runs("1 2\n");
    // An applied edit invalidates the saved baseline. Neither preview nor write
    // may silently replay against this stale graph.
    fixture.assert_rejected_unchanged(
        &["modules", "apply", "--assignments", &path],
        &["graph compatibility mismatch"],
    );
}

#[test]
fn selector_realization_failure_leaves_the_spec_unchanged() {
    let fixture = GraphFixture::new("console.log('same');\nconsole.log('same');\n", &[]);
    let path = batch(
        &fixture,
        json!([
            {"unit_id": fixture.source_unit_for_line(1), "module": "effects"},
            {"unit_id": fixture.source_unit_for_line(2), "module": "effects"},
        ]),
    );
    fixture.assert_success(&["modules", "check", "--batch", &path]);
    fixture.assert_rejected_unchanged(
        &["modules", "apply", "--assignments", &path],
        &["no unique structural selector"],
    );
}

#[test]
fn generated_residual_dependency_can_remain_in_its_actual_module() {
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
    let path = batch(
        &fixture,
        json!([{"unit_id": fixture.source_unit_for_binding("consumer"), "module": "consumer"}]),
    );
    fixture.assert_success(&["modules", "check", "--batch", &path]);
    let result = fixture.command(&[
        "modules",
        "apply",
        "--assignments",
        &path,
        "--format",
        "json",
    ]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        parse_stdout_json(&result)["residual_source_units_claimed"],
        1
    );
    fixture.assert_runs("3\n");
}
