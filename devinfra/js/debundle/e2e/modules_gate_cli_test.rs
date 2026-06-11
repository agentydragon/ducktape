//! End-to-end coverage of the realizability gate hookup in
//! `debundle modules merge` and `debundle modules delete --force`
//! (task #84). Shells out to the built `debundle` binary against
//! synthetic owner_graph.json fixtures.
//!
//! Fixtures are hand-rolled JSON so the cycle topology is precise:
//! the constraining edges in the owner graph, combined with the
//! post-edit partition the gate builds from the modified spec
//! YAMLs, force `validate_factorization` to surface an
//! unrealizable SCC (or none). The reject path's diagnostic is
//! the same `render_cycle_summary` text the pipeline prints when
//! the materializer's gate rejects.

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

/// Synthetic owner graph with three owners (alpha, beta, gamma)
/// and a DAG of constraining eager-use edges that, when alpha and
/// beta land in the same merged module, closes into a 2-cycle with
/// gamma's module.
///
/// Edges (all `eager_use`, `constrains_init_order: true`):
///   alpha (owner:0) → gamma (owner:2)
///   gamma (owner:2) → beta  (owner:1)
///
/// Pre-merge with alpha in module_a, beta in module_b, gamma in
/// module_c: the quotient is `a → c → b` — a DAG, realizable.
///
/// Post-merge (a+b → m): the quotient becomes `m → c`, `c → m` —
/// a 2-cycle of constraining edges, unrealizable.
fn graph_with_merge_cycle_potential() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "declared_bindings": [
                    { "binding": "alpha", "export_name": "alpha" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "a"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "b"
            },
            {
                "id": "owner:2",
                "statement_ordinal": 2,
                "declared_bindings": [
                    { "binding": "gamma", "export_name": "gamma" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "c"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:2",
                "edge_kind": "eager_use",
                "binding": "gamma",
                "statement_ordinal": 0,
                "constrains_init_order": true
            },
            {
                "id": "owner_edge:1",
                "source": "owner:2",
                "target": "owner:1",
                "edge_kind": "eager_use",
                "binding": "beta",
                "statement_ordinal": 2,
                "constrains_init_order": true
            }
        ],
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

/// Synthetic owner graph where alpha (owner:0) and beta (owner:1)
/// mutually eager-read each other. Pre-edit with alpha in module_a
/// and beta in module_b: a ↔ b cycle (unrealizable). Whatever the
/// caller does next — co-locating them into one module fixes it
/// (clean merge); deleting either while keeping the other still
/// leaves the surviving binding's owner pointing at residual,
/// which still cycles with the other module.
fn graph_with_mutual_cross_module_reads() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "declared_bindings": [
                    { "binding": "alpha", "export_name": "alpha" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "a"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "b"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_use",
                "binding": "beta",
                "statement_ordinal": 0,
                "constrains_init_order": true
            },
            {
                "id": "owner_edge:1",
                "source": "owner:1",
                "target": "owner:0",
                "edge_kind": "eager_use",
                "binding": "alpha",
                "statement_ordinal": 1,
                "constrains_init_order": true
            }
        ],
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

/// Synthetic owner graph with one cross-module read (alpha → beta).
/// alpha lives in module_a, beta in module_b. Pre-edit quotient is
/// the DAG `a → b`, realizable. Merging a + b drops the cross-module
/// edge entirely (same-module). Deleting either one leaves a clean
/// residual fallback with no cycle.
fn graph_with_acyclic_cross_module_read() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "declared_bindings": [
                    { "binding": "alpha", "export_name": "alpha" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "a"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "b"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_use",
                "binding": "beta",
                "statement_ordinal": 0,
                "constrains_init_order": true
            }
        ],
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

#[test]
fn modules_merge_rejects_when_merge_creates_cycle() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_merge_cycle_potential());
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );
    write(
        &modules.join("c.yaml"),
        "members:\n  - selector: { binding: { name: gamma } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "merge",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--target",
            "a.yaml",
            "b.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("unrealizable"),
        "expected unrealizability diagnostic, got stderr:\n{stderr}",
    );
    // The YAML must NOT have been written.
    assert!(modules.join("a.yaml").exists(), "a.yaml must survive");
    assert!(modules.join("b.yaml").exists(), "b.yaml must survive");
}

#[test]
fn modules_merge_accepts_clean_merge() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_acyclic_cross_module_read());
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "merge",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--target",
            "a.yaml",
            "b.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    // After-merge: a.yaml exists, b.yaml has been removed.
    assert!(modules.join("a.yaml").exists());
    assert!(!modules.join("b.yaml").exists());
}

#[test]
fn modules_merge_gate_accepts_missing_target() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_acyclic_cross_module_read());
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "merge",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--target",
            "merged/new_target",
            "a.yaml",
            "b.yaml",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(modules.join("merged/new_target.yaml").exists());
    assert!(!modules.join("a.yaml").exists());
    assert!(!modules.join("b.yaml").exists());
}

#[test]
fn modules_delete_force_rejects_when_post_state_unrealizable() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    // Pre-delete state already mutually-references — deleting either
    // module leaves the surviving one in a cycle with residual
    // (the orphaned binding's effective destination).
    write(&graph, &graph_with_mutual_cross_module_reads());
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "b.yaml",
            "--force",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success(), "expected non-zero exit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    // The mutual-eager-reads fixture forms one atomic unit; deleting
    // either module strands one member at residual while the other
    // stays on its module, which the atom-split check rejects before
    // the cycle check would. Either diagnostic is acceptable
    // evidence the gate fired.
    assert!(
        stderr.contains("unrealizable") || stderr.contains("splits one or more atomic units"),
        "expected unrealizability or atom-split diagnostic, got stderr:\n{stderr}",
    );
    assert!(modules.join("b.yaml").exists(), "b.yaml must survive");
}

#[test]
fn modules_delete_force_accepts_clean_deletion() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_acyclic_cross_module_read());
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    // Delete `b.yaml`: beta becomes unclaimed → residual. The
    // post-delete quotient is `a → residual`, still a DAG, so the
    // gate accepts.
    let out = Command::new(debundle_binary())
        .args([
            "modules",
            "delete",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "b.yaml",
            "--force",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(!modules.join("b.yaml").exists());
    assert!(modules.join("a.yaml").exists());
}
