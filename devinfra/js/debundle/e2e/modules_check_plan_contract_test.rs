//! modules check returns an executable plan only after source preflight.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{
    TreeFixture, debundle_fixture_output, parse_stdout_json, process, read_tree,
    run_tree_graph_fixture,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;

fn duplicate_initializer_case() -> (GraphFixture, Value) {
    let fixture = GraphFixture::new(
        "const anchor = 0;\nconst first = 0;\nconst second = first + 1;\nconst third = first + 1;\nconsole.log(first, second, third);\n",
        &[(
            "middle.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let changes = json!([{
        "unit_id": fixture.source_unit_for_binding("first"),
        "module": "a_before",
    }]);
    (fixture, changes)
}

fn write_batch(fixture: &GraphFixture, name: &str, changes: &Value) -> (PathBuf, PathBuf) {
    let batch = fixture.write_physical_assignment_batch(&format!("{name}-batch.json"), changes);
    let order = fixture.write_assignment_import_order(&format!("{name}-order.json"), changes);
    (batch, order)
}

fn run_check(
    fixture: &GraphFixture,
    batch: &Path,
    order: &Path,
    graph_only: bool,
    source_root: bool,
    print_invocation: bool,
) -> Output {
    let mut args = vec![
        "modules".to_owned(),
        "check".to_owned(),
        "--batch".to_owned(),
        batch.display().to_string(),
        "--import-order".to_owned(),
        order.display().to_string(),
        "--format".to_owned(),
        "json".to_owned(),
        "--tree-config".to_owned(),
        fixture.tree_config().display().to_string(),
    ];
    if graph_only {
        args.push("--graph-only".to_owned());
    }
    if print_invocation {
        args.push("--print-invocation".to_owned());
    }
    let args = args.iter().map(String::as_str).collect::<Vec<_>>();
    let mut command = fixture.process(&args);
    if !source_root {
        command.env_remove("DEBUNDLE_SOURCE_ROOT");
    }
    debundle_fixture_output(&mut command, fixture.root()).expect("run modules check")
}

fn check_plan(fixture: &GraphFixture, plan: &Path, tree_config: &Path) -> Output {
    fixture.command(&[
        "modules",
        "check",
        "--plan",
        plan.to_str().unwrap(),
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--format",
        "json",
    ])
}

#[derive(Debug, PartialEq, Eq)]
struct InputSnapshot {
    modules: BTreeMap<PathBuf, Vec<u8>>,
    config: Vec<u8>,
    graph: Vec<u8>,
    source: Vec<u8>,
    batch: Vec<u8>,
    order: Vec<u8>,
}

fn input_snapshot(fixture: &GraphFixture, batch: &Path, order: &Path) -> InputSnapshot {
    InputSnapshot {
        modules: read_tree(&fixture.modules),
        config: fs::read(fixture.root().join("spec_config.yaml")).unwrap(),
        graph: fs::read(&fixture.graph).unwrap(),
        source: fs::read(fixture.source_path()).unwrap(),
        batch: fs::read(batch).unwrap(),
        order: fs::read(order).unwrap(),
    }
}

fn assert_owner(fixture: &GraphFixture, binding: &str, expected: &str) {
    let graph = fixture.dependency_graph();
    let unit_id = fixture.source_unit_for_binding(binding);
    let node = graph
        .nodes
        .iter()
        .find(|node| node.id == unit_id)
        .unwrap_or_else(|| panic!("graph has no source unit for {binding}"));
    let actual = graph
        .module(&node.destination)
        .expect("source unit destination is in the module graph")
        .path
        .as_str();
    assert_eq!(actual, expected, "owner for source binding {binding}");
}

#[test]
fn graph_only_keeps_graph_acceptance_separate_from_selector_realization() {
    let (fixture, changes) = duplicate_initializer_case();
    let (batch, order) = write_batch(&fixture, "ambiguous", &changes);
    let before = input_snapshot(&fixture, &batch, &order);

    let default = run_check(&fixture, &batch, &order, false, true, false);
    assert!(
        !default.status.success(),
        "a graph-valid batch with ambiguous selectors received an executable plan: {}",
        String::from_utf8_lossy(&default.stdout)
    );
    let default = parse_stdout_json(&default);
    assert_eq!(default["scope"], "executable_plan");
    assert_eq!(default["accepted"], false, "{default}");
    assert_eq!(default["graph_accepted"], true, "{default}");
    assert!(default["validation"].is_object(), "{default}");
    assert!(default["plan"].is_null(), "{default}");
    let selector_error = default["selector_realization_error"]
        .as_str()
        .expect("default writer failure is reported separately");
    assert!(
        selector_error.contains("unique") || selector_error.contains("ambiguous"),
        "{selector_error}"
    );
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);

    let default_invocation = run_check(&fixture, &batch, &order, false, true, true);
    assert!(default_invocation.status.success());
    assert_eq!(
        parse_stdout_json(&default_invocation)["requested_proof"],
        "executable_plan_check_requested"
    );
    let graph_only_invocation = run_check(&fixture, &batch, &order, true, true, true);
    assert!(graph_only_invocation.status.success());
    assert_eq!(
        parse_stdout_json(&graph_only_invocation)["requested_proof"],
        "graph_snapshot_input"
    );

    let graph_only = run_check(&fixture, &batch, &order, true, true, false);
    process::assert_success(&graph_only);
    let graph_only = parse_stdout_json(&graph_only);
    assert_eq!(graph_only["scope"], "graph_only");
    assert_eq!(graph_only["accepted"], true, "{graph_only}");
    assert_eq!(graph_only["graph_accepted"], true, "{graph_only}");
    assert!(graph_only["validation"].is_object(), "{graph_only}");
    assert!(graph_only["plan"].is_null(), "{graph_only}");
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);
}

#[test]
fn executable_check_plan_previews_applies_and_runs_with_tree_paths() {
    let source = "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer);\n";
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", source)],
            module_roots: &[("app", "main")],
            modules: &[(
                "app/home.yaml",
                "members: [{selector: {binding: {name: anchor}}}]\n",
            )],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let fixture = GraphFixture::from_tree_run(run, "modules");
    let changes = json!([
        {"unit_id": fixture.source_unit_for_binding("dep"), "module": "chain"},
        {"unit_id": fixture.source_unit_for_binding("consumer"), "module": "chain"},
    ]);
    let (batch, order) = write_batch(&fixture, "chain", &changes);
    let config = fixture.root().join("spec_config.yaml");
    let modules_before = read_tree(&fixture.modules);
    let config_before = fs::read(&config).unwrap();

    let checked = run_check(&fixture, &batch, &order, false, true, false);
    process::assert_success(&checked);
    let checked = parse_stdout_json(&checked);
    assert_eq!(checked["scope"], "executable_plan");
    assert_eq!(checked["accepted"], true, "{checked}");
    assert_eq!(checked["graph_accepted"], true, "{checked}");
    assert_eq!(checked["residual_units_extracted"], 2, "{checked}");
    assert!(checked["validation"].is_object(), "{checked}");
    let plan = checked["plan"].clone();
    assert!(plan.is_object(), "{checked}");
    assert_eq!(plan["import_order"], json!(["home", "chain"]), "{plan}");
    assert!(
        plan["changes"]
            .as_array()
            .unwrap()
            .iter()
            .all(|change| change["module"] == "chain"),
        "plan destinations are chunk-logical paths: {plan}"
    );
    assert!(
        serde_json::from_slice::<Value>(&fs::read(&batch).unwrap()).unwrap()[0]["module"]
            .as_str()
            .unwrap()
            .starts_with("app/"),
        "full-tree batch destinations use physical YAML-root paths"
    );
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&config).unwrap(), config_before);

    let plan_path = fixture.root().join("checked-plan.json");
    fs::write(&plan_path, serde_json::to_vec_pretty(&plan).unwrap()).unwrap();
    let rechecked = check_plan(&fixture, &plan_path, &config);
    process::assert_success(&rechecked);
    let rechecked = parse_stdout_json(&rechecked);
    assert_eq!(rechecked["scope"], "executable_plan");
    assert_eq!(rechecked["accepted"], true, "{rechecked}");
    assert_eq!(rechecked["graph_accepted"], true, "{rechecked}");
    assert!(rechecked["plan"].is_object(), "{rechecked}");
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&config).unwrap(), config_before);

    let modules = fixture.root().join("modules");
    let plan_arg = plan_path.to_str().unwrap();
    let modules_arg = modules.to_str().unwrap();
    let config_arg = config.to_str().unwrap();
    let preview = fixture.command(&[
        "modules",
        "apply",
        "--plan",
        plan_arg,
        "--modules",
        modules_arg,
        "--tree-config",
        config_arg,
        "--dry-run",
        "--format",
        "json",
    ]);
    process::assert_success(&preview);
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&config).unwrap(), config_before);

    let applied = fixture.command(&[
        "modules",
        "apply",
        "--plan",
        plan_arg,
        "--modules",
        modules_arg,
        "--tree-config",
        config_arg,
        "--format",
        "json",
    ]);
    process::assert_success(&applied);
    fixture.assert_runs("1 3\n");
    fixture.refresh_graph();
    assert_owner(&fixture, "anchor", "home");
    assert_owner(&fixture, "dep", "chain");
    assert_owner(&fixture, "consumer", "chain");
}

#[test]
fn default_check_requires_source_root_while_graph_only_omits_the_option() {
    let (fixture, changes) = duplicate_initializer_case();
    let (batch, order) = write_batch(&fixture, "no-source", &changes);
    let before = input_snapshot(&fixture, &batch, &order);

    let default = run_check(&fixture, &batch, &order, false, false, false);
    assert!(
        !default.status.success(),
        "default check succeeded without a source root: {}",
        String::from_utf8_lossy(&default.stdout)
    );
    let diagnostic = format!(
        "{}\n{}",
        String::from_utf8_lossy(&default.stdout),
        String::from_utf8_lossy(&default.stderr)
    );
    assert!(
        diagnostic.contains("--source-root") || diagnostic.contains("source root"),
        "missing source diagnostic: {diagnostic}"
    );
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);

    let graph_only = run_check(&fixture, &batch, &order, true, false, false);
    process::assert_success(&graph_only);
    let graph_only = parse_stdout_json(&graph_only);
    assert_eq!(graph_only["scope"], "graph_only");
    assert_eq!(graph_only["accepted"], true, "{graph_only}");
    assert_eq!(graph_only["graph_accepted"], true, "{graph_only}");
    assert!(graph_only["plan"].is_null(), "{graph_only}");
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);

    let missing_plan = fixture.root().join("missing-plan.json");
    let conflict = fixture.command(&[
        "modules",
        "check",
        "--plan",
        missing_plan.to_str().unwrap(),
        "--graph-only",
    ]);
    assert_eq!(
        conflict.status.code(),
        Some(2),
        "{}",
        String::from_utf8_lossy(&conflict.stderr)
    );
    assert!(
        String::from_utf8_lossy(&conflict.stderr).contains("cannot be used with"),
        "{}",
        String::from_utf8_lossy(&conflict.stderr)
    );
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);
}

#[test]
fn graph_invalid_batch_reports_graph_rejection_without_a_plan() {
    let fixture = GraphFixture::new(
        "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nexport { anchor, consumer };\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let changes = json!([{
        "unit_id": fixture.source_unit_for_binding("consumer"),
        "module": "chain",
    }]);
    let (batch, order) = write_batch(&fixture, "graph-invalid", &changes);
    let before = input_snapshot(&fixture, &batch, &order);

    let output = run_check(&fixture, &batch, &order, false, true, false);
    assert!(!output.status.success());
    let report = parse_stdout_json(&output);
    assert_eq!(report["scope"], "executable_plan");
    assert_eq!(report["accepted"], false, "{report}");
    assert_eq!(report["graph_accepted"], false, "{report}");
    assert!(report["validation"].is_object(), "{report}");
    assert!(report["plan"].is_null(), "{report}");
    assert_eq!(input_snapshot(&fixture, &batch, &order), before);
}
