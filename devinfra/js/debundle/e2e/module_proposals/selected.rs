//! End-to-end selected-view residual discovery and overlay authoring.

use super::support::unit_id_for_binding;
use debundle_e2e_graph_support::json_module_path_for_unit;
use debundle_e2e_support::saved_analysis::assert_ok;
use debundle_e2e_support::{
    TreeFixture, TreeRun, assert_node_output, debundle_fixture_output, debundler_path, process,
    read_gzip_json, read_tree, tree_spec_args, write_tree_fixture,
};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const SOURCE: &str = r#"
function anchor() {}
function other(argument) {}
const a = anchor.length;
const b = other.length;
const extra = 8;
console.log(a, b, extra);
export { a, b, extra };
"#;

const MODULES: &[(&str, &str)] = &[
    (
        "main/anchor.yaml",
        "members: [{name: Anchor, selector: {binding: {name: anchor}}}]\n",
    ),
    (
        "main/fixed.yaml",
        "members: [{name: FixedA, selector: {reads_member: {member: length, object: Anchor}}}]\n",
    ),
    (
        "main/kept.yaml",
        "source_matches: [{match: 'const value = OBJECT.length;', bindings: [{local: value, name: Kept}]}]\n",
    ),
];

fn tree() -> TreeFixture<'static> {
    TreeFixture {
        chunks: &[("app", SOURCE)],
        module_roots: &[("main", "app")],
        modules: MODULES,
    }
}

struct SelectedFixture {
    _run: TreeRun,
    root: PathBuf,
    modules: PathBuf,
    config: PathBuf,
    subset: PathBuf,
    graph: PathBuf,
}

fn selected_fixture_for_tree(
    tree: &TreeFixture<'_>,
    view: Value,
    import_order: &[&str],
) -> SelectedFixture {
    let workspace = write_tree_fixture(tree);
    let root = workspace.root().to_path_buf();
    let config = root.join("spec_config.yaml");
    let modules = root.join("modules");
    let subset = root.join("selected.json");
    let mut config_value: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config).unwrap()).unwrap();
    config_value["import_order"]["app"] =
        serde_yaml::to_value(import_order).expect("serialize selected priority");
    fs::write(&config, serde_yaml::to_string(&config_value).unwrap()).unwrap();
    fs::write(&subset, serde_json::to_vec_pretty(&view).unwrap()).unwrap();

    let subset_arg = subset.to_str().unwrap();
    let run = workspace.run(&["--graphs", "--ownership-subset", subset_arg]);
    assert_ok(&run.result, "selected tree run");
    let graph = run.report_root.join("app/dependency_graph.json.gz");
    SelectedFixture {
        _run: run,
        root,
        modules,
        config,
        subset,
        graph,
    }
}

fn selected_fixture_with_view(view: Value, import_order: &[&str]) -> SelectedFixture {
    selected_fixture_for_tree(&tree(), view, import_order)
}

fn selected_fixture() -> SelectedFixture {
    // The full base resolves Kept to `b`: FixedA's reads_member claim anchors
    // `a`, and the source_match claim is all_different from it. The selected
    // view omits the base owners while retaining Kept and an overlay placement
    // that deliberately reuses the omitted `a` claim.
    let fixture = selected_fixture_with_view(
        json!({
            "modules": [{"chunk":"app", "module":"kept"}],
            "placements": {"app": {"existing": {
                "comment": "Preserve this authored overlay.",
                "source_matches": [{"match":"const value = Anchor.length;", "bindings":[{"local":"value", "name":"Existing"}]}]
            }}}
        }),
        &["kept", "existing"],
    );
    assert_node_output(
        &fixture._run.out_root.join("app/app/entry.js"),
        "0 1 8\n",
        "",
    );
    fixture
}

fn cli(fixture: &SelectedFixture, args: &[impl AsRef<std::ffi::OsStr>]) -> Output {
    debundle_fixture_output(
        Command::new(debundler_path())
            .arg("--graph-ownership-subset-root")
            .arg(&fixture.root)
            .args(args),
        &fixture.root,
    )
    .unwrap()
}

fn graph_args(fixture: &SelectedFixture) -> Vec<String> {
    vec![
        "--graph".to_owned(),
        fixture.graph.display().to_string(),
        "--modules".to_owned(),
        fixture.modules.display().to_string(),
        "--source-root".to_owned(),
        fixture.root.display().to_string(),
        "--tree-config".to_owned(),
        fixture.config.display().to_string(),
    ]
}

fn require_success(output: Output, context: &str) -> Value {
    assert!(
        output.status.success(),
        "{context}\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("CLI emits JSON")
}

fn propose(fixture: &SelectedFixture) -> Value {
    let mut args = vec!["modules".to_owned(), "propose".to_owned()];
    args.extend(graph_args(fixture));
    args.extend([
        "--into".to_owned(),
        "fresh".to_owned(),
        "--format".to_owned(),
        "json".to_owned(),
    ]);
    require_success(cli(fixture, &args), "unseeded selected proposal")
}

fn propose_requiring(fixture: &SelectedFixture, unit_id: &str) -> Value {
    let mut args = vec!["modules".to_owned(), "propose".to_owned()];
    args.extend(graph_args(fixture));
    args.extend([
        "--into".to_owned(),
        "fresh".to_owned(),
        "--require".to_owned(),
        unit_id.to_owned(),
        "--format".to_owned(),
        "json".to_owned(),
    ]);
    require_success(cli(fixture, &args), "required selected proposal")
}

fn plan_path(fixture: &SelectedFixture, plan: &Value) -> PathBuf {
    let path = fixture.root.join("selected-plan.json");
    fs::write(&path, serde_json::to_vec_pretty(plan).unwrap()).unwrap();
    path
}

fn check_plan(fixture: &SelectedFixture, plan: &Path) -> Output {
    let mut args = vec!["modules".to_owned(), "check".to_owned()];
    args.extend(graph_args(fixture));
    args.extend([
        "--plan".to_owned(),
        plan.display().to_string(),
        "--format".to_owned(),
        "json".to_owned(),
    ]);
    cli(fixture, &args)
}

fn apply_plan(fixture: &SelectedFixture, plan: &Path, dry_run: bool) -> Output {
    let mut args = vec!["modules".to_owned(), "apply".to_owned()];
    args.extend(graph_args(fixture));
    args.extend(["--plan".to_owned(), plan.display().to_string()]);
    if dry_run {
        args.push("--dry-run".to_owned());
    }
    args.extend(["--format".to_owned(), "json".to_owned()]);
    cli(fixture, &args)
}

fn check_batch(fixture: &SelectedFixture, batch: &Path, import_order: &Path) -> Output {
    let mut args = vec!["modules".to_owned(), "check".to_owned()];
    args.extend(graph_args(fixture));
    args.extend([
        "--batch".to_owned(),
        batch.display().to_string(),
        "--import-order".to_owned(),
        import_order.display().to_string(),
        "--format".to_owned(),
        "json".to_owned(),
    ]);
    cli(fixture, &args)
}

fn assert_apply_rejected_without_writes(fixture: &SelectedFixture, plan: &Path, dry_run: bool) {
    let modules_before = read_tree(&fixture.modules);
    let view_before = fs::read(&fixture.subset).unwrap();
    let config_before = fs::read(&fixture.config).unwrap();
    let source_path = fixture.root.join("snapshot/app.js");
    let source_before = fs::read(&source_path).unwrap();
    let output = apply_plan(fixture, plan, dry_run);
    assert!(
        !output.status.success(),
        "invalid selected-view apply unexpectedly succeeded:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&fixture.subset).unwrap(), view_before);
    assert_eq!(fs::read(&fixture.config).unwrap(), config_before);
    assert_eq!(fs::read(source_path).unwrap(), source_before);
}

fn materialize_again(fixture: &SelectedFixture, output_root: &Path) -> Output {
    let mut command = Command::new(debundler_path());
    command
        .arg("run")
        .args(tree_spec_args(&fixture.root))
        .arg("--out-root")
        .arg(output_root)
        .args(["--graphs", "--ownership-subset"])
        .arg(&fixture.subset);
    debundle_fixture_output(&mut command, &fixture.root).unwrap()
}

#[test]
fn unseeded_selected_residual_peel_checks_previews_applies_and_runs() {
    let base = debundle_e2e_support::run_tree_fixture(&tree(), &[]);
    assert_ok(&base.result, "base tree run");
    assert_node_output(&base.out_root.join("app/app/entry.js"), "0 1 8\n", "");

    let fixture = selected_fixture();
    assert_node_output(
        &fixture._run.out_root.join("app/app/entry.js"),
        "0 1 8\n",
        "",
    );
    let graph_before: Value = read_gzip_json(&fixture.graph);
    assert_eq!(graph_before["import_order"], json!(["kept", "existing"]));
    assert_eq!(
        graph_before["ownership_subset"],
        json!([{"module": "kept"}])
    );
    let retained_unit = unit_id_for_binding(&graph_before, "b");
    assert_eq!(
        json_module_path_for_unit(&graph_before, &retained_unit),
        "kept"
    );
    let original_view: Value = serde_json::from_slice(&fs::read(&fixture.subset).unwrap()).unwrap();
    let existing_placement = original_view["placements"]["app"]["existing"].clone();
    let modules_before = read_tree(&fixture.modules);
    let view_before = fs::read(&fixture.subset).unwrap();
    let config_before = fs::read(&fixture.config).unwrap();

    let proposal = propose(&fixture);
    assert_eq!(proposal["status"], "found", "{proposal:#}");
    assert!(proposal["plan"].is_object(), "{proposal:#}");
    assert!(proposal["residual_units_extracted"].as_u64().unwrap() > 0);
    let plan = &proposal["plan"];
    assert_eq!(
        plan["snapshot"]["dependency_graph_sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    assert_eq!(
        plan["snapshot"]["program_graph_sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    let changes = plan["changes"].as_array().unwrap();
    assert!(!changes.is_empty());
    for change in changes {
        assert_eq!(change["module"], "fresh");
    }
    assert_eq!(plan["import_order"], json!(["kept", "existing", "fresh"]));
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&fixture.subset).unwrap(), view_before);
    assert_eq!(fs::read(&fixture.config).unwrap(), config_before);

    let plan_file = plan_path(&fixture, plan);
    let checked = require_success(check_plan(&fixture, &plan_file), "selected plan check");
    assert_eq!(checked["accepted"], true, "{checked:#}");
    assert_eq!(checked["existing_units_moved"], 0, "{checked:#}");
    assert!(checked["plan"].is_object(), "{checked:#}");

    require_success(
        apply_plan(&fixture, &plan_file, true),
        "selected plan preview",
    );
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&fixture.subset).unwrap(), view_before);
    assert_eq!(fs::read(&fixture.config).unwrap(), config_before);

    require_success(
        apply_plan(&fixture, &plan_file, false),
        "selected plan apply",
    );
    assert_eq!(read_tree(&fixture.modules), modules_before);
    let updated_view: Value = serde_json::from_slice(&fs::read(&fixture.subset).unwrap()).unwrap();
    assert_eq!(
        updated_view["placements"]["app"]["existing"],
        existing_placement
    );
    assert!(updated_view["placements"]["app"]["fresh"].is_object());
    let updated_config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&fixture.config).unwrap()).unwrap();
    assert_eq!(
        serde_json::to_value(&updated_config["import_order"]["app"]).unwrap(),
        json!(["kept", "existing", "fresh"])
    );

    let output_root = fixture.root.join("after-apply");
    process::assert_success(&materialize_again(&fixture, &output_root));
    assert_node_output(&output_root.join("app/app/entry.js"), "0 1 8\n", "");
    let exports_probe = output_root.join("retained-exports.mjs");
    fs::write(
        &exports_probe,
        r#"await import('./app/app/entry.js');
const kept = await import('./app/app/kept.js');
const existing = await import('./app/app/existing.js');
console.log(JSON.stringify([Object.keys(kept).sort(), Object.keys(existing).sort(), kept.Kept, existing.Existing]));
"#,
    )
    .unwrap();
    assert_node_output(
        &exports_probe,
        "0 1 8\n[[\"Kept\"],[\"Existing\"],1,0]\n",
        "",
    );
    let graph_after: Value =
        read_gzip_json(&output_root.join("reports/tree/app/dependency_graph.json.gz"));
    assert_eq!(
        graph_after["import_order"],
        json!(["kept", "existing", "fresh"])
    );
    for change in changes {
        let id = change["unit_id"].as_str().unwrap();
        assert_ne!(json_module_path_for_unit(&graph_before, id), "fresh");
        assert_eq!(json_module_path_for_unit(&graph_after, id), "fresh");
    }
    for binding in ["a", "b"] {
        let id = unit_id_for_binding(&graph_before, binding);
        let expected_owner = if binding == "b" { "kept" } else { "existing" };
        assert_eq!(json_module_path_for_unit(&graph_after, &id), expected_owner);
    }
}

#[test]
fn omitted_all_different_anchor_changes_retained_identity_in_control() {
    const WITH_OTHER_ANCHOR: &[(&str, &str)] = &[
        (
            "main/anchor.yaml",
            "members: [{name: Anchor, selector: {binding: {name: anchor}}}]\n",
        ),
        (
            "main/other.yaml",
            "members: [{name: Other, selector: {binding: {name: other}}}]\n",
        ),
        (
            "main/fixed.yaml",
            "members: [{name: FixedB, selector: {reads_member: {member: length, object: Other}}}]\n",
        ),
        (
            "main/kept.yaml",
            "source_matches: [{match: 'const value = OBJECT.length;', bindings: [{local: value, name: Kept}]}]\n",
        ),
    ];
    let changed_premise_tree = TreeFixture {
        chunks: &[("app", SOURCE)],
        module_roots: &[("main", "app")],
        modules: WITH_OTHER_ANCHOR,
    };
    let fixture = selected_fixture_for_tree(
        &changed_premise_tree,
        json!({"modules": [{"chunk":"app", "module":"kept"}]}),
        &["kept"],
    );
    assert_node_output(
        &fixture._run.out_root.join("app/app/entry.js"),
        "0 1 8\n",
        "",
    );
    let graph: Value = read_gzip_json(&fixture.graph);
    let a = unit_id_for_binding(&graph, "a");
    let b = unit_id_for_binding(&graph, "b");
    assert_eq!(json_module_path_for_unit(&graph, &a), "kept");
    assert_ne!(json_module_path_for_unit(&graph, &b), "kept");
}

#[test]
fn ambiguous_selected_selector_discovery_has_no_plan_or_writes() {
    const AMBIGUOUS_SOURCE: &str = "void 1;\nvoid 1;\nconsole.log('baseline');\n";
    let ambiguous_tree = TreeFixture {
        chunks: &[("app", AMBIGUOUS_SOURCE)],
        module_roots: &[("main", "app")],
        modules: &[],
    };
    let fixture = selected_fixture_for_tree(&ambiguous_tree, json!({"modules": []}), &[]);
    assert_node_output(
        &fixture._run.out_root.join("app/app/entry.js"),
        "baseline\n",
        "",
    );
    let modules_before = read_tree(&fixture.modules);
    let view_before = fs::read(&fixture.subset).unwrap();
    let config_before = fs::read(&fixture.config).unwrap();

    let graph: Value = read_gzip_json(&fixture.graph);
    let ambiguous_unit = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|unit| unit["source_location"]["start_line"] == 1)
        .unwrap()["id"]
        .as_str()
        .unwrap();
    let report = propose_requiring(&fixture, ambiguous_unit);
    assert_eq!(report["status"], "inventory_only", "{report:#}");
    assert!(report["plan"].is_null(), "{report:#}");
    assert!(
        report["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|blocker| blocker
                .as_str()
                .unwrap()
                .contains("no uniquely proven structural selector")),
        "ambiguous selector must be the reason no plan is returned: {report:#}"
    );
    let batch = fixture.root.join("ambiguous-batch.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{"unit_id": ambiguous_unit, "module": "fresh"}])).unwrap(),
    )
    .unwrap();
    let order = fixture.root.join("ambiguous-order.json");
    fs::write(&order, serde_json::to_vec(&json!(["fresh"])).unwrap()).unwrap();
    let check = check_batch(&fixture, &batch, &order);
    assert!(
        !check.status.success(),
        "ambiguous selector check unexpectedly passed"
    );
    let checked: Value = serde_json::from_slice(&check.stdout).unwrap_or_else(|error| {
        panic!(
            "selected batch check must emit its rejection report: {error}\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&check.stdout),
            String::from_utf8_lossy(&check.stderr)
        )
    });
    assert_eq!(checked["accepted"], false, "{checked:#}");
    assert!(checked["plan"].is_null(), "{checked:#}");
    assert!(
        checked["selector_realization_error"]
            .as_str()
            .unwrap()
            .contains("no uniquely proven structural selector"),
        "{checked:#}"
    );
    assert_eq!(read_tree(&fixture.modules), modules_before);
    assert_eq!(fs::read(&fixture.subset).unwrap(), view_before);
    assert_eq!(fs::read(&fixture.config).unwrap(), config_before);
}

#[test]
fn stale_selected_inputs_and_invalid_plan_edits_never_write_even_in_preview() {
    for stale_input in ["source", "config", "view"] {
        let fixture = selected_fixture();
        let proposal = propose(&fixture);
        assert_eq!(proposal["status"], "found", "{proposal:#}");
        let plan = plan_path(&fixture, &proposal["plan"]);
        match stale_input {
            "source" => {
                let path = fixture.root.join("snapshot/app.js");
                let mut bytes = fs::read(&path).unwrap();
                bytes.extend_from_slice(b"\n// changed after graph capture\n");
                fs::write(path, bytes).unwrap();
            }
            "config" => {
                let mut bytes = fs::read(&fixture.config).unwrap();
                bytes.extend_from_slice(b"# changed after graph capture\n");
                fs::write(&fixture.config, bytes).unwrap();
            }
            "view" => {
                let mut bytes = fs::read(&fixture.subset).unwrap();
                bytes.extend_from_slice(b"\n");
                fs::write(&fixture.subset, bytes).unwrap();
            }
            _ => unreachable!(),
        }
        assert_apply_rejected_without_writes(&fixture, &plan, true);
    }

    let fixture = selected_fixture();
    let proposal = propose(&fixture);
    assert_eq!(proposal["status"], "found", "{proposal:#}");
    let mut bad_priority = proposal["plan"].clone();
    // An incomplete priority is rejected even when the changed units are
    // otherwise individually realizable.
    bad_priority["import_order"] = json!(["kept", "fresh"]);
    let bad_priority_path = plan_path(&fixture, &bad_priority);
    assert_apply_rejected_without_writes(&fixture, &bad_priority_path, false);

    let fixture = selected_fixture();
    let proposal = propose(&fixture);
    assert_eq!(proposal["status"], "found", "{proposal:#}");
    let mut reassignment = proposal["plan"].clone();
    for change in reassignment["changes"].as_array_mut().unwrap() {
        change["module"] = json!("kept");
    }
    reassignment["import_order"] = json!(["kept", "existing"]);
    let reassignment_path = plan_path(&fixture, &reassignment);
    assert_apply_rejected_without_writes(&fixture, &reassignment_path, true);
}

#[test]
fn selected_plan_cannot_split_an_atomic_group() {
    const ATOMIC_SOURCE: &str = "let value = 0;\nvalue += 1;\nconsole.log(value);\n";
    let atom_tree = TreeFixture {
        chunks: &[("app", ATOMIC_SOURCE)],
        module_roots: &[("main", "app")],
        modules: &[],
    };
    let fixture = selected_fixture_for_tree(&atom_tree, json!({"modules": []}), &[]);
    assert_node_output(&fixture._run.out_root.join("app/app/entry.js"), "1\n", "");
    let graph: Value = read_gzip_json(&fixture.graph);
    let group = graph["atomic_graph"]["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| group["source_unit_ids"].as_array().unwrap().len() > 1)
        .unwrap_or_else(|| panic!("fixture has no multi-unit atom: {graph:#}"));
    let first_unit = group["source_unit_ids"][0].as_str().unwrap();

    let proposal = propose(&fixture);
    assert_eq!(proposal["status"], "found", "{proposal:#}");
    let mut split = proposal["plan"].clone();
    split["changes"] = json!([{"unit_id": first_unit, "module": "fresh"}]);
    split["import_order"] = json!(["fresh"]);
    let split_path = plan_path(&fixture, &split);
    assert_apply_rejected_without_writes(&fixture, &split_path, true);
}
