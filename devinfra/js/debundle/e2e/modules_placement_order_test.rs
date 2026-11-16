//! Exact assignment edits persist the complete import priority with ownership.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::json;
use std::fs;

#[test]
fn new_owner_requires_and_atomically_persists_an_explicit_priority() {
    let fixture = GraphFixture::new(
        "const anchor = 'alpha';\nconst value = anchor + '-value';\nconsole.log(value);\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let unit = fixture.source_unit_for_binding("value");
    let changes_path = fixture.root().join("changes.json");
    fs::write(
        &changes_path,
        serde_json::to_vec(&json!([{"unit_id": unit, "module": "fresh"}])).unwrap(),
    )
    .unwrap();
    let order = fixture.root().join("order.json");
    fs::write(
        &order,
        serde_json::to_vec(&json!(["home", "fresh"])).unwrap(),
    )
    .unwrap();
    let physical_changes = fixture.write_physical_assignment_batch(
        "physical-changes.json",
        &json!([{"unit_id": unit, "module": "fresh"}]),
    );
    let tree_config = fixture.root().join("spec_config.yaml");
    let before = read_tree(&fixture.modules);

    // An ownership batch cannot invent a schedule when it creates a new owner.
    let missing = fixture.command(&[
        "modules",
        "check",
        "--batch",
        changes_path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(!missing.status.success(), "{missing:?}");
    assert_eq!(read_tree(&fixture.modules), before);

    let checked = fixture.command(&[
        "modules",
        "check",
        "--batch",
        physical_changes.to_str().unwrap(),
        "--import-order",
        order.to_str().unwrap(),
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(checked.status.success(), "{checked:?}");
    let report = parse_stdout_json(&checked);
    assert_eq!(report["accepted"], true, "{report}");
    assert_eq!(report["plan"]["import_order"], json!(["home", "fresh"]));

    fixture.assert_success(&[
        "modules",
        "apply",
        "--assignments",
        physical_changes.to_str().unwrap(),
        "--modules",
        fixture.root().join("modules").to_str().unwrap(),
        "--import-order",
        order.to_str().unwrap(),
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--dry-run",
    ]);
    assert_eq!(read_tree(&fixture.modules), before);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--assignments",
        physical_changes.to_str().unwrap(),
        "--modules",
        fixture.root().join("modules").to_str().unwrap(),
        "--import-order",
        order.to_str().unwrap(),
        "--tree-config",
        tree_config.to_str().unwrap(),
    ]);

    let config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&tree_config).unwrap()).unwrap();
    let import_order: Vec<String> =
        serde_yaml::from_value(config["import_order"]["main"].clone()).unwrap();
    assert_eq!(import_order, ["home", "fresh"]);
    fixture.assert_runs("alpha-value\n");
}
