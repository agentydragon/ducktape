//! E2e for `debundle scc` and `debundle cluster` against a synthetic
//! owner-graph fixture.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn debundle_binary() -> PathBuf {
    let runfiles_path = std::env::var("RUNFILES_DIR")
        .or_else(|_| std::env::var("TEST_SRCDIR"))
        .expect("runfiles env var");
    Path::new(&runfiles_path).join("_main/devinfra/js/debundle/debundle")
}

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

/// Build a small owner_graph.json with a 2-module SCC + a singleton.
fn synthetic_graph_json() -> String {
    serde_json::json!({
        "chunk_id": "static/index",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 1,
                "source_location": null,
                "declared_bindings": [
                    { "binding": "XOe", "export_name": "PluginAccessor" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "ui/plugins"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 2,
                "source_location": null,
                "declared_bindings": [
                    { "binding": "YOe", "export_name": "YOe" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "residual"
            }
        ],
        "edges": [],
        "module_graph": {
            "nodes": [
                { "key": "ui/plugins", "path": "ui/plugins", "residual": false },
                { "key": "residual", "path": "residual", "residual": true },
                { "key": "isolated", "path": "isolated", "residual": false }
            ],
            "edges": [
                {
                    "id": "q_edge:0",
                    "source": "ui/plugins",
                    "target": "residual",
                    "edge_kinds": ["eager_use"],
                    "constrains_init_order": true
                },
                {
                    "id": "q_edge:1",
                    "source": "residual",
                    "target": "ui/plugins",
                    "edge_kinds": ["eager_use"],
                    "constrains_init_order": true
                }
            ],
            "sccs": [
                {
                    "id": "scc:0",
                    "modules": ["ui/plugins", "residual"],
                    "is_cycle": true,
                    "realizable": false,
                    "module_edge_ids": ["q_edge:0", "q_edge:1"],
                    "constraining_module_edge_ids": ["q_edge:0", "q_edge:1"]
                },
                {
                    "id": "scc:1",
                    "modules": ["isolated"],
                    "is_cycle": false,
                    "realizable": true,
                    "module_edge_ids": [],
                    "constraining_module_edge_ids": []
                }
            ]
        },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

#[test]
fn scc_lists_every_scc_in_quotient() {
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write(&graph_path, &synthetic_graph_json());

    let out = Command::new(debundle_binary())
        .args([
            "scc",
            "--graph",
            graph_path.to_str().unwrap(),
            "--modules",
            modules.to_str().unwrap(),
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "scc exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["sccs"].as_array().unwrap().len(), 2);
}

#[test]
fn scc_cycles_only_filter() {
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write(&graph_path, &synthetic_graph_json());

    let out = Command::new(debundle_binary())
        .args([
            "scc",
            "--graph",
            graph_path.to_str().unwrap(),
            "--modules",
            modules.to_str().unwrap(),
            "--cycles-only",
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(out.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let sccs = parsed["sccs"].as_array().unwrap();
    assert_eq!(sccs.len(), 1);
    assert_eq!(sccs[0]["id"].as_str(), Some("scc:0"));
}

#[test]
fn scc_binding_filter() {
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write(&graph_path, &synthetic_graph_json());

    let out = Command::new(debundle_binary())
        .args([
            "scc",
            "--graph",
            graph_path.to_str().unwrap(),
            "--modules",
            modules.to_str().unwrap(),
            "--binding",
            "XOe",
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(out.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let sccs = parsed["sccs"].as_array().unwrap();
    assert_eq!(sccs.len(), 1);
}

#[test]
fn cluster_emits_quotient_neighbors() {
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write(&graph_path, &synthetic_graph_json());

    let out = Command::new(debundle_binary())
        .args([
            "cluster",
            "XOe",
            "--graph",
            graph_path.to_str().unwrap(),
            "--modules",
            modules.to_str().unwrap(),
            "--format",
            "json",
        ])
        .output()
        .expect("spawn debundle");
    assert!(out.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["home_module"].as_str(), Some("ui/plugins"));
    assert_eq!(parsed["incoming_modules"][0].as_str(), Some("residual"));
    assert_eq!(parsed["outgoing_modules"][0].as_str(), Some("residual"));
}
