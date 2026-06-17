//! End-to-end coverage of `debundle bindings unassign` — including
//! the realizability + atom-split gate hookup and the dry-run /
//! non-dry-run exit-code consistency contract.
//!
//! Mirrors `bindings_assign_gate_cli_test.rs`. Each fixture uses the
//! same synthetic-`owner_graph.json` pattern: the gate joins YAML
//! members → owners by binding name, so the spec YAMLs we write under
//! `--modules` carry the same module-vs-binding assignments and the
//! gate's reconstruction is unambiguous.

use debundle_e2e_support::{debundler_path, write_atomic_unit_fixture, write_text_file};
use std::fs;
use std::process::Command;

/// Synthetic owner graph where alpha (owner:0) and beta (owner:1)
/// form an atomic unit via mutual `eager_rebind` edges (matching the
/// `bindings_assign_gate_cli_test.rs` fixture). The atom MUST
/// co-locate: any partition that places one in a different module
/// (including "this one's home is residual, the other one isn't")
/// splits the unit.
#[test]
fn bindings_unassign_rejects_atom_split_when_other_members_stay() {
    // Atomic unit {alpha, beta} co-located in `home/atom.yaml`.
    // Unassigning just `alpha` would send alpha to residual while
    // beta stays at `home/atom`; the atom is split. Gate must
    // reject before any YAML is written.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "alpha",
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
}

#[test]
fn bindings_unassign_rejects_split_under_dry_run_too() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);
    let pre_atom = fs::read_to_string(modules.join("home/atom.yaml")).unwrap();

    let out = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "--dry-run",
            "alpha",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        !out.status.success(),
        "dry-run on an atom-splitting plan must still exit non-zero; stdout: {}; stderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr),
    );
    // The dry-run path must still surface the *gate's* diagnostic,
    // not a clap "unknown subcommand" error — so this assertion
    // pins the rejection to the realizability gate.
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("splits one or more atomic units") || stderr.contains("atom-split"),
        "expected atom-split diagnostic under dry-run, got stderr:\n{stderr}",
    );
    assert_eq!(
        fs::read_to_string(modules.join("home/atom.yaml")).unwrap(),
        pre_atom,
    );
}

#[test]
fn bindings_unassign_dry_run_and_apply_share_exit_code() {
    // Atom-split fixture: both modes must exit non-zero with the
    // same code so callers can dry-run before applying.
    let dir_dry = tempfile::tempdir().unwrap();
    let (modules_dry, graph_dry) = write_atomic_unit_fixture(dir_dry.path());
    let dry = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules_dry.to_str().unwrap(),
            "--graph",
            graph_dry.to_str().unwrap(),
            "--dry-run",
            "alpha",
        ])
        .output()
        .expect("spawn debundle");

    let dir_apply = tempfile::tempdir().unwrap();
    let (modules_apply, graph_apply) = write_atomic_unit_fixture(dir_apply.path());
    let apply = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules_apply.to_str().unwrap(),
            "--graph",
            graph_apply.to_str().unwrap(),
            "alpha",
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
    // Both modes must surface the gate's atom-split diagnostic, not
    // a clap "unknown subcommand" error — pins the assertion to the
    // realizability gate's verdict rather than any old non-zero exit.
    let dry_err = String::from_utf8_lossy(&dry.stderr);
    let apply_err = String::from_utf8_lossy(&apply.stderr);
    assert!(
        dry_err.contains("splits one or more atomic units") || dry_err.contains("atom-split"),
        "dry-run stderr missing atom-split diagnostic:\n{dry_err}",
    );
    assert!(
        apply_err.contains("splits one or more atomic units") || apply_err.contains("atom-split"),
        "apply stderr missing atom-split diagnostic:\n{apply_err}",
    );
}

#[test]
fn bindings_unassign_accepts_when_whole_atom_unassigned_together() {
    // Unassigning {alpha, beta} together moves the entire atom to
    // residual atomically — no split, gate must accept.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let (modules, graph) = write_atomic_unit_fixture(root);

    let out = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules.to_str().unwrap(),
            "--graph",
            graph.to_str().unwrap(),
            "alpha",
            "beta",
        ])
        .output()
        .expect("spawn debundle");
    assert!(
        out.status.success(),
        "expected zero exit; stderr: {}",
        String::from_utf8_lossy(&out.stderr),
    );
    // The source module became empty and carried no module-level
    // comment, so it must have been auto-deleted (drain rule).
    assert!(
        !modules.join("home/atom.yaml").exists(),
        "drained source must be deleted",
    );
}

#[test]
fn bindings_unassign_requires_graph_or_no_verify() {
    // Same "graph or no-verify" policy `bindings assign` enforces:
    // invoking the verb without a way to validate is refused up
    // front, before any YAML write.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let modules = root.join("modules");
    write_text_file(
        &modules.join("a.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n",
    );
    let pre_a = fs::read_to_string(modules.join("a.yaml")).unwrap();

    let out = Command::new(debundler_path())
        .args([
            "bindings",
            "unassign",
            "--modules",
            modules.to_str().unwrap(),
            "alpha",
        ])
        .output()
        .expect("spawn debundle");
    assert!(!out.status.success());
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("--graph") || stderr.contains("--no-verify"),
        "expected refusal mentioning --graph / --no-verify; got: {stderr}",
    );
    // Spec must NOT have been touched.
    assert_eq!(
        fs::read_to_string(modules.join("a.yaml")).unwrap(),
        pre_a,
        "a.yaml must be unchanged after refusal",
    );
}
