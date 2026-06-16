//! End-to-end coverage for the edit gate's view of source_match
//! member selectors and binding_groups.
//!
//! The CLI edit gate (`gate_post_edit_partition`) must see the SAME
//! claims `debundle run` materializes. A module whose member is
//! selected via `selector.source_match` (or via a `binding_groups`
//! entry) still owns that binding's owner; treating it as residual
//! lets the gate green-light edits the run pipeline's authoritative
//! gate rejects (atom-split between the source_match-claimed owner
//! and a moved/unassigned sibling).
//!
//! Shells out to the built `debundle` binary against synthetic
//! `owner_graph.json` + source-file fixtures, mirroring
//! `bindings_unassign_gate_cli_test.rs`.

use debundle_e2e_support::{debundler_path as debundle_binary, write_text_file as write};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Synthetic owner graph where alpha (owner:0) and beta (owner:1)
/// form an atomic unit via mutual `eager_rebind` edges, plus an
/// independent gamma (owner:2). Every node carries a
/// `source_location` into `static/chunk.js` so source-backed
/// selector resolution can run.
fn graph_with_atomic_unit_and_sources() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "source_location": {
                    "source_path": "static/chunk.js",
                    "start_line": 1,
                    "end_line": 1
                },
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
                "source_location": {
                    "source_path": "static/chunk.js",
                    "start_line": 2,
                    "end_line": 2
                },
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "home/atom"
            },
            {
                "id": "owner:2",
                "statement_ordinal": 2,
                "source_location": {
                    "source_path": "static/chunk.js",
                    "start_line": 3,
                    "end_line": 3
                },
                "declared_bindings": [
                    { "binding": "gamma", "export_name": "gamma" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "solo/gamma"
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

const CHUNK_SOURCE: &str = "const alpha = 1;\nconst beta = 2;\nconst gamma = 3;\n";

/// Module spec where `alpha` is claimed via a `source_match` member
/// selector (not a binding selector) and `beta` via a plain binding
/// selector — both co-located, so the pre-edit spec is realizable.
const ATOM_YAML_SOURCE_MATCH: &str = r#"members:
  - name: Alpha
    selector:
      source_match:
        match: 'const alpha = 1;'
  - selector: { binding: { name: beta } }
"#;

/// Same claim set, but `alpha` arrives via a `binding_groups` entry.
const ATOM_YAML_BINDING_GROUP: &str = r#"binding_groups:
  - source_match:
      match: 'const alpha = 1;'
    exports: { alpha: Alpha }
members:
  - selector: { binding: { name: beta } }
"#;

const GAMMA_YAML: &str = "members:\n  - selector: { binding: { name: gamma } }\n";

fn write_fixture(root: &Path, atom_yaml: &str) -> (PathBuf, PathBuf) {
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write(&graph, &graph_with_atomic_unit_and_sources());
    write(&root.join("static/chunk.js"), CHUNK_SOURCE);
    write(&modules.join("home/atom.yaml"), atom_yaml);
    write(&modules.join("solo/gamma.yaml"), GAMMA_YAML);
    (modules, graph)
}

fn run_unassign(root: &Path, modules: &Path, graph: &Path, sym: &str) -> std::process::Output {
    Command::new(debundle_binary())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--source-root",
            root.to_str().unwrap(),
            sym,
        ])
        .output()
        .expect("spawn debundle")
}

#[test]
fn unassign_rejects_atom_split_when_sibling_is_claimed_via_source_match() {
    // Truth: alpha (source_match member) and beta (binding member)
    // co-locate in home/atom — realizable. Unassigning beta sends it
    // to residual while alpha stays claimed → atom split. A gate
    // that only reads `selector.binding` treats alpha as residual
    // and wrongly passes.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_fixture(root, ATOM_YAML_SOURCE_MATCH);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = run_unassign(root, &modules, &graph, "beta");

    assert!(
        !out.status.success(),
        "unassigning beta must be rejected (atom split with source_match-claimed alpha); \
         stdout: {}; stderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("splits one or more atomic units") || stderr.contains("atom-split"),
        "expected atom-split diagnostic, got stderr:\n{stderr}",
    );
    assert_eq!(
        fs::read_to_string(modules.join("home/atom.yaml")).unwrap(),
        pre_atom,
        "atom.yaml must be unchanged after rejection",
    );
}

#[test]
fn unassign_rejects_atom_split_when_sibling_is_claimed_via_binding_group() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_fixture(root, ATOM_YAML_BINDING_GROUP);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = run_unassign(root, &modules, &graph, "beta");

    assert!(
        !out.status.success(),
        "unassigning beta must be rejected (atom split with binding_group-claimed alpha); \
         stdout: {}; stderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    assert_eq!(
        fs::read_to_string(modules.join("home/atom.yaml")).unwrap(),
        pre_atom,
    );
}

#[test]
fn unassign_of_independent_binding_passes_with_source_match_claims_present() {
    // Positive control: the spec contains a source_match member (so
    // the gate must resolve it against the chunk source), but the
    // edit itself — unassigning the independent gamma — splits
    // nothing. The gate must accept.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_fixture(root, ATOM_YAML_SOURCE_MATCH);

    let out = run_unassign(root, &modules, &graph, "gamma");

    assert!(
        out.status.success(),
        "unassigning the independent gamma must pass; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    assert!(
        !modules.join("solo/gamma.yaml").exists(),
        "drained gamma.yaml must be deleted",
    );
}

#[test]
fn gate_hard_errors_when_source_match_spec_has_unresolvable_sources() {
    // Soundness: when the spec carries source_match claims the gate
    // cannot resolve (chunk source missing), the gate must hard-error
    // rather than silently treating those owners as residual.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_fixture(root, ATOM_YAML_SOURCE_MATCH);
    fs::remove_file(root.join("static/chunk.js")).unwrap();

    let out = run_unassign(root, &modules, &graph, "gamma");

    assert!(
        !out.status.success(),
        "gate must refuse to run with unresolvable source_match claims; stdout: {}",
        String::from_utf8_lossy(&out.stdout),
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("source"),
        "expected a source-resolution error, got stderr:\n{stderr}",
    );
    assert!(
        modules.join("solo/gamma.yaml").exists(),
        "no file may be touched when the gate errors",
    );
}
