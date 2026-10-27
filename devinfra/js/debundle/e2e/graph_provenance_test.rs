//! Real CLI checks that a saved graph is joined only to its analyzed inputs.

use std::fs;
use std::path::Path;

use debundle_e2e_support::{
    FixtureOpts, GraphFixture, TreeFixture, debundler_path, run_fixture, run_tree_fixture,
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

#[test]
fn flat_graph_unit_inspection_checks_source_without_module_tree_scope() {
    let fixture = run_fixture(FixtureOpts::new(
        "const answer = 42; console.log(answer);\n",
        vec![],
    ));
    let root = fixture.out_root.parent().unwrap().parent().unwrap();
    let source_root = root.join("snapshot");
    let source = source_root.join(format!("{}.js", fixture.chunk_id));
    let modules = root.join("modules/main");
    let graph = fixture
        .report_root
        .join(&fixture.chunk_id)
        .join("dependency_graph.json");
    let unit = fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| node.id.starts_with("unit:"))
        .expect("flat fixture source unit")
        .id;
    let inspect = |selection: &str, selector_root: &Path| {
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
            .args(["--format", "json"])
            .output()
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
    let output = fixture
        .process(&["coverage", "--format", "json"])
        .env_remove("DEBUNDLE_SOURCE_ROOT")
        .output()
        .unwrap();
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
fn config_assumption_and_engine_change_reject() {
    let fixture = GraphFixture::acyclic_pair();
    let config = fixture.root().join("spec_config.yaml");
    let mut text = fs::read_to_string(&config).unwrap();
    text.push_str("chunk_analysis_options:\n  main:\n    trusted_dataflow_summaries: true\n");
    fs::write(config, text).unwrap();
    assert_mismatch(&fixture, "file");

    let fixture = GraphFixture::acyclic_pair();
    let mut graph: serde_json::Value =
        serde_json::from_slice(&fs::read(&fixture.graph).unwrap()).unwrap();
    graph["provenance"]["engine_sha256"] = "wrong-engine".into();
    fs::write(&fixture.graph, serde_json::to_vec(&graph).unwrap()).unwrap();
    assert_mismatch(&fixture, "engine");
}

#[test]
fn provider_chunk_analysis_option_invalidates_consumer_graph() {
    let run = run_tree_fixture(
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
    let output = std::process::Command::new(debundler_path())
        .args(["coverage", "--graph"])
        .arg(run.report_root.join("main/dependency_graph.json"))
        .arg("--modules")
        .arg(run.root().join("modules/main"))
        .args(["--format", "json"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1), "{}", stderr(&output));
    let rejection: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rejection["mismatch"]["reason"], "file");
}

#[test]
fn missing_metadata_cannot_authorize_edit_but_graph_only_read_works() {
    let fixture = GraphFixture::acyclic_pair();
    let mut graph: serde_json::Value =
        serde_json::from_slice(&fs::read(&fixture.graph).unwrap()).unwrap();
    graph.as_object_mut().unwrap().remove("provenance");
    fs::write(&fixture.graph, serde_json::to_vec(&graph).unwrap()).unwrap();
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
    let run = run_tree_fixture(
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
    let graph = run.report_root.join("main/dependency_graph.json");
    let wrong = run.root().join("modules/dependency");
    let output = std::process::Command::new(debundler_path())
        .arg("coverage")
        .arg("--graph")
        .arg(&graph)
        .arg("--modules")
        .arg(&wrong)
        .arg("--format")
        .arg("json")
        .output()
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
        .join("out/reports/tree/main/dependency_graph.json");
    let output = fixture
        .process(&[
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
        ])
        .output()
        .unwrap();
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
        .join("out/reports/tree/main/dependency_graph.json");
    let output = std::process::Command::new(debundler_path())
        .args([
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
        ])
        .output()
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
    let replay = std::process::Command::new(&command[0])
        .args(&command[1..])
        .current_dir(cwd)
        .output()
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
    let output = std::process::Command::new(debundler_path())
        .args([
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
        ])
        .output()
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
    let result = std::process::Command::new(debundler_path())
        .current_dir(fixture.root())
        .args([
            "run",
            "--tree-config",
            "spec_config.yaml",
            "--tree-modules",
            "modules",
            "--tree-vendor-marks",
            "vendor_marks.yaml",
        ])
        .env("DEBUNDLE_TREE_SOURCE_ROOT", fixture.root())
        .env("DEBUNDLE_OUT", &out)
        .output()
        .unwrap();
    assert!(result.status.success(), "{}", stderr(&result));
    let graph = out.join("reports/tree/main/dependency_graph.json");
    let report: serde_json::Value = serde_json::from_slice(&fs::read(&graph).unwrap()).unwrap();
    let provenance = &report["provenance"];
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
    let replay = std::process::Command::new(argv[0])
        .args(&argv[1..])
        .current_dir(other_cwd.path())
        .env_remove("DEBUNDLE_TREE_SOURCE_ROOT")
        .env_remove("DEBUNDLE_OUT")
        .output()
        .unwrap();
    assert!(replay.status.success(), "{}", stderr(&replay));
    assert!(graph.exists());
}
