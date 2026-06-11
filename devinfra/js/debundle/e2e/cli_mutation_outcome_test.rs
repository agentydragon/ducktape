//! E2e for the machine-consumable outcome contract shared by the five
//! spec-mutating CLI verbs (`bindings {assign,unassign,rename}`,
//! `modules {merge,delete}`):
//!
//! - every verb supports `--format json` and prints one outcome
//!   object carrying the shared core (`verb` / `action` / `gate` /
//!   `files_written` / `files_deleted`) plus verb-specific fields;
//! - a realizability-gate rejection prints a structured rejection
//!   object on stdout (`action: "rejected"`, `rejection.kind`,
//!   canonical per-SCC / per-conflict projections) while the human
//!   blame report stays on stderr;
//! - edit-gate rejections write the same `cycles.json` /
//!   `atomic_unit_conflicts.json` artifacts the pipeline writes, as
//!   siblings of `--graph`, so `debundle gate list` works on the
//!   failure that was just reported.
//!
//! Shells out to the built `debundle` binary against synthetic
//! `owner_graph.json` fixtures (same shapes as
//! `modules_gate_cli_test` / `bindings_assign_gate_cli_test`).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

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

fn run_debundle(args: &[&str]) -> std::process::Output {
    Command::new(debundle_binary())
        .args(args)
        .output()
        .expect("spawn debundle")
}

fn parse_stdout_json(out: &std::process::Output) -> Value {
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err})\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr),
        )
    })
}

fn owner_node(id: &str, ordinal: usize, binding: &str, destination: &str) -> Value {
    serde_json::json!({
        "id": id,
        "statement_ordinal": ordinal,
        "declared_bindings": [ { "binding": binding, "export_name": binding } ],
        "statement_kind": "var_decl",
        "purity": { "kind": "pure" },
        "destination": destination
    })
}

fn eager_edge(id: &str, source: &str, target: &str, binding: &str, ordinal: usize) -> Value {
    serde_json::json!({
        "id": id,
        "source": source,
        "target": target,
        "edge_kind": "eager_use",
        "binding": binding,
        "statement_ordinal": ordinal,
        "constrains_init_order": true
    })
}

fn graph(nodes: Vec<Value>, edges: Vec<Value>) -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": nodes,
        "edges": edges,
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

/// alpha (module `a`) eager-reads beta (module `b`) — an acyclic
/// cross-module read, so single-binding moves between the modules
/// stay realizable. Positive-control graph.
fn acyclic_graph() -> String {
    graph(
        vec![
            owner_node("owner:0", 0, "alpha", "a"),
            owner_node("owner:1", 1, "beta", "b"),
        ],
        vec![eager_edge("owner_edge:0", "owner:0", "owner:1", "beta", 0)],
    )
}

/// alpha (owner:0) and beta (owner:1) form an atomic unit via mutual
/// `eager_rebind` edges; both pre-claimed by `home/atom`. Moving one
/// of them out splits the atom.
fn atomic_unit_graph() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            owner_node("owner:0", 0, "alpha", "home/atom"),
            owner_node("owner:1", 1, "beta", "home/atom"),
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

/// alpha (`a`) → gamma (`c`) → beta (`b`): a DAG quotient `a → c → b`
/// that closes into the 2-cycle `m ↔ c` when `a` and `b` merge.
fn merge_cycle_graph() -> String {
    graph(
        vec![
            owner_node("owner:0", 0, "alpha", "a"),
            owner_node("owner:1", 1, "beta", "b"),
            owner_node("owner:2", 2, "gamma", "c"),
        ],
        vec![
            eager_edge("owner_edge:0", "owner:0", "owner:2", "gamma", 0),
            eager_edge("owner_edge:1", "owner:2", "owner:1", "beta", 2),
        ],
    )
}

fn one_member_module(binding: &str) -> String {
    format!("members:\n  - selector: {{ binding: {{ name: {binding} }} }}\n")
}

fn write_acyclic_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let modules = root.join("modules");
    let graph_path = root.join("owner_graph.json");
    write(&graph_path, &acyclic_graph());
    write(&modules.join("a.yaml"), &one_member_module("alpha"));
    write(&modules.join("b.yaml"), &one_member_module("beta"));
    (modules, graph_path)
}

// ---------------------------------------------------------------
// `--format json` success outcomes: one shared schema core across
// the five mutating verbs.
// ---------------------------------------------------------------

#[test]
fn assign_json_outcome_carries_shared_core() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, graph_path) = write_acyclic_fixture(dir.path());

    let out = run_debundle(&[
        "bindings",
        "assign",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "beta:c",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "assign", "{parsed}");
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["gate"], "passed", "{parsed}");
    assert_eq!(parsed["moves_applied"], 1, "{parsed}");
    let written: Vec<&str> = parsed["files_written"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f.as_str().unwrap())
        .collect();
    assert!(written.iter().any(|f| f.ends_with("c.yaml")), "{parsed}");
    let deleted: Vec<&str> = parsed["files_deleted"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f.as_str().unwrap())
        .collect();
    assert!(
        deleted.iter().any(|f| f.ends_with("b.yaml")),
        "drained source module must be reported deleted: {parsed}"
    );
}

#[test]
fn unassign_json_outcome_carries_shared_core() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, graph_path) = write_acyclic_fixture(dir.path());

    let out = run_debundle(&[
        "bindings",
        "unassign",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "beta",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "unassign", "{parsed}");
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["gate"], "passed", "{parsed}");
    assert_eq!(parsed["unassigned"], 1, "{parsed}");
}

#[test]
fn rename_json_outcome_carries_shared_core() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(&modules.join("m.yaml"), &one_member_module("alpha"));

    let out = run_debundle(&[
        "bindings",
        "rename",
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
        "alpha",
        "ReadableAlpha",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "rename", "{parsed}");
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["gate"], "names_only", "{parsed}");
    assert_eq!(parsed["binding"], "alpha", "{parsed}");
    assert_eq!(parsed["new_readable"], "ReadableAlpha", "{parsed}");
    assert_eq!(parsed["files_written"].as_array().unwrap().len(), 1);
    assert_eq!(parsed["files_deleted"].as_array().unwrap().len(), 0);
}

#[test]
fn merge_json_outcome_carries_shared_core() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, graph_path) = write_acyclic_fixture(dir.path());

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "merge", "{parsed}");
    assert_eq!(parsed["action"], "applied", "{parsed}");
    assert_eq!(parsed["gate"], "passed", "{parsed}");
    let written = parsed["files_written"].as_array().unwrap();
    assert!(written[0].as_str().unwrap().ends_with("a.yaml"), "{parsed}");
    let deleted = parsed["files_deleted"].as_array().unwrap();
    assert_eq!(deleted.len(), 1, "{parsed}");
    assert!(deleted[0].as_str().unwrap().ends_with("b.yaml"), "{parsed}");
    assert!(
        parsed["target"].as_str().unwrap().ends_with("a.yaml"),
        "{parsed}"
    );
}

#[test]
fn merge_dry_run_json_outcome_reports_dry_run_action() {
    let dir = tempfile::tempdir().unwrap();
    let (modules, graph_path) = write_acyclic_fixture(dir.path());

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "--dry-run",
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "merge", "{parsed}");
    assert_eq!(parsed["action"], "dry-run", "{parsed}");
    assert!(modules.join("b.yaml").exists(), "dry-run must not delete");
}

#[test]
fn delete_json_outcome_carries_shared_core() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    write(&modules.join("ui/empty.yaml"), "members: []\n");

    let out = run_debundle(&[
        "modules",
        "delete",
        "--modules",
        modules.to_str().unwrap(),
        "--format",
        "json",
        "ui/empty.yaml",
    ]);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "delete", "{parsed}");
    assert_eq!(parsed["action"], "applied", "{parsed}");
    // Deleting a structurally-empty module cannot change the
    // partition, so no gate runs.
    assert_eq!(parsed["gate"], "not_required", "{parsed}");
    assert_eq!(parsed["files_written"].as_array().unwrap().len(), 0);
    let deleted = parsed["files_deleted"].as_array().unwrap();
    assert_eq!(deleted.len(), 1, "{parsed}");
    assert!(
        deleted[0].as_str().unwrap().ends_with("ui/empty.yaml"),
        "{parsed}"
    );
}

// ---------------------------------------------------------------
// Structured gate rejections on stdout + on-disk artifacts.
// ---------------------------------------------------------------

#[test]
fn assign_atom_split_rejection_emits_structured_json_and_artifact() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let graph_path = dir.path().join("owner_graph.json");
    write(&graph_path, &atomic_unit_graph());
    write(
        &modules.join("home/atom.yaml"),
        &format!(
            "{}{}",
            one_member_module("alpha"),
            "  - selector: { binding: { name: beta } }\n"
        ),
    );

    let out = run_debundle(&[
        "bindings",
        "assign",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "alpha:dogfood/split",
    ]);
    assert!(!out.status.success(), "atom split must be rejected");
    // Human blame report stays on stderr.
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("splits one or more atomic units"),
        "stderr: {stderr}"
    );
    // Structured rejection on stdout.
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "assign", "{parsed}");
    assert_eq!(parsed["action"], "rejected", "{parsed}");
    assert_eq!(parsed["rejection"]["kind"], "atom_split", "{parsed}");
    let conflicts = parsed["rejection"]["conflicts"].as_array().unwrap();
    assert_eq!(conflicts.len(), 1, "{parsed}");
    let claims = conflicts[0]["claims"].as_array().unwrap();
    let claimed_modules: Vec<&str> = claims
        .iter()
        .map(|c| c["module"].as_str().unwrap())
        .collect();
    assert!(
        claimed_modules.contains(&"dogfood/split") && claimed_modules.contains(&"home/atom"),
        "claims must name both destinations: {parsed}"
    );
    // The same projection lands on disk next to --graph.
    let artifact: Value = serde_json::from_str(
        &fs::read_to_string(dir.path().join("atomic_unit_conflicts.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(artifact.as_array().unwrap().len(), 1, "{artifact}");
}

#[test]
fn merge_cycle_rejection_emits_structured_json_and_gate_list_works() {
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let graph_path = dir.path().join("owner_graph.json");
    write(&graph_path, &merge_cycle_graph());
    write(&modules.join("a.yaml"), &one_member_module("alpha"));
    write(&modules.join("b.yaml"), &one_member_module("beta"));
    write(&modules.join("c.yaml"), &one_member_module("gamma"));

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(!out.status.success(), "merge-induced cycle must reject");
    let parsed = parse_stdout_json(&out);
    assert_eq!(parsed["verb"], "merge", "{parsed}");
    assert_eq!(parsed["action"], "rejected", "{parsed}");
    assert_eq!(
        parsed["rejection"]["kind"], "unrealizable_cycles",
        "{parsed}"
    );
    let sccs = parsed["rejection"]["blocking_sccs"].as_array().unwrap();
    assert_eq!(sccs.len(), 1, "{parsed}");
    let scc_modules: Vec<&str> = sccs[0]["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert!(
        scc_modules.contains(&"a") && scc_modules.contains(&"c"),
        "SCC must name the post-merge cycle members: {parsed}"
    );
    assert!(
        !sccs[0]["cut"].as_array().unwrap().is_empty(),
        "cut edges carry the binding-pair blame: {parsed}"
    );

    // The documented follow-up works on the rejection that was just
    // reported: cycles.json landed next to --graph, where `gate list`
    // looks by default.
    let gate_out = run_debundle(&[
        "gate",
        "list",
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        gate_out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&gate_out.stderr)
    );
    let gate_parsed = parse_stdout_json(&gate_out);
    let entries = gate_parsed["blocking_sccs"].as_array().unwrap();
    assert_eq!(entries.len(), 1, "{gate_parsed}");
    assert_eq!(entries[0]["module_count"].as_u64(), Some(2));
}

#[test]
fn passing_edit_gate_clears_stale_rejection_artifacts() {
    // A rejected merge leaves cycles.json; a subsequent edit that
    // passes the gate must clear it so `gate list` reports the clean
    // state again.
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let graph_path = dir.path().join("owner_graph.json");
    write(&graph_path, &merge_cycle_graph());
    write(&modules.join("a.yaml"), &one_member_module("alpha"));
    write(&modules.join("b.yaml"), &one_member_module("beta"));
    write(&modules.join("c.yaml"), &one_member_module("gamma"));

    let rejected = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(!rejected.status.success());
    assert!(dir.path().join("cycles.json").exists());

    // Merging a + c is realizable (`{a,c} → b` stays a DAG).
    let accepted = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--target",
        "a.yaml",
        "c.yaml",
    ]);
    assert!(
        accepted.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&accepted.stderr)
    );
    assert!(
        !dir.path().join("cycles.json").exists(),
        "stale cycles.json must be cleared by a passing gate"
    );
}

#[test]
fn rejection_without_json_format_keeps_stdout_text_free_of_json() {
    // With `--format text`, the rejection path must not print the
    // structured object — scripting callers opt in via JSON formats.
    let dir = tempfile::tempdir().unwrap();
    let modules = dir.path().join("modules");
    let graph_path = dir.path().join("owner_graph.json");
    write(&graph_path, &merge_cycle_graph());
    write(&modules.join("a.yaml"), &one_member_module("alpha"));
    write(&modules.join("b.yaml"), &one_member_module("beta"));
    write(&modules.join("c.yaml"), &one_member_module("gamma"));

    let out = run_debundle(&[
        "modules",
        "merge",
        "--modules",
        modules.to_str().unwrap(),
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "text",
        "--target",
        "a.yaml",
        "b.yaml",
    ]);
    assert!(!out.status.success());
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        !stdout.contains("\"rejection\""),
        "text format must not emit the JSON rejection object: {stdout}"
    );
}
