//! Checked source-unit batches realize the exact complete assignment.

use super::support::{apply_args_with_order, import_order_file};
use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{
    VariableDeclarationKind, VariableInitializerKind, assert_module_variable_declarators,
    parse_stdout_json, process, read_tree,
};
use serde_json::{Value, json};
use std::fs;
use std::path::Path;

fn batch(fixture: &GraphFixture, changes: Value) -> String {
    let path = fixture.root().join("assignments.json");
    fs::write(&path, serde_json::to_vec(&changes).unwrap()).unwrap();
    path.to_str().unwrap().to_string()
}

fn check_graph(fixture: &GraphFixture, assignments: &str) -> std::process::Output {
    let order = import_order_file(fixture, Path::new(assignments));
    fixture.command(&[
        "modules",
        "check",
        "--graph-only",
        "--batch",
        assignments,
        "--import-order",
        order.to_str().unwrap(),
        "--format",
        "json",
    ])
}

fn apply(
    fixture: &GraphFixture,
    assignments: &str,
    dry_run: bool,
    extra: &[&str],
) -> std::process::Output {
    let order = import_order_file(fixture, Path::new(assignments));
    apply_with_order(fixture, Path::new(assignments), &order, dry_run, extra)
}

fn apply_with_order(
    fixture: &GraphFixture,
    assignments: &Path,
    order: &Path,
    dry_run: bool,
    extra: &[&str],
) -> std::process::Output {
    let mut args = apply_args_with_order(fixture, assignments, order);
    if dry_run {
        args.push("--dry-run".to_owned());
    }
    args.extend(extra.iter().map(|arg| (*arg).to_owned()));
    fixture.command(&args.iter().map(String::as_str).collect::<Vec<_>>())
}

fn assert_rejected(fixture: &GraphFixture, assignments: &str, diagnostics: &[&str]) {
    let assignments = Path::new(assignments);
    let order = import_order_file(fixture, assignments);
    let args = apply_args_with_order(fixture, assignments, &order);
    fixture.assert_rejected_unchanged(
        &args.iter().map(String::as_str).collect::<Vec<_>>(),
        diagnostics,
    );
}

#[test]
fn graph_accepted_duplicate_literal_binding_has_no_unique_selector_and_writes_nothing() {
    // Shrunk from the complete-search property run: graph acceptance does not
    // promise a uniquely addressable structural selector for either `0`.
    let fixture = GraphFixture::new(
        "const anchor = 0;\nconst first = 0;\nconst second = first + 1;\nconst third = first + 1;\nconsole.log(first, second, third);\n",
        &[(
            "middle.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let path = batch(
        &fixture,
        json!([
            {"unit_id": fixture.source_unit_for_binding("first"), "module": "a_before"},
        ]),
    );
    assert!(check_graph(&fixture, &path).status.success());
    assert_rejected(
        &fixture,
        &path,
        &["no uniquely proven structural selector", "ambiguous"],
    );
    // The concrete graph assignment is valid through the normal pipeline when
    // an author supplies an explicit selector instead of asking for synthesis.
    fs::write(
        fixture.modules.join("a_before.yaml"),
        "members: [{selector: {binding: {name: first}}}]\n",
    )
    .unwrap();
    let config_path = fixture.root().join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    config["import_order"]["main"] = serde_yaml::to_value(["middle", "a_before"]).unwrap();
    fs::write(config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    fixture.assert_runs("0 1 1\n");
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
    let [dep, consumer, count] = fixture.source_units_for_bindings(["dep", "consumer", "count"]);
    let write = fixture.source_unit_for_line(5);
    let before = read_tree(&fixture.modules);
    for changes in [
        json!([{"unit_id": consumer, "module": "chain"}]),
        json!([{"unit_id": count, "module": "counter"}]),
    ] {
        let path = batch(&fixture, changes);
        assert_rejected(&fixture, &path, &["complete assignment gate rejected"]);
    }
    let changes = json!([
        {"unit_id": dep, "module": "chain"},
        {"unit_id": consumer, "module": "chain"},
        {"unit_id": count, "module": "counter"},
        {"unit_id": write, "module": "counter"},
    ]);
    let path = batch(&fixture, changes.clone());
    assert!(check_graph(&fixture, &path).status.success());
    let preview = parse_stdout_json(&apply(&fixture, &path, true, &[]));
    assert_eq!(preview["residual_source_units_claimed"], 4);
    assert_eq!(preview["source_units_reassigned"], 0);
    assert_eq!(read_tree(&fixture.modules), before);
    let applied = parse_stdout_json(&apply(&fixture, &path, false, &[]));
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
    let [anchor, remaining] = fixture.source_units_for_bindings(["anchor", "remaining"]);
    let path = batch(
        &fixture,
        json!([
            {"unit_id": anchor, "module": "destination"},
            {"unit_id": remaining, "module": "destination"},
        ]),
    );
    let names = fixture.root().join("names.json");
    fs::write(&names, "{\"remaining\":\"readableRemaining\"}").unwrap();
    let result = parse_stdout_json(&apply(
        &fixture,
        &path,
        false,
        &["--readable-names", names.to_str().unwrap()],
    ));
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
    assert_rejected(&fixture, &path, &["graph compatibility mismatch"]);
}

#[test]
fn readable_name_may_match_selector_keyword_without_changing_runtime_binding() {
    let fixture = GraphFixture::new(
        "const value = 41, read = () => value;\nconsole.log(read() + 1);\n",
        &[],
    );
    let [value, read] = fixture.source_units_for_bindings(["value", "read"]);
    let path = batch(
        &fixture,
        json!([
            {"unit_id": value, "module": "values/value"},
            {"unit_id": read, "module": "values/value"},
        ]),
    );
    let names = fixture.root().join("names.json");
    fs::write(&names, "{\"value\":\"ANYTHING\",\"read\":\"read\"}").unwrap();
    let result = parse_stdout_json(&apply(
        &fixture,
        &path,
        false,
        &["--readable-names", names.to_str().unwrap()],
    ));
    assert_eq!(result["action"], "applied");

    let emitted = fixture.rerun();
    assert_module_variable_declarators(
        emitted.path(),
        "app/main/values/value.js",
        &[
            (
                VariableDeclarationKind::Const,
                &["ANYTHING"],
                VariableInitializerKind::Number,
            ),
            (
                VariableDeclarationKind::Const,
                &["read"],
                VariableInitializerKind::Arrow,
            ),
        ],
    );
    debundle_e2e_support::assert_node_output(&emitted.path().join("app/main/entry.js"), "42\n", "");
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
    assert!(check_graph(&fixture, &path).status.success());
    assert_rejected(&fixture, &path, &["no unique structural selector"]);
}

#[test]
fn generated_residual_dependency_can_remain_in_its_actual_module() {
    let fixture = GraphFixture::per_atomic_group(
        "const dep = 2;\nconst consumer = dep + 1;\nconsole.log(consumer);\n",
        &[],
    );
    let changes = json!([{
        "unit_id": fixture.source_unit_for_binding("consumer"),
        "module": "consumer"
    }]);
    let path = batch(&fixture, changes.clone());
    let checked = check_graph(&fixture, &path);
    process::assert_success(&checked);

    // The old priority still names the later generated group by its baseline
    // index. Materialization compacts that group after the middle group moves,
    // so explicit check/apply must reject the stale witness without writing.
    let report = fixture.dependency_graph();
    let moved_unit = fixture.source_unit_for_binding("consumer");
    let moved_node = report
        .nodes
        .iter()
        .find(|node| node.id == moved_unit)
        .unwrap();
    let moved_owner = report.module(&moved_node.destination).unwrap().path.clone();
    let stale_priority: Vec<_> = report
        .import_order
        .iter()
        .filter(|module| *module != &moved_owner)
        .map(ToString::to_string)
        .chain(std::iter::once("consumer".to_owned()))
        .collect();
    let stale_order = fixture.root().join("stale-import-order.json");
    fs::write(&stale_order, serde_json::to_vec(&stale_priority).unwrap()).unwrap();
    let stale_order = stale_order.to_str().unwrap();
    let before = read_tree(&fixture.modules);
    let stale_check = fixture.command(&[
        "modules",
        "check",
        "--batch",
        &path,
        "--import-order",
        stale_order,
        "--format",
        "json",
    ]);
    assert!(!stale_check.status.success());
    assert!(
        String::from_utf8_lossy(&stale_check.stderr)
            .contains("import priority must contain every emitted nonentry module exactly once"),
        "{}",
        String::from_utf8_lossy(&stale_check.stderr)
    );
    assert_eq!(read_tree(&fixture.modules), before);
    let config_path = fixture.root().join("spec_config.yaml");
    let config_before = fs::read(&config_path).unwrap();
    let stale_args = apply_args_with_order(&fixture, Path::new(&path), Path::new(stale_order));
    fixture.assert_rejected_unchanged(
        &stale_args.iter().map(String::as_str).collect::<Vec<_>>(),
        &["import priority must contain every emitted nonentry module exactly once"],
    );
    assert_eq!(fs::read(config_path).unwrap(), config_before);

    let before = read_tree(&fixture.modules);
    let preview = apply(&fixture, &path, true, &[]);
    process::assert_success(&preview);
    assert_eq!(read_tree(&fixture.modules), before);
    let result = apply(&fixture, &path, false, &[]);
    process::assert_success(&result);
    assert_eq!(
        parse_stdout_json(&result)["residual_source_units_claimed"],
        1
    );
    fixture.assert_runs("3\n");
}
