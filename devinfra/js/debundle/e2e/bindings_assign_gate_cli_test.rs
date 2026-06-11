//! End-to-end coverage of the realizability gate hookup in
//! `debundle bindings assign` — atom-split detection and the
//! dry-run / non-dry-run exit-code consistency contract (see
//! `CLI_DOGFOOD.md`).
//!
//! Shells out to the built `debundle` binary against synthetic
//! `owner_graph.json` fixtures so the gate's path through CLI args +
//! validation + diagnostic rendering is exercised end-to-end.
//!
//! Each fixture pre-declares the destination claims in
//! `OwnerGraphNodeReport.destination` so reconstructing the
//! pre-edit partition is unambiguous; the spec YAMLs we write under
//! `--modules` carry the same module-vs-binding assignments. The
//! gate's reconstruction joins YAML members → owners by binding
//! name, matching `gate_post_edit_partition`'s wire contract.

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

/// Synthetic owner graph where alpha (owner:0) and beta (owner:1)
/// form an atomic unit via a mutual `eager_rebind` edge. Per
/// `atomic_units.rs`'s closure rules, `EagerRebind` adds edges in
/// both directions to G_atomic, so the resulting SCC is `{alpha,
/// beta}` — they MUST co-locate in any realizable spec.
///
/// Pre-edit: both members live in `home/atom.yaml` → one module,
/// atom respected, realizable. A `bindings assign` that moves only
/// `alpha` to a different module would split the atom; the gate
/// must reject before any YAML is written.
fn graph_with_atomic_unit() -> String {
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
                "destination": "home/atom"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "home/atom"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_rebind",
                "binding": "beta",
                "statement_ordinal": 0,
                "constrains_init_order": true
            },
            {
                "id": "owner_edge:1",
                "source": "owner:1",
                "target": "owner:0",
                "edge_kind": "eager_rebind",
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

/// Synthetic owner graph with one acyclic cross-module read so a
/// `bindings assign` between two existing modules is realizable.
/// Used as the positive control.
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

fn write_atomic_unit_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_atomic_unit());
    // Pre-edit: alpha + beta co-located in one module — atom
    // respected, realizable.
    write(
        &modules.join("home/atom.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n  - selector: { binding: { name: beta } }\n",
    );
    (modules, graph)
}

#[test]
fn bindings_assign_rejects_split_of_known_atomic_unit() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "alpha:dogfood/split",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        !out.status.success(),
        "expected non-zero exit; stdout: {}; stderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("splits one or more atomic units") || stderr.contains("atom-split"),
        "expected atom-split diagnostic, got stderr:\n{stderr}",
    );
    // Spec must NOT have been touched.
    assert_eq!(
        fs::read_to_string(modules.join("home/atom.yaml")).unwrap(),
        pre_atom,
        "atom.yaml must be unchanged after rejection",
    );
    assert!(
        !modules.join("dogfood/split.yaml").exists(),
        "destination must not have been created",
    );
}

#[test]
fn bindings_assign_rejects_split_under_dry_run_too() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--dry-run",
            "alpha:dogfood/split",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        !out.status.success(),
        "dry-run on an atom-splitting plan must still exit non-zero; stdout: {}; stderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    assert_eq!(
        fs::read_to_string(modules.join("home/atom.yaml")).unwrap(),
        pre_atom,
    );
}

#[test]
fn bindings_assign_dry_run_and_apply_share_exit_code() {
    // CLI_DOGFOOD.md contract: `--dry-run` and non-dry-run on the same
    // input must return the same exit code. The atom-split fixture is a
    // clean way to assert this — both should bail with exit 1.
    let dir_dry = tempfile::tempdir().unwrap();
    let (modules_dry, graph_dry) = write_atomic_unit_fixture(dir_dry.path());
    let dry = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules_dry.to_str().unwrap(),
            "--graph",
            graph_dry.to_str().unwrap(),
            "--dry-run",
            "alpha:dogfood/split",
        ])
        .output()
        .expect("spawn debundle");

    let dir_apply = tempfile::tempdir().unwrap();
    let (modules_apply, graph_apply) = write_atomic_unit_fixture(dir_apply.path());
    let apply = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules_apply.to_str().unwrap(),
            "--graph",
            graph_apply.to_str().unwrap(),
            "alpha:dogfood/split",
        ])
        .output()
        .expect("spawn debundle");

    assert_eq!(
        dry.status.code(),
        apply.status.code(),
        "dry-run and apply must return the same exit code on the same input.\n\
         dry stderr: {}\napply stderr: {}",
        String::from_utf8_lossy(&dry.stderr),
        String::from_utf8_lossy(&apply.stderr),
    );
    assert!(!dry.status.success() && !apply.status.success());
}

#[test]
fn bindings_assign_accepts_acyclic_cross_module_move() {
    // Positive control: an assign that doesn't split any atomic
    // unit nor introduce a cycle is accepted and writes the YAML.
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

    // Move beta into module `c`. The post-edit partition is
    // `a → c` (DAG) — no cycle, no atomic-unit conflict.
    let out = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "beta:c",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(modules.join("c.yaml").exists(), "c.yaml must be written");
    assert!(
        !modules.join("b.yaml").exists(),
        "drained b.yaml must be deleted"
    );
}

#[test]
fn bindings_assign_requires_graph_or_no_verify() {
    // Mirrors the policy `modules merge` / `modules delete --force`
    // already enforce: invoking the verb without a way to validate
    // is refused up front.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    write(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );

    let out = Command::new(debundle_binary())
        .args([
            "bindings",
            "assign",
            "--modules",
            modules.to_str().unwrap(),
            "alpha:b",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("--graph") || stderr.contains("--no-verify"),
        "expected refusal mentioning --graph / --no-verify; got: {stderr}",
    );
}
