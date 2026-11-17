//! Selected-ownership corpus proposals reuse verified program preparation.

use debundle_e2e_graph_support::{json_module_path_for_unit, json_node_declaring};
use debundle_e2e_support::{
    TreeFixture, TreeWorkspace, assert_node_output, parse_stdout_json, read_gzip_json, read_tree,
    run_debundle, tree_spec_args, write_tree_fixture,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;

const SOURCE: &str = r#"
function anchor() {}
function other(argument) {}
const a = 0;
const b = 1;
const c = 2;
const extra = 8;
console.log(a, b, c, extra);
export { a, b, c, extra };
"#;

const CHUNKS: &[(&str, &str)] = &[("alpha", SOURCE), ("beta", SOURCE)];
const ROOTS: &[(&str, &str)] = &[("chunks/alpha", "alpha"), ("chunks/beta", "beta")];
const ANCHOR: &str = "members: [{name: Anchor, selector: {binding: {name: anchor}}}]\n";
const FIXED: &str = "members: [{name: FixedExtra, selector: {binding: {name: extra}}}]\n";
const KEPT: &str = "members: [{name: KeptB, selector: {binding: {name: b}}}, {name: KeptC, selector: {binding: {name: c}}}]\n";
const MODULES: &[(&str, &str)] = &[
    ("chunks/alpha/anchor.yaml", ANCHOR),
    ("chunks/alpha/fixed.yaml", FIXED),
    ("chunks/alpha/kept.yaml", KEPT),
    ("chunks/beta/anchor.yaml", ANCHOR),
    ("chunks/beta/fixed.yaml", FIXED),
    ("chunks/beta/kept.yaml", KEPT),
];

struct Fixture {
    _workspace: TreeWorkspace,
    root: PathBuf,
    modules: PathBuf,
    config: PathBuf,
}

fn tree() -> TreeFixture<'static> {
    TreeFixture {
        chunks: CHUNKS,
        module_roots: ROOTS,
        modules: MODULES,
    }
}

fn fixture() -> Fixture {
    let workspace = write_tree_fixture(&tree());
    let root = workspace.root().to_path_buf();
    let config = root.join("spec_config.yaml");
    let modules = root.join("modules");
    let mut value: serde_yaml::Value = serde_yaml::from_slice(&fs::read(&config).unwrap()).unwrap();
    for chunk in ["alpha", "beta"] {
        value["import_order"][chunk] = serde_yaml::to_value(["kept"]).unwrap();
    }
    fs::write(&config, serde_yaml::to_string(&value).unwrap()).unwrap();
    Fixture {
        _workspace: workspace,
        root,
        modules,
        config,
    }
}

fn run(args: &[String]) -> Output {
    run_debundle(&args.iter().map(String::as_str).collect::<Vec<_>>())
}

fn path_arg(path: &Path) -> String {
    path.to_str().expect("fixture path is UTF-8").to_owned()
}

fn write_view(root: &Path, name: &str, view: &Value) -> PathBuf {
    let path = root.join(name);
    fs::write(&path, serde_json::to_vec_pretty(view).unwrap()).unwrap();
    path
}

fn all_kept_view() -> Value {
    json!({
        "modules": [
            {"chunk": "alpha", "module": "kept"},
            {"chunk": "beta", "module": "kept"}
        ]
    })
}

fn one_binding_view(binding: &str) -> Value {
    json!({
        "modules": [
            {"chunk": "alpha", "module": "kept", "bindings": [binding]},
            {"chunk": "beta", "module": "kept", "bindings": [binding]}
        ]
    })
}

fn capture_selected(root: &Path, output_root: &Path, view: &Path) -> Output {
    let mut args = vec!["run".to_owned()];
    args.extend(
        tree_spec_args(root)
            .into_iter()
            .map(|arg| arg.into_string().expect("fixture path is UTF-8")),
    );
    args.extend([
        "--out-root".to_owned(),
        path_arg(output_root),
        "--graphs".to_owned(),
        "--ownership-subset".to_owned(),
        path_arg(view),
    ]);
    run(&args)
}

fn corpus_args(root: &Path, graphs: &Path) -> Vec<String> {
    vec![
        "--graph-ownership-subset-root".into(),
        path_arg(root),
        "modules".into(),
        "propose".into(),
        "--graphs".into(),
        path_arg(graphs),
        "--modules".into(),
        path_arg(&root.join("modules")),
        "--source-root".into(),
        path_arg(&root.join("snapshot")),
        "--tree-config".into(),
        path_arg(&root.join("spec_config.yaml")),
        "--format".into(),
        "json".into(),
    ]
}

fn selected_graph_args(root: &Path, graph: &Path) -> Vec<String> {
    vec![
        "--graph".into(),
        path_arg(graph),
        "--modules".into(),
        path_arg(&root.join("modules")),
        "--source-root".into(),
        path_arg(&root.join("snapshot")),
        "--tree-config".into(),
        path_arg(&root.join("spec_config.yaml")),
    ]
}

fn graph_for(reports_root: &Path, chunk: &str) -> PathBuf {
    reports_root.join(chunk).join("dependency_graph.json.gz")
}

fn unit_id_for_binding(graph: &Value, binding: &str) -> String {
    json_node_declaring(graph, binding)["id"]
        .as_str()
        .unwrap()
        .to_owned()
}

fn restore_tree(root: &Path, files: &BTreeMap<PathBuf, Vec<u8>>) {
    if root.exists() {
        fs::remove_dir_all(root).unwrap();
    }
    for (relative, bytes) in files {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }
}

fn assert_stale_corpus_refused(
    fixture: &Fixture,
    reports_root: &Path,
    view: &Path,
    modules_before: &BTreeMap<PathBuf, Vec<u8>>,
    config_bytes: &[u8],
    view_bytes: &[u8],
    beta_source_bytes: &[u8],
) {
    let output = run(&corpus_args(&fixture.root, reports_root));
    assert!(!output.status.success());
    assert!(
        !String::from_utf8_lossy(&output.stdout).contains("\"chunks\""),
        "stale corpus emitted successful proposal rows:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(read_tree(&fixture.modules), *modules_before);
    assert_eq!(fs::read(&fixture.config).unwrap(), config_bytes);
    assert_eq!(fs::read(view).unwrap(), view_bytes);
    assert_eq!(
        fs::read(fixture.root.join("snapshot/beta.js")).unwrap(),
        beta_source_bytes
    );
}

fn assert_runtime(output_root: &Path, chunk: &str) {
    let mut pending = vec![output_root.to_path_buf()];
    let mut entries = Vec::new();
    while let Some(directory) = pending.pop() {
        for item in fs::read_dir(directory).unwrap() {
            let path = item.unwrap().path();
            if path.is_dir() {
                pending.push(path);
            } else if path.file_name() == Some(OsStr::new("entry.js"))
                && path
                    .components()
                    .any(|component| component.as_os_str() == OsStr::new(chunk))
            {
                entries.push(path);
            }
        }
    }
    assert_eq!(entries.len(), 1, "{chunk} entry files: {entries:?}");
    let entry = entries.pop().unwrap();
    let module_directory = entry.parent().unwrap();
    assert_node_output(&entry, "0 1 2 8\n", "");
    let probe = module_directory.join("retained-exports.mjs");
    fs::write(
        &probe,
        "await import('./entry.js');\n\
         const kept = await import('./kept.js');\n\
         console.log(JSON.stringify([Object.keys(kept).sort(), kept.KeptB, kept.KeptC]));\n",
    )
    .unwrap();
    assert_node_output(&probe, "0 1 2 8\n[[\"KeptB\",\"KeptC\"],1,2]\n", "");
}

#[test]
fn selected_corpus_plans_share_preparation_and_apply_as_chunk_local_views() {
    let fixture = fixture();
    let view = write_view(&fixture.root, "selected.json", &all_kept_view());
    let original_view = fs::read(&view).unwrap();
    let original_config = fs::read(&fixture.config).unwrap();
    let original_modules = read_tree(&fixture.modules);
    let first_capture = fixture.root.join("capture");
    let captured = capture_selected(&fixture.root, &first_capture, &view);
    assert!(
        captured.status.success(),
        "selected graph capture failed:\n{}\n{}",
        String::from_utf8_lossy(&captured.stdout),
        String::from_utf8_lossy(&captured.stderr)
    );
    for chunk in ["alpha", "beta"] {
        assert_runtime(&first_capture, chunk);
    }

    let reports_root = first_capture.join("reports/tree");
    let initial_graphs = [
        ("alpha", graph_for(&reports_root, "alpha")),
        ("beta", graph_for(&reports_root, "beta")),
    ];
    for (chunk, graph_path) in &initial_graphs {
        let graph: Value = read_gzip_json(graph_path);
        assert_eq!(graph["import_order"], json!(["kept"]), "{chunk}");
        for binding in ["b", "c"] {
            let unit = unit_id_for_binding(&graph, binding);
            assert_eq!(
                json_module_path_for_unit(&graph, &unit),
                "kept",
                "{chunk}:{binding}"
            );
        }
    }

    let corpus_output = run(&corpus_args(&fixture.root, &reports_root));
    assert!(
        corpus_output.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&corpus_output.stdout),
        String::from_utf8_lossy(&corpus_output.stderr)
    );
    let corpus = parse_stdout_json(&corpus_output);
    assert_eq!(corpus["status"], "found", "{corpus}");
    assert_eq!(corpus["chunks"].as_array().unwrap().len(), 2, "{corpus}");
    let rows = corpus["chunks"].as_array().unwrap();
    let mut plans = Vec::new();
    for (row, (expected_chunk, graph)) in rows.iter().zip(&initial_graphs) {
        assert_eq!(row["chunk_id"], *expected_chunk, "{row}");
        assert_eq!(row["search"]["status"], "found", "{row}");
        assert!(row["plan"].is_object(), "{row}");
        assert!(
            row["search"]["residual_units_extracted"]
                .as_u64()
                .unwrap_or_default()
                > 0,
            "{row}"
        );
        let changes = row["plan"]["changes"].as_array().unwrap();
        assert!(!changes.is_empty(), "{row}");
        let owner = changes[0]["module"]
            .as_str()
            .expect("plan change has a module destination")
            .to_owned();
        assert!(
            changes.iter().all(|change| change["module"] == owner),
            "{row}"
        );
        plans.push((
            (*expected_chunk).to_owned(),
            row["plan"].clone(),
            graph.clone(),
            owner,
        ));
    }
    assert_eq!(read_tree(&fixture.modules), original_modules);
    assert_eq!(fs::read(&fixture.config).unwrap(), original_config);
    assert_eq!(fs::read(&view).unwrap(), original_view);

    let beta_source = fixture.root.join("snapshot/beta.js");
    let original_beta_source = fs::read(&beta_source).unwrap();

    // A stale secondary source, tree config or selected view must refuse the
    // full corpus before emitting successful rows and without authoring writes.
    let mut stale_source = original_beta_source.clone();
    stale_source.extend_from_slice(b"// stale secondary source\n");
    fs::write(&beta_source, &stale_source).unwrap();
    assert_stale_corpus_refused(
        &fixture,
        &reports_root,
        &view,
        &original_modules,
        &original_config,
        &original_view,
        &stale_source,
    );
    fs::write(&beta_source, &original_beta_source).unwrap();

    let mut stale_config = original_config.clone();
    stale_config.extend_from_slice(b"# stale config bytes\n");
    fs::write(&fixture.config, &stale_config).unwrap();
    assert_stale_corpus_refused(
        &fixture,
        &reports_root,
        &view,
        &original_modules,
        &stale_config,
        &original_view,
        &original_beta_source,
    );
    fs::write(&fixture.config, &original_config).unwrap();

    // Even a semantically empty view edit changes the captured input identity.
    let mut stale_view: Value = serde_json::from_slice(&original_view).unwrap();
    stale_view["placements"] = json!({});
    fs::write(&view, serde_json::to_vec_pretty(&stale_view).unwrap()).unwrap();
    let stale_bytes = fs::read(&view).unwrap();
    assert_stale_corpus_refused(
        &fixture,
        &reports_root,
        &view,
        &original_modules,
        &original_config,
        &stale_bytes,
        &original_beta_source,
    );
    fs::write(&view, &original_view).unwrap();

    // Plan snapshots bind the complete authoring tree, so each selected plan
    // starts from the same module, config and view bytes.
    for (chunk, plan, graph, owner) in plans {
        let plan_path = fixture.root.join(format!("{chunk}-plan.json"));
        fs::write(&plan_path, serde_json::to_vec_pretty(&plan).unwrap()).unwrap();

        let mut check = vec![
            "--graph-ownership-subset-root".to_owned(),
            path_arg(&fixture.root),
        ];
        check.extend(["modules".into(), "check".into()]);
        check.extend(selected_graph_args(&fixture.root, &graph));
        check.extend([
            "--plan".into(),
            path_arg(&plan_path),
            "--format".into(),
            "json".into(),
        ]);
        let checked = run(&check);
        assert!(
            checked.status.success(),
            "check {chunk} failed:\n{}\n{}",
            String::from_utf8_lossy(&checked.stdout),
            String::from_utf8_lossy(&checked.stderr)
        );
        assert_eq!(parse_stdout_json(&checked)["accepted"], true);

        let before_preview = read_tree(&fixture.modules);
        let mut preview = vec![
            "--graph-ownership-subset-root".to_owned(),
            path_arg(&fixture.root),
        ];
        preview.extend(["modules".into(), "apply".into()]);
        preview.extend(selected_graph_args(&fixture.root, &graph));
        preview.extend([
            "--plan".into(),
            path_arg(&plan_path),
            "--dry-run".into(),
            "--format".into(),
            "json".into(),
        ]);
        let previewed = run(&preview);
        assert!(
            previewed.status.success(),
            "preview {chunk} failed:\n{}\n{}",
            String::from_utf8_lossy(&previewed.stdout),
            String::from_utf8_lossy(&previewed.stderr)
        );
        assert_eq!(read_tree(&fixture.modules), before_preview);
        assert_eq!(fs::read(&fixture.config).unwrap(), original_config);
        assert_eq!(fs::read(&view).unwrap(), original_view);

        let mut apply = vec![
            "--graph-ownership-subset-root".to_owned(),
            path_arg(&fixture.root),
        ];
        apply.extend(["modules".into(), "apply".into()]);
        apply.extend(selected_graph_args(&fixture.root, &graph));
        apply.extend([
            "--plan".into(),
            path_arg(&plan_path),
            "--format".into(),
            "json".into(),
        ]);
        let applied = run(&apply);
        assert!(
            applied.status.success(),
            "apply {chunk} failed:\n{}\n{}",
            String::from_utf8_lossy(&applied.stdout),
            String::from_utf8_lossy(&applied.stderr)
        );
        assert_eq!(read_tree(&fixture.modules), original_modules);
        let applied_view: Value = serde_json::from_slice(&fs::read(&view).unwrap()).unwrap();
        assert!(
            applied_view["placements"][chunk.as_str()][owner.as_str()].is_object(),
            "{applied_view}"
        );
        let other_chunk = if chunk == "alpha" { "beta" } else { "alpha" };
        assert!(
            applied_view["placements"][other_chunk].is_null(),
            "{applied_view}"
        );
        let applied_config: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(&fixture.config).unwrap()).unwrap();
        assert_eq!(
            serde_json::to_value(&applied_config["import_order"][chunk.as_str()]).unwrap(),
            json!(["kept", owner.as_str()])
        );
        assert_eq!(
            serde_json::to_value(&applied_config["import_order"][other_chunk]).unwrap(),
            json!(["kept"])
        );

        let output_root = fixture.root.join(format!("after-{chunk}"));
        let regenerated = capture_selected(&fixture.root, &output_root, &view);
        assert!(
            regenerated.status.success(),
            "selected regeneration after {chunk} apply failed:\n{}\n{}",
            String::from_utf8_lossy(&regenerated.stdout),
            String::from_utf8_lossy(&regenerated.stderr)
        );
        assert_runtime(&output_root, &chunk);
        let graph_before: Value = read_gzip_json(&graph);
        let graph_after: Value =
            read_gzip_json(&graph_for(&output_root.join("reports/tree"), &chunk));
        for change in plan["changes"].as_array().unwrap() {
            let unit_id = change["unit_id"].as_str().unwrap();
            assert_ne!(json_module_path_for_unit(&graph_before, unit_id), owner);
            assert_eq!(json_module_path_for_unit(&graph_after, unit_id), owner);
        }
        for binding in ["b", "c"] {
            let unit_id = unit_id_for_binding(&graph_after, binding);
            assert_eq!(
                json_module_path_for_unit(&graph_after, &unit_id),
                "kept",
                "{chunk}:{binding}"
            );
        }

        restore_tree(&fixture.modules, &original_modules);
        fs::write(&fixture.config, &original_config).unwrap();
        fs::write(&view, &original_view).unwrap();
    }

    assert_eq!(corpus["preparation"]["selected_programs"], 1, "{corpus}");
}

#[test]
fn corpus_does_not_share_preparation_across_distinct_selected_views() {
    let fixture = fixture();
    let view_a = write_view(&fixture.root, "selected-a.json", &one_binding_view("KeptB"));
    let view_b = write_view(&fixture.root, "selected-b.json", &one_binding_view("KeptC"));
    let original_modules = read_tree(&fixture.modules);
    let original_config = fs::read(&fixture.config).unwrap();
    let original_view_a = fs::read(&view_a).unwrap();
    let original_view_b = fs::read(&view_b).unwrap();

    let capture_a = fixture.root.join("capture-a");
    let capture_b = fixture.root.join("capture-b");
    for (output_root, view) in [(&capture_a, &view_a), (&capture_b, &view_b)] {
        let output = capture_selected(&fixture.root, output_root, view);
        assert!(
            output.status.success(),
            "selected capture failed:\n{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    // Each graph keeps its own program_graph reference and captured subset
    // provenance while sharing the current source, base spec and tree config.
    let graphs_root = fixture.root.join("mixed-graphs");
    fs::create_dir_all(graphs_root.join("alpha")).unwrap();
    fs::copy(
        capture_a.join("reports/tree/program_graph.json.gz"),
        graphs_root.join("program_graph.json.gz"),
    )
    .unwrap();
    fs::copy(
        graph_for(&capture_a.join("reports/tree"), "alpha"),
        graph_for(&graphs_root, "alpha"),
    )
    .unwrap();
    fs::create_dir_all(graphs_root.join("beta/beta")).unwrap();
    fs::copy(
        capture_b.join("reports/tree/program_graph.json.gz"),
        graphs_root.join("beta/program_graph.json.gz"),
    )
    .unwrap();
    fs::copy(
        graph_for(&capture_b.join("reports/tree"), "beta"),
        graph_for(&graphs_root.join("beta"), "beta"),
    )
    .unwrap();

    let output = run(&corpus_args(&fixture.root, &graphs_root));
    assert!(
        output.status.success(),
        "mixed selected corpus was refused:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let corpus = parse_stdout_json(&output);
    assert_eq!(corpus["status"], "found", "{corpus}");
    let rows = corpus["chunks"].as_array().unwrap();
    assert_eq!(rows.len(), 2, "{corpus}");
    for (row, chunk) in rows.iter().zip(["alpha", "beta"]) {
        assert_eq!(row["chunk_id"], chunk, "{row}");
        assert_eq!(row["search"]["status"], "found", "{row}");
        assert!(row["plan"].is_object(), "{row}");
        assert!(
            !row["plan"]["changes"].as_array().unwrap().is_empty(),
            "{row}"
        );
    }

    let alpha_graph: Value = read_gzip_json(&graph_for(&graphs_root, "alpha"));
    let beta_graph: Value = read_gzip_json(&graph_for(&graphs_root.join("beta"), "beta"));
    let alpha_b = unit_id_for_binding(&alpha_graph, "b");
    let alpha_c = unit_id_for_binding(&alpha_graph, "c");
    assert_eq!(json_module_path_for_unit(&alpha_graph, &alpha_b), "kept");
    assert_ne!(json_module_path_for_unit(&alpha_graph, &alpha_c), "kept");
    let beta_b = unit_id_for_binding(&beta_graph, "b");
    let beta_c = unit_id_for_binding(&beta_graph, "c");
    assert_ne!(json_module_path_for_unit(&beta_graph, &beta_b), "kept");
    assert_eq!(json_module_path_for_unit(&beta_graph, &beta_c), "kept");

    assert_eq!(read_tree(&fixture.modules), original_modules);
    assert_eq!(fs::read(&fixture.config).unwrap(), original_config);
    assert_eq!(fs::read(&view_a).unwrap(), original_view_a);
    assert_eq!(fs::read(&view_b).unwrap(), original_view_b);
    assert_eq!(corpus["preparation"]["selected_programs"], 2, "{corpus}");
}
