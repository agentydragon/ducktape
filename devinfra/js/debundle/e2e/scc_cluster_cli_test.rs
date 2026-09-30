//! E2e for `debundle scc` and `debundle cluster` against a synthetic
//! owner-graph fixture.

use debundle_e2e_support::{owner_graph, owner_node, run_debundle, write_text_file};
use serde_json::json;
use std::fs;

/// Build a small owner_graph.json with a 2-module SCC + a singleton.
fn synthetic_graph_json() -> String {
    let mut graph = owner_graph(
        "static/index",
        vec![
            owner_node("owner:0", 1, "XOe", "ui/plugins"),
            owner_node("owner:1", 2, "YOe", "residual"),
        ],
        vec![],
    );
    graph["module_graph"] = json!({
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
    });
    graph.to_string()
}

#[test]
fn scc_lists_every_scc_in_quotient() {
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write_text_file(&graph_path, &synthetic_graph_json());

    let out = run_debundle(&[
        "scc",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
    ]);
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
    write_text_file(&graph_path, &synthetic_graph_json());

    let out = run_debundle(&[
        "scc",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules.to_str().unwrap(),
        "--cycles-only",
        "--format",
        "json",
    ]);
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
    write_text_file(&graph_path, &synthetic_graph_json());

    let out = run_debundle(&[
        "scc",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules.to_str().unwrap(),
        "--binding",
        "XOe",
        "--format",
        "json",
    ]);
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
    write_text_file(&graph_path, &synthetic_graph_json());

    let out = run_debundle(&[
        "cluster",
        "XOe",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(out.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    // Each module-quotient node carries both its interned id and a
    // human path label (CLI_DOGFOOD #2). In this synthetic graph the
    // interned key already equals the path, so id == label here.
    assert_eq!(parsed["home_module"]["label"].as_str(), Some("ui/plugins"));
    assert_eq!(parsed["home_module"]["id"].as_str(), Some("ui/plugins"));
    assert_eq!(
        parsed["incoming_modules"][0]["label"].as_str(),
        Some("residual")
    );
    assert_eq!(
        parsed["outgoing_modules"][0]["label"].as_str(),
        Some("residual")
    );
}

#[test]
fn cluster_accepts_binding_flag_alias() {
    // CLI_DOGFOOD #1: `--binding <sym>` is accepted as an alias for the
    // positional `<sym>` (the spelling some operator skills document).
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules = dir.path().join("modules");
    fs::create_dir_all(&modules).unwrap();
    write_text_file(&graph_path, &synthetic_graph_json());

    let out = run_debundle(&[
        "cluster",
        "--binding",
        "XOe",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["home_module"]["label"].as_str(), Some("ui/plugins"));
}
