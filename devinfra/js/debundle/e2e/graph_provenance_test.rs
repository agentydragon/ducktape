//! Real CLI checks that a saved graph is joined only to its analyzed inputs.

use std::fs;
use std::path::Path;

use debundle_e2e_support::{GraphFixture, TreeFixture, debundler_path, run_tree_fixture};
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
