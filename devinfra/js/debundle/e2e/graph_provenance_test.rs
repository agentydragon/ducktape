//! Real CLI checks that a saved graph is joined only to its analyzed inputs.

use debundle_e2e_graph_support::{FixtureGraphExt, GraphFixture, program_graph_path};
use std::fs;
use std::path::Path;
use std::process::Command;

use debundle_e2e_support::{
    FixtureOpts, Member, TreeFixture, assert_entry_output, assert_node_output, debundler_path,
    logical_module, node_module_evaluation_order, process, read_tree, run_fixture_with_graphs,
    run_tree_graph_fixture, tree_spec_args, unassigned_mode_inline,
};
use tempfile::TempDir;

fn stderr(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_mismatch(fixture: &GraphFixture, expected: &str) -> serde_json::Value {
    let output = fixture.command(&["coverage", "--format", "json"]);
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["kind"], "graph_compatibility");
    assert_eq!(json["mismatch"]["reason"], expected);
    assert!(json["mismatch"]["regenerate_command"].is_array());
    json
}

struct AliasGraphFixture {
    _root: TempDir,
    source_root: std::path::PathBuf,
    source_file: std::path::PathBuf,
    modules: std::path::PathBuf,
    graph: std::path::PathBuf,
}

impl AliasGraphFixture {
    fn new() -> Self {
        let root = TempDir::new().unwrap();
        let source_root = root.path().join("snapshot");
        let source_file = source_root.join("provider.js");
        fs::create_dir_all(&source_root).unwrap();
        fs::write(&source_file, "const value = 42;\nconsole.log(value);\n").unwrap();
        fs::write(
            root.path().join("js-files.txt"),
            "provider.js\tvirtual/chunk.js\n",
        )
        .unwrap();

        let modules = root.path().join("modules/provider");
        fs::create_dir_all(&modules).unwrap();
        fs::write(
            modules.join("provider.yaml"),
            "source_matches:\n  - match: 'const value = 42;'\n    bindings: [value]\n",
        )
        .unwrap();

        let config = root.path().join("spec_config.yaml");
        fs::write(
            &config,
            "main_chunk_id: provider\nmodule_roots:\n  provider: provider\ninputs:\n  root: snapshot\n  js_list_path: js-files.txt\nwrite_js_tree: true\nunassigned_mode:\n  provider: { kind: inline_in_entry }\nimport_order:\n  provider: [provider]\n",
        )
        .unwrap();
        let vendor_marks = root.path().join("vendor_marks.yaml");
        fs::write(&vendor_marks, "vendor_marks: []\n").unwrap();
        let out_root = root.path().join("out");
        let output = debundle_e2e_support::process::debundle_output(
            Command::new(debundler_path())
                .arg("run")
                .arg("--graphs")
                .args(tree_spec_args(root.path()))
                .arg("--out-root")
                .arg(&out_root),
        )
        .unwrap();
        process::assert_success(&output);

        Self {
            graph: out_root.join("reports/tree/provider/dependency_graph.json.gz"),
            _root: root,
            source_root,
            source_file,
            modules,
        }
    }

    fn command(
        &self,
        args: &[&str],
        selector_root: Option<&Path>,
        graph_source_root: Option<&Path>,
    ) -> std::process::Output {
        let mut command = Command::new(debundler_path());
        command
            .args(args)
            .arg("--graph")
            .arg(&self.graph)
            .arg("--modules")
            .arg(&self.modules)
            .args(["--format", "json"]);
        if let Some(selector_root) = selector_root {
            command.arg("--source-root").arg(selector_root);
        }
        if let Some(graph_source_root) = graph_source_root {
            command.arg("--graph-source-root").arg(graph_source_root);
        }
        if args.starts_with(&["modules", "propose"]) {
            command
                .arg("--tree-config")
                .arg(self._root.path().join("spec_config.yaml"));
        }
        debundle_e2e_support::process::debundle_output(&mut command)
            .unwrap_or_else(|err| panic!("{err}"))
    }
}

fn query_json(output: &std::process::Output) -> serde_json::Value {
    process::assert_success(output);
    serde_json::from_slice(&output.stdout).unwrap()
}

fn assert_graph_mismatch(output: &std::process::Output, expected: &str) {
    assert_eq!(output.status.code(), Some(1), "{}", stderr(output));
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["kind"], "graph_compatibility");
    assert_eq!(json["mismatch"]["reason"], expected);
}

#[test]
fn aliased_chunk_sources_use_the_verified_physical_input_for_queries() {
    let fixture = AliasGraphFixture::new();

    // The authored source_match is resolved by coverage and proposal queries.
    for output in [
        fixture.command(&["coverage"], None, None),
        fixture.command(&["coverage"], Some(&fixture.source_root), None),
    ] {
        let report = query_json(&output);
        assert_eq!(
            report["rows"][0]["requested_binding_ids"],
            serde_json::json!(["value"])
        );
    }
    let proposal =
        query_json(&fixture.command(&["modules", "propose"], Some(&fixture.source_root), None));
    assert_eq!(proposal["status"], "found", "{proposal}");
    assert!(proposal["plan"].is_object(), "{proposal}");

    let describe =
        query_json(&fixture.command(&["describe", "value"], Some(&fixture.source_root), None));
    assert_eq!(describe["source_unit_ids"], serde_json::json!(["unit:0"]));
    let inspected = query_json(&fixture.command(&["inspect-source", "--id", "unit:0"], None, None));
    assert!(
        inspected["files"][0]["statements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|statement| statement["source"].as_str().unwrap().contains("value = 42"))
    );

    let plan = fixture._root.path().join("peel.json");
    fs::write(&plan, serde_json::to_vec(&proposal["plan"]).unwrap()).unwrap();
    let config = fixture._root.path().join("spec_config.yaml");
    let before = read_tree(&fixture.modules);
    let config_before = fs::read(&config).unwrap();
    for mut args in [
        vec!["modules", "check"],
        vec!["modules", "apply", "--dry-run"],
    ] {
        args.extend([
            "--plan",
            plan.to_str().unwrap(),
            "--tree-config",
            config.to_str().unwrap(),
        ]);
        process::assert_success(&fixture.command(&args, Some(&fixture.source_root), None));
        assert_eq!(read_tree(&fixture.modules), before);
        assert_eq!(fs::read(&config).unwrap(), config_before);
    }
    process::assert_success(&fixture.command(
        &[
            "modules",
            "apply",
            "--plan",
            plan.to_str().unwrap(),
            "--tree-config",
            config.to_str().unwrap(),
        ],
        Some(&fixture.source_root),
        None,
    ));
    let out_root = fixture._root.path().join("after-peel");
    let materialized = debundle_e2e_support::process::debundle_output(
        Command::new(debundler_path())
            .arg("run")
            .args(tree_spec_args(fixture._root.path()))
            .arg("--out-root")
            .arg(&out_root),
    )
    .unwrap();
    process::assert_success(&materialized);
    assert_node_output(&out_root.join("app/provider/entry.js"), "42\n", "");
}

#[test]
fn aliased_source_match_remains_resolvable_by_the_graph_edit_gate() {
    let fixture = AliasGraphFixture::new();
    let import_order = fixture._root.path().join("import-order.json");
    fs::write(&import_order, serde_json::to_vec(&["next"]).unwrap()).unwrap();
    let import_order = import_order.to_str().unwrap();
    let output = fixture.command(
        &[
            "bindings",
            "assign",
            "value:next",
            "--import-order",
            import_order,
        ],
        None,
        None,
    );
    process::assert_success(&output);
    assert!(fixture.modules.join("next.yaml").is_file());

    assert_graph_mismatch(
        &fixture.command(&["coverage"], None, None),
        "yaml_membership",
    );
}

#[test]
fn aliased_chunk_sources_keep_root_and_shadow_guards_during_relocation() {
    let fixture = AliasGraphFixture::new();
    let wrong_root = TempDir::new().unwrap();
    fs::copy(&fixture.source_file, wrong_root.path().join("provider.js")).unwrap();
    assert_graph_mismatch(
        &fixture.command(&["coverage"], Some(wrong_root.path()), None),
        "selector_source_root",
    );
    assert_graph_mismatch(
        &fixture.command(&["coverage"], Some(&fixture.source_file), None),
        "selector_source_root",
    );

    let logical_shadow = fixture.source_root.join("virtual/chunk.js");
    fs::create_dir_all(logical_shadow.parent().unwrap()).unwrap();
    fs::copy(&fixture.source_file, &logical_shadow).unwrap();
    assert_graph_mismatch(
        &fixture.command(&["coverage"], Some(&fixture.source_root), None),
        "selector_source_root",
    );
    fs::remove_file(logical_shadow).unwrap();

    let logical_directory = fixture.source_root.join("virtual/chunk.js");
    fs::create_dir_all(&logical_directory).unwrap();
    assert_graph_mismatch(
        &fixture.command(&["coverage"], Some(&fixture.source_root), None),
        "selector_source_root",
    );
    fs::remove_dir_all(logical_directory).unwrap();

    let fallback_shadow = fixture.modules.parent().unwrap().join("virtual/chunk.js");
    fs::create_dir_all(fallback_shadow.parent().unwrap()).unwrap();
    fs::copy(&fixture.source_file, &fallback_shadow).unwrap();
    assert_graph_mismatch(
        &fixture.command(&["coverage"], None, None),
        "selector_source_root",
    );
    fs::remove_file(fallback_shadow).unwrap();

    let relocated_root = fixture._root.path().join("relocated-source");
    fs::create_dir_all(relocated_root.join("snapshot")).unwrap();
    fs::copy(
        &fixture.source_file,
        relocated_root.join("snapshot/provider.js"),
    )
    .unwrap();
    fs::copy(
        fixture._root.path().join("js-files.txt"),
        relocated_root.join("js-files.txt"),
    )
    .unwrap();
    fs::remove_file(&fixture.source_file).unwrap();
    let relocated_coverage =
        query_json(&fixture.command(&["coverage"], None, Some(&relocated_root)));
    assert_eq!(
        relocated_coverage["rows"][0]["requested_binding_ids"],
        serde_json::json!(["value"])
    );
    let relocated_selector_root = relocated_root.join("snapshot");
    let relocated_inspection = query_json(&fixture.command(
        &["inspect-source", "--id", "unit:0"],
        Some(&relocated_selector_root),
        Some(&relocated_root),
    ));
    assert!(
        relocated_inspection["files"][0]["statements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|statement| statement["source"].as_str().unwrap().contains("value = 42"))
    );
    let relocated_coverage_with_selector_root = query_json(&fixture.command(
        &["coverage"],
        Some(&relocated_selector_root),
        Some(&relocated_root),
    ));
    assert_eq!(
        relocated_coverage_with_selector_root["rows"][0]["requested_binding_ids"],
        serde_json::json!(["value"])
    );

    fs::write(
        relocated_root.join("snapshot/provider.js"),
        "const value = 43;\nconsole.log(value);\n",
    )
    .unwrap();
    assert_graph_mismatch(
        &fixture.command(&["coverage"], None, Some(&relocated_root)),
        "file",
    );
}

#[test]
fn flat_graph_unit_inspection_checks_source_without_module_tree_scope() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new("const answer = 42; console.log(answer);\n", vec![])
            .with_import_order(&["residual"]),
    );
    let root = fixture.out_root.parent().unwrap().parent().unwrap();
    let source_root = root.join("snapshot");
    let source = source_root.join(format!("{}.js", fixture.chunk_id));
    let modules = root.join("modules/main");
    let graph = fixture
        .report_root
        .join(&fixture.chunk_id)
        .join("dependency_graph.json.gz");
    let unit = fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| node.id.starts_with("unit:"))
        .expect("flat fixture source unit")
        .id;
    let inspect = |selection: &str, selector_root: &Path| {
        debundle_e2e_support::process::debundle_output(
            std::process::Command::new(debundler_path())
                .arg("inspect-source")
                .arg("--id")
                .arg(selection)
                .arg("--graph")
                .arg(&graph)
                .arg("--modules")
                .arg(&modules)
                .arg("--source-root")
                .arg(selector_root)
                .args(["--format", "json"]),
        )
        .unwrap()
    };
    let output = inspect(&unit, &source_root);
    assert!(output.status.success(), "{}", stderr(&output));
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(
        result["files"][0]["statements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|statement| statement["source_unit_ids"]
                .as_array()
                .unwrap()
                .iter()
                .any(|id| id.as_str() == Some(unit.as_str())))
    );

    let output = inspect("answer", &source_root);
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "module_scope");

    let wrong_root = TempDir::new().unwrap();
    let wrong_source = wrong_root.path().join(format!("{}.js", fixture.chunk_id));
    fs::create_dir_all(wrong_source.parent().unwrap()).unwrap();
    fs::copy(&source, wrong_source).unwrap();
    let output = inspect(&unit, wrong_root.path());
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "selector_source_root");

    fs::write(&source, "const answer = 43; console.log(answer);\n").unwrap();
    let output = inspect(&unit, &source_root);
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "file");
}

#[test]
fn unchanged_graph_matches_and_source_change_rejects() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_success(&["coverage", "--format", "json"]);
    fs::write(
        fixture.source_path(),
        "const beta = 2;\nconst alpha = beta + 1;\nconsole.log(alpha);\n",
    )
    .unwrap();
    let mismatch = assert_mismatch(&fixture, "file");
    assert_eq!(
        mismatch["mismatch"]["path"],
        fixture.source_path().to_str().unwrap()
    );
}

#[test]
fn selector_root_must_resolve_to_analyzed_source_file() {
    let fixture = GraphFixture::acyclic_pair();
    let other = TempDir::new().unwrap();
    fs::copy(fixture.source_path(), other.path().join("main.js")).unwrap();
    let output = fixture.command(&[
        "coverage",
        "--source-root",
        other.path().to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "selector_source_root");
}

#[test]
fn fallback_source_search_cannot_shadow_analyzed_file() {
    let fixture = GraphFixture::acyclic_pair();
    let shadow = fixture.modules.parent().unwrap().join("main.js");
    fs::copy(fixture.source_path(), shadow).unwrap();
    let output = debundle_e2e_support::process::debundle_output(
        fixture
            .process(&["coverage", "--format", "json"])
            .env_remove("DEBUNDLE_SOURCE_ROOT"),
    )
    .unwrap_or_else(|err| panic!("{err}"));
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "selector_source_root");
}

#[test]
fn existing_added_and_deleted_yaml_reject_before_edit() {
    for mutation in ["existing", "added", "deleted"] {
        let fixture = GraphFixture::acyclic_pair();
        match mutation {
            "existing" => fs::write(fixture.modules.join("a.yaml"), "members: []\n").unwrap(),
            "added" => fs::write(fixture.modules.join("extra.yaml"), "members: []\n").unwrap(),
            "deleted" => fs::remove_file(fixture.modules.join("a.yaml")).unwrap(),
            _ => unreachable!(),
        }
        let before = fs::read(fixture.modules.join("b.yaml")).unwrap();
        let output = fixture.command(&["bindings", "assign", "beta:a"]);
        assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
        let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(
            rejection["mismatch"]["reason"],
            if mutation == "existing" {
                "file"
            } else {
                "yaml_membership"
            }
        );
        assert_eq!(fs::read(fixture.modules.join("b.yaml")).unwrap(), before);
    }
}

#[test]
fn initializer_source_only_edit_invalidates_saved_graph_authority() {
    let fixture = GraphFixture::acyclic_pair();
    fixture.assert_success(&["coverage", "--format", "json"]);
    let module = fixture.modules.join("a.yaml");
    let mut yaml = fs::read_to_string(&module).unwrap();
    yaml.push_str("annotations:\n  alpha:\n    initializer_binding_effects: source_only\n");
    fs::write(&module, yaml).unwrap();
    let rejection = assert_mismatch(&fixture, "file");
    assert_eq!(rejection["mismatch"]["path"], module.to_str().unwrap());
}

#[test]
fn config_assumption_and_engine_change_reject() {
    let fixture = GraphFixture::acyclic_pair();
    let config = fixture.root().join("spec_config.yaml");
    let mut text = fs::read_to_string(&config).unwrap();
    text.push_str("chunk_analysis_options:\n  main:\n    trusted_dataflow_summaries: true\n");
    fs::write(config, text).unwrap();
    assert_mismatch(&fixture, "file");

    let fixture = GraphFixture::acyclic_pair();
    let path = program_graph_path(&fixture.graph);
    let mut program_graph: serde_json::Value = report_io::read_json_gzip(&path).unwrap();
    program_graph["provenance"]["engine_sha256"] = "wrong-engine".into();
    report_io::write_json_gzip(&path, &program_graph).unwrap();
    assert_mismatch(&fixture, "engine");
}

#[test]
fn provider_chunk_analysis_option_invalidates_consumer_graph() {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "const mainValue = 1; console.log(mainValue);"),
                (
                    "provider",
                    "const providerValue = 2; console.log(providerValue);",
                ),
            ],
            module_roots: &[("main", "main"), ("provider", "provider")],
            modules: &[],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let config = run.root().join("spec_config.yaml");
    let mut text = fs::read_to_string(&config).unwrap();
    text.push_str("chunk_analysis_options:\n  provider:\n    trusted_dataflow_summaries: true\n");
    fs::write(config, text).unwrap();
    let output = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path())
            .args(["coverage", "--graph"])
            .arg(run.report_root.join("main/dependency_graph.json.gz"))
            .arg("--modules")
            .arg(run.root().join("modules/main"))
            .args(["--format", "json"]),
    )
    .unwrap();
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "file");
}

#[test]
fn missing_metadata_cannot_authorize_edit_but_graph_only_read_works() {
    let fixture = GraphFixture::acyclic_pair();
    let path = program_graph_path(&fixture.graph);
    let mut program_graph: serde_json::Value = report_io::read_json_gzip(&path).unwrap();
    program_graph.as_object_mut().unwrap().remove("provenance");
    report_io::write_json_gzip(&path, &program_graph).unwrap();
    fixture.assert_success(&["atoms", "--format", "json"]);
    assert_mismatch(&fixture, "missing_provenance");
}

#[test]
fn accepted_edit_uses_baseline_then_old_graph_becomes_stale() {
    let fixture = GraphFixture::acyclic_pair();
    let output = fixture.command(&["bindings", "assign", "beta:a"]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(stderr(&output).contains("pre-edit"));
    assert_mismatch(&fixture, "yaml_membership");
}

#[test]
fn preview_and_no_op_keep_graph_compatible() {
    let fixture = GraphFixture::acyclic_pair();
    let preview = fixture.command(&["bindings", "assign", "alpha:a", "--dry-run"]);
    assert!(preview.status.success(), "{}", stderr(&preview));
    assert!(!stderr(&preview).contains("pre-edit"));
    fixture.assert_success(&["coverage", "--format", "json"]);
    fixture.assert_success(&["bindings", "assign", "alpha:a"]);
    fixture.assert_success(&["coverage", "--format", "json"]);
}

#[test]
fn wrong_chunk_scope_with_same_binding_is_rejected() {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "const shared = 1; console.log(shared);"),
                ("dependency", "const shared = 2; console.log(shared);"),
            ],
            module_roots: &[("main", "main"), ("dependency", "dependency")],
            modules: &[
                (
                    "main/owned.yaml",
                    "members: [{selector: {binding: {name: shared}}}]\n",
                ),
                (
                    "dependency/owned.yaml",
                    "members: [{selector: {binding: {name: shared}}}]\n",
                ),
            ],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let graph = run.report_root.join("main/dependency_graph.json.gz");
    let wrong = run.root().join("modules/dependency");
    let output = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path())
            .arg("coverage")
            .arg("--graph")
            .arg(&graph)
            .arg("--modules")
            .arg(&wrong)
            .arg("--format")
            .arg("json"),
    )
    .unwrap();
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "module_scope");
    assert_eq!(rejection["mismatch"]["chunk_id"], "main");
    assert_eq!(rejection["mismatch"]["actual"], wrong.to_str().unwrap());
}

fn copy_tree(source: &Path, destination: &Path) {
    fs::create_dir_all(destination).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let from = entry.path();
        let to = destination.join(entry.file_name());
        if from.is_dir() {
            copy_tree(&from, &to);
        } else {
            fs::copy(&from, &to).unwrap();
        }
    }
}

#[test]
fn explicit_roots_allow_relocated_graph_and_spec() {
    let fixture = GraphFixture::acyclic_pair();
    let relocated = TempDir::new().unwrap();
    copy_tree(fixture.root(), relocated.path());
    let graph = relocated
        .path()
        .join("out/reports/tree/main/dependency_graph.json.gz");
    let output = debundle_e2e_support::process::debundle_output(&mut fixture.process(&[
        "--spec-root",
        relocated.path().to_str().unwrap(),
        "--graph-source-root",
        relocated.path().to_str().unwrap(),
        "coverage",
        "--graph",
        graph.to_str().unwrap(),
        "--modules",
        relocated.path().join("modules/main").to_str().unwrap(),
        "--source-root",
        relocated.path().join("snapshot").to_str().unwrap(),
        "--format",
        "json",
    ]))
    .unwrap_or_else(|err| panic!("{err}"));
    assert!(output.status.success(), "{}", stderr(&output));
}

#[test]
fn relocated_mismatch_recipe_rebuilds_after_original_inputs_are_removed() {
    let fixture = GraphFixture::acyclic_pair();
    let original = fixture.root().to_path_buf();
    let relocated = TempDir::new().unwrap();
    copy_tree(&original, relocated.path());
    fs::remove_dir_all(&original).unwrap();
    let source = relocated.path().join("snapshot/main.js");
    let mut source_bytes = fs::read(&source).unwrap();
    source_bytes.extend_from_slice(b"\n// changed after snapshot\n");
    fs::write(&source, source_bytes).unwrap();
    // The replay recipe must avoid both the populated original output and an
    // existing output from a previous regeneration attempt.
    fs::create_dir(relocated.path().join("out-regenerated")).unwrap();
    let graph = relocated
        .path()
        .join("out/reports/tree/main/dependency_graph.json.gz");
    let output = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path()).args([
            "--spec-root",
            relocated.path().to_str().unwrap(),
            "--graph-source-root",
            relocated.path().to_str().unwrap(),
            "coverage",
            "--graph",
            graph.to_str().unwrap(),
            "--modules",
            relocated.path().join("modules/main").to_str().unwrap(),
            "--source-root",
            relocated.path().join("snapshot").to_str().unwrap(),
            "--format",
            "json",
        ]),
    )
    .unwrap();
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "file");
    let command = rejection["mismatch"]["regenerate_command"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    assert!(
        command
            .iter()
            .any(|arg| arg == relocated.path().join("out-regenerated-1").to_str().unwrap())
    );
    assert!(
        command
            .iter()
            .all(|arg| !arg.contains(&*original.to_string_lossy()))
    );
    let cwd = rejection["mismatch"]["regenerate_cwd"].as_str().unwrap();
    assert_eq!(cwd, relocated.path().to_str().unwrap());
    let replay = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(&command[0])
            .args(&command[1..])
            .current_dir(cwd),
    )
    .unwrap();
    assert!(replay.status.success(), "{}", stderr(&replay));
}

#[test]
fn source_only_relocation_keeps_spec_paths_in_regeneration_recipe() {
    let fixture = GraphFixture::acyclic_pair();
    let relocated_source = TempDir::new().unwrap();
    copy_tree(fixture.root(), relocated_source.path());
    fs::write(
        relocated_source.path().join("snapshot/main.js"),
        "const beta = 3; const alpha = beta + 1; console.log(alpha);\n",
    )
    .unwrap();
    let output = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path()).args([
            "--graph-source-root",
            relocated_source.path().to_str().unwrap(),
            "coverage",
            "--graph",
            fixture.graph.to_str().unwrap(),
            "--modules",
            fixture.modules.to_str().unwrap(),
            "--source-root",
            relocated_source.path().join("snapshot").to_str().unwrap(),
            "--format",
            "json",
        ]),
    )
    .unwrap();
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "file");
    let argv = rejection["mismatch"]["regenerate_command"]
        .as_array()
        .unwrap();
    let flag_value = |flag: &str| {
        let index = argv
            .iter()
            .position(|arg| arg.as_str() == Some(flag))
            .unwrap();
        argv[index + 1].as_str().unwrap()
    };
    assert_eq!(
        flag_value("--tree-config"),
        fixture.root().join("spec_config.yaml").to_str().unwrap()
    );
    assert_eq!(
        flag_value("--tree-modules"),
        fixture.root().join("modules").to_str().unwrap()
    );
    assert_eq!(
        flag_value("--tree-source-root"),
        relocated_source.path().to_str().unwrap()
    );
}

#[test]
fn env_only_run_records_replayable_absolute_argv_across_cwd() {
    let fixture = GraphFixture::acyclic_pair();
    let out = fixture.root().join("env_out");
    let result = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path())
            .current_dir(fixture.root())
            .args([
                "run",
                "--graphs",
                "--tree-config",
                "spec_config.yaml",
                "--tree-modules",
                "modules",
                "--tree-vendor-marks",
                "vendor_marks.yaml",
            ])
            .env("DEBUNDLE_TREE_SOURCE_ROOT", fixture.root())
            .env("DEBUNDLE_OUT", &out),
    )
    .unwrap();
    assert!(result.status.success(), "{}", stderr(&result));
    let graph = out.join("reports/tree/main/dependency_graph.json.gz");
    let program_graph: serde_json::Value =
        report_io::read_json_gzip(&program_graph_path(&graph)).unwrap();
    let provenance = &program_graph["provenance"];
    assert_eq!(
        provenance["generation_cwd"],
        fixture.root().to_str().unwrap()
    );
    let argv = provenance["generation_command"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| arg.as_str().unwrap())
        .collect::<Vec<_>>();
    assert!(argv.contains(&"--tree-source-root"));
    assert!(argv.contains(&"--out-root"));
    assert!(argv.contains(&fixture.root().to_str().unwrap()));
    assert!(argv.contains(&out.to_str().unwrap()));
    fs::remove_dir_all(&out).unwrap();
    let other_cwd = TempDir::new().unwrap();
    let replay = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(argv[0])
            .args(&argv[1..])
            .current_dir(other_cwd.path())
            .env_remove("DEBUNDLE_TREE_SOURCE_ROOT")
            .env_remove("DEBUNDLE_OUT"),
    )
    .unwrap();
    assert!(replay.status.success(), "{}", stderr(&replay));
    assert!(graph.exists());
}

#[test]
fn exported_priority_captures_the_materialized_cycle_traversal() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"const hub = () => [a, b];
const a = { dependency: hub, lazy: () => b };
const b = { dependency: hub };
console.log(a.dependency === hub, a.lazy() === b, b.dependency === hub);
"#,
            vec![
                logical_module("hub", &[Member::new("hub")]),
                logical_module("a", &[Member::new("a")]),
                logical_module("b", &[Member::new("b")]),
            ],
        )
        .with_import_order(&["a", "b", "hub"])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_entry_output(&fixture, "true true true\n");
    let priority: Vec<_> = fixture
        .dependency_graph()
        .import_order
        .iter()
        .map(ToString::to_string)
        .collect();
    let mut owners = priority.clone();
    owners.sort();
    assert_eq!(owners, ["a", "b", "hub"]);
    // Independent DFS over this fixture's source-known import edges. This
    // also covers a supplied priority that differs from discovery order.
    fn visit(
        module: &str,
        priority: &[String],
        seen: &mut std::collections::BTreeSet<String>,
        completed: &mut Vec<String>,
    ) {
        if !seen.insert(module.to_owned()) {
            return;
        }
        let targets: &[&str] = match module {
            "entry" => &["a", "b", "hub"],
            "hub" => &["a", "b"],
            "a" => &["b", "hub"],
            "b" => &["hub"],
            _ => panic!("unexpected fixture module {module}"),
        };
        let mut targets = targets.to_vec();
        targets.sort_by_key(|target| priority.iter().position(|owner| owner == target).unwrap());
        for target in targets {
            visit(target, priority, seen, completed);
        }
        completed.push(module.to_owned());
    }
    let mut expected = Vec::new();
    visit("entry", &priority, &mut Default::default(), &mut expected);
    assert_eq!(
        node_module_evaluation_order(&fixture, &["a", "b", "hub", "entry"]),
        expected,
    );
}
