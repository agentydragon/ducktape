//! E2e for `debundle gate {list,describe,cut}` against a synthetic
//! `owner_graph.json` + `cycles.json` pair. Exercises the binary,
//! so the env-var / flag plumbing is covered alongside the report
//! shapes.

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

/// Two-module cycle `mod_a <-> mod_b`. Each module has a single
/// owner; the edge from `mod_a` to `mod_b` is at-init on binding
/// `b1` and the reverse is at-init on `a1`. Both endpoints are
/// in the SCC, so `gate describe 0` should recompute exactly two
/// evidence edges.
fn synthetic_graph_json() -> String {
    serde_json::json!({
        "chunk_id": "static/app",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "source_location": null,
                "declared_bindings": [
                    { "binding": "a1", "export_name": "a1" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "mod_a"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "source_location": null,
                "declared_bindings": [
                    { "binding": "b1", "export_name": "b1" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "mod_b"
            }
        ],
        "edges": [
            {
                "id": "e0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_use",
                "binding": "b1",
                "statement_ordinal": 0,
                "constrains_init_order": true
            },
            {
                "id": "e1",
                "source": "owner:1",
                "target": "owner:0",
                "edge_kind": "eager_use",
                "binding": "a1",
                "statement_ordinal": 1,
                "constrains_init_order": true
            }
        ],
        "module_graph": {
            "nodes": [
                { "key": "mod_a", "path": "mod_a", "residual": false },
                { "key": "mod_b", "path": "mod_b", "residual": false }
            ],
            "edges": [],
            "sccs": []
        },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

/// Trimmed `cycles.json` shape: one blocking SCC with `id`, `modules`,
/// and `cut`. Mirrors what the materializer writes after the wire-shape
/// trim (no `evidence` field).
fn synthetic_cycles_json() -> String {
    serde_json::json!([
        {
            "id": 0,
            "modules": ["mod_a", "mod_b"],
            "cut": [
                {
                    "from": "mod_a",
                    "to": "mod_b",
                    "statement_ordinal": 0,
                    "binding": "b1",
                    "from_binding": "a1",
                    "kind": "eager_use"
                }
            ]
        }
    ])
    .to_string()
}

struct Fixture {
    _dir: tempfile::TempDir,
    graph_path: PathBuf,
    modules_root: PathBuf,
}

fn fixture_with_default_cycles_layout() -> Fixture {
    // Default `cycles.json` location: sibling of `--graph`. Place
    // both under the same parent dir so `gate ...` resolves cycles.json
    // without an explicit `--cycles` flag.
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("reports/static/app/owner_graph.json");
    let cycles_path = dir.path().join("reports/static/app/cycles.json");
    let modules_root = dir.path().join("modules");
    fs::create_dir_all(&modules_root).unwrap();
    write(&graph_path, &synthetic_graph_json());
    write(&cycles_path, &synthetic_cycles_json());
    Fixture {
        _dir: dir,
        graph_path,
        modules_root,
    }
}

fn run_gate(args: &[&str]) -> std::process::Output {
    Command::new(debundle_binary())
        .args(args)
        .output()
        .expect("spawn debundle")
}

#[test]
fn gate_list_reports_each_blocking_scc() {
    let fx = fixture_with_default_cycles_layout();
    let out = run_gate(&[
        "gate",
        "list",
        "--graph",
        fx.graph_path.to_str().unwrap(),
        "--modules",
        fx.modules_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "gate list exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let entries = parsed["blocking_sccs"].as_array().unwrap();
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0]["id"].as_u64(), Some(0));
    assert_eq!(entries[0]["module_count"].as_u64(), Some(2));
    assert_eq!(entries[0]["cut_count"].as_u64(), Some(1));
}

#[test]
fn gate_cut_returns_the_actionable_edges() {
    let fx = fixture_with_default_cycles_layout();
    let out = run_gate(&[
        "gate",
        "cut",
        "0",
        "--graph",
        fx.graph_path.to_str().unwrap(),
        "--modules",
        fx.modules_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "gate cut exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["id"].as_u64(), Some(0));
    let cut = parsed["cut"].as_array().unwrap();
    assert_eq!(cut.len(), 1);
    assert_eq!(cut[0]["from"].as_str(), Some("mod_a"));
    assert_eq!(cut[0]["to"].as_str(), Some("mod_b"));
    assert_eq!(cut[0]["kind"].as_str(), Some("eager_use"));
}

#[test]
fn gate_describe_recomputes_evidence_from_owner_graph() {
    let fx = fixture_with_default_cycles_layout();
    let out = run_gate(&[
        "gate",
        "describe",
        "0",
        "--graph",
        fx.graph_path.to_str().unwrap(),
        "--modules",
        fx.modules_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "gate describe exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["id"].as_u64(), Some(0));
    let modules = parsed["modules"].as_array().unwrap();
    assert_eq!(modules.len(), 2);
    let evidence = parsed["evidence"].as_array().unwrap();
    // Two intra-SCC cross-module owner edges; both should be in
    // the recomputed evidence (cycles.json carries no evidence
    // — the CLI recomputed it from owner_graph.json).
    assert_eq!(evidence.len(), 2, "{parsed}");
    assert!(
        evidence
            .iter()
            .any(|e| e["from"] == "mod_a" && e["to"] == "mod_b" && e["binding"] == "b1"),
        "evidence missing mod_a -> mod_b: {parsed}"
    );
    assert!(
        evidence
            .iter()
            .any(|e| e["from"] == "mod_b" && e["to"] == "mod_a" && e["binding"] == "a1"),
        "evidence missing mod_b -> mod_a: {parsed}"
    );
}

#[test]
fn gate_describe_binding_filter_narrows_evidence_to_one_symbol() {
    let fx = fixture_with_default_cycles_layout();
    let out = run_gate(&[
        "gate",
        "describe",
        "0",
        "--graph",
        fx.graph_path.to_str().unwrap(),
        "--modules",
        fx.modules_root.to_str().unwrap(),
        "--binding",
        "a1",
        "--format",
        "json",
    ]);
    assert!(out.status.success());
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let evidence = parsed["evidence"].as_array().unwrap();
    // `a1` is the source binding for mod_a -> mod_b AND the target
    // binding for mod_b -> mod_a; both rows are kept.
    assert_eq!(evidence.len(), 2);
    for e in evidence {
        assert!(
            e["binding"] == "a1" || e["from_binding"] == "a1",
            "binding filter kept an unrelated row: {e}"
        );
    }
}

#[test]
fn gate_unknown_id_fails_cleanly() {
    let fx = fixture_with_default_cycles_layout();
    let out = run_gate(&[
        "gate",
        "describe",
        "99",
        "--graph",
        fx.graph_path.to_str().unwrap(),
        "--modules",
        fx.modules_root.to_str().unwrap(),
    ]);
    assert!(!out.status.success(), "describe 99 should fail");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("no blocking SCC with id 99"),
        "stderr: {stderr}"
    );
}

#[test]
fn gate_cycles_override_picks_up_custom_path() {
    // Put cycles.json somewhere other than the graph's sibling and
    // make sure `--cycles` finds it.
    let dir = tempfile::tempdir().unwrap();
    let graph_path = dir.path().join("reports/static/app/owner_graph.json");
    let cycles_path = dir.path().join("elsewhere/cycles.json");
    let modules_root = dir.path().join("modules");
    fs::create_dir_all(&modules_root).unwrap();
    write(&graph_path, &synthetic_graph_json());
    write(&cycles_path, &synthetic_cycles_json());

    let out = run_gate(&[
        "gate",
        "list",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--cycles",
        cycles_path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "gate list with --cycles exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["blocking_sccs"].as_array().unwrap().len(), 1);
}

#[test]
fn gate_list_runs_without_modules_flag() {
    // `--modules` is optional (gate reads nothing from it). Omitting
    // it — and clearing the env fallback — must still succeed.
    let fx = fixture_with_default_cycles_layout();
    let out = Command::new(debundle_binary())
        .args([
            "gate",
            "list",
            "--graph",
            fx.graph_path.to_str().unwrap(),
            "--format",
            "json",
        ])
        .env_remove("DEBUNDLE_MODULES")
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "gate list without --modules exit: stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(parsed["blocking_sccs"].as_array().unwrap().len(), 1);
}
