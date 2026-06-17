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

use debundle_e2e_support::{
    debundler_path, graph_with_acyclic_cross_module_read, write_atomic_unit_fixture,
    write_text_file,
};
use std::fs;
use std::process::Command;

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
/// Synthetic owner graph with one acyclic cross-module read so a
/// `bindings assign` between two existing modules is realizable.
/// Used as the positive control.
#[test]
fn bindings_assign_rejects_split_of_known_atomic_unit() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = Command::new(debundler_path())
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

    let out = Command::new(debundler_path())
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
    let dry = Command::new(debundler_path())
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
    let apply = Command::new(debundler_path())
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
    write_text_file(&graph, &graph_with_acyclic_cross_module_read());
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    write_text_file(
        &modules.join("b.yaml"),
        "members:\n  - selector: { binding: { name: beta } }\n",
    );

    // Move beta into module `c`. The post-edit partition is
    // `a → c` (DAG) — no cycle, no atomic-unit conflict.
    let out = Command::new(debundler_path())
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
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );

    let out = Command::new(debundler_path())
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
