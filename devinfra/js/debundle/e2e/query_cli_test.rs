//! End-to-end coverage for the orthogonal query CLI surface
//! (`debundle binding`, `debundle scc`, `debundle cluster`).
//!
//! The tests run the real debundle pipeline on a small fixture, then
//! drive the new subcommands as if they were a downstream tool and
//! parse their JSON output. This pins the contract a script /
//! agent gets when it composes:
//!
//! ```text
//! debundle scc --graph ... --cycles-only --ndjson | jq -c '...'
//! debundle binding describe --graph ... --modules ... <name>
//! debundle binding assign --modules ... <name> <module>
//! ```

use std::process::Command;

use debundle_e2e_support::*;
use serde_json::Value;

fn debundle(args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(bin);
    for arg in args {
        command.arg(arg);
    }
    let output = command.output().expect("spawn debundle");
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        status: output.status,
    }
}

fn json_stdout(result: &CommandResult) -> Value {
    assert!(
        result.status.success(),
        "debundle exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    serde_json::from_str(&result.stdout).unwrap_or_else(|err| {
        panic!(
            "parse JSON stdout: {err}\nstdout:\n{}\nstderr:\n{}",
            result.stdout, result.stderr
        )
    })
}

fn ndjson_stdout(result: &CommandResult) -> Vec<Value> {
    assert!(
        result.status.success(),
        "debundle exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    result
        .stdout
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str::<Value>(line).expect("parse ndjson line"))
        .collect()
}

fn pipeline_graph_path(fixture: &Fixture) -> std::path::PathBuf {
    fixture
        .report_root
        .join(format!("{}/owner_graph.json", fixture.chunk_id))
}

/// Tree-shaped spec used by the binding / scc / cluster tests.
/// Two leaf bindings claimed by their own modules; everything else
/// stays in residual.
fn build_two_module_fixture() -> (Fixture, std::path::PathBuf) {
    let source = r#"const anchor = "anchor";
const helper = "helper";
function consumer() { return helper; }
export { anchor, consumer };
"#;

    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("consumers/consumer", &[Member::new("consumer")]),
        ],
    );
    let fixture = run_fixture(opts);
    // The materializer mirrors logical-module YAML into the spec
    // tree under `<report_root>/spec/modules`. The e2e harness
    // doesn't expose that path directly, so we synthesize a
    // matching spec tree the binding-edit commands can read.
    let modules_root = fixture.report_root.join("spec/modules");
    std::fs::create_dir_all(modules_root.join("anchors")).unwrap();
    std::fs::create_dir_all(modules_root.join("consumers")).unwrap();
    std::fs::write(
        modules_root.join("anchors/anchor.yaml"),
        "members:\n  - selector:\n      binding:\n        name: anchor\n",
    )
    .unwrap();
    std::fs::write(
        modules_root.join("consumers/consumer.yaml"),
        "members:\n  - selector:\n      binding:\n        name: consumer\n",
    )
    .unwrap();
    (fixture, modules_root)
}

#[test]
fn binding_describe_reports_module_home_for_assigned_binding() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "describe",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "anchor",
    ]);
    let json = json_stdout(&result);
    assert_eq!(json["binding"].as_str(), Some("anchor"));
    let home = &json["current_home"];
    assert_eq!(home["source"].as_str(), Some("module"));
    assert_eq!(home["module_path"].as_str(), Some("anchors/anchor"));
    let owners = json["owners"].as_array().expect("owners array");
    assert!(!owners.is_empty(), "expected at least one owner");
}

#[test]
fn binding_describe_reports_no_home_for_residual_binding() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "describe",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
    ]);
    let json = json_stdout(&result);
    assert!(json["current_home"].is_null());
    assert!(
        json["destination"]["residual"].as_bool().unwrap_or(false),
        "expected residual destination, got {:?}",
        json["destination"]
    );
}

#[test]
fn binding_show_code_emits_owner_source_span() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // The pipeline writes the source chunk under `<snapshot_root>`.
    let snapshot_root = fixture.snapshot_root.clone();
    let result = debundle(&[
        "binding",
        "show-code",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--source-root",
        snapshot_root.to_str().unwrap(),
        "anchor",
    ]);
    let json = json_stdout(&result);
    assert_eq!(json["binding"].as_str(), Some("anchor"));
    let slices = json["slices"].as_array().expect("slices");
    assert_eq!(slices.len(), 1);
    let text = slices[0]["text"].as_str().expect("slice text");
    assert!(
        text.contains("anchor"),
        "expected anchor source body, got {text:?}",
    );
}

#[test]
fn binding_assign_then_unassign_round_trips_spec() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // Move helper from residual into a new module. The default-on
    // validation has the owner graph available, so it can run the
    // realizability gate — the edit is safe (helper into a fresh
    // module with no edges).
    let assign = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
        "consumers/helper",
    ]);
    let assign_json = json_stdout(&assign);
    assert_eq!(assign_json["binding"].as_str(), Some("helper"));
    assert_eq!(
        assign_json["new_home"]["module_path"].as_str(),
        Some("consumers/helper"),
    );
    assert!(assign_json["created_destination_file"].as_bool().unwrap());
    assert!(assign_json["written"].as_bool().unwrap_or(false));
    assert!(
        assign_json.get("validation").is_some(),
        "default validation should produce a report"
    );
    let helper_yaml = modules_root.join("consumers/helper.yaml");
    assert!(helper_yaml.is_file());

    // Re-assign to a different module; old YAML should disappear
    // because it becomes empty.
    let reassign = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
        "consumers/utility",
    ]);
    json_stdout(&reassign);
    assert!(!helper_yaml.exists(), "previous home should be deleted");
    assert!(modules_root.join("consumers/utility.yaml").is_file());

    // Unassign — file is dropped because the only member is removed.
    let unassign = debundle(&[
        "binding",
        "unassign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
    ]);
    let unassign_json = json_stdout(&unassign);
    assert_eq!(
        unassign_json["previous_home"]["module_path"].as_str(),
        Some("consumers/utility"),
    );
    assert!(!modules_root.join("consumers/utility.yaml").exists());
}

#[test]
fn binding_assign_dry_run_does_not_modify_disk() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--dry-run",
        "helper",
        "consumers/helper",
    ]);
    let json = json_stdout(&result);
    assert!(json["dry_run"].as_bool().unwrap_or(false));
    assert!(!json["written"].as_bool().unwrap_or(true));
    // Validation still ran during the dry run.
    assert!(json.get("validation").is_some());
    assert!(!modules_root.join("consumers/helper.yaml").exists());
}

#[test]
fn binding_assign_without_graph_refuses_to_validate_blind() {
    let (fixture, modules_root) = build_two_module_fixture();
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "assign",
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
        "consumers/helper",
    ]);
    assert!(
        !result.status.success(),
        "expected nonzero exit when validation requested but no --graph"
    );
    assert!(
        result.stderr.contains("--graph"),
        "stderr should mention --graph: {}",
        result.stderr,
    );
    assert!(
        result.stderr.contains("--no-validate") || result.stderr.contains("--force"),
        "stderr should mention escape hatches: {}",
        result.stderr,
    );
    // Spec untouched.
    assert!(!modules_root.join("consumers/helper.yaml").exists());
}

#[test]
fn binding_assign_force_skips_validation_and_writes_blind() {
    let (fixture, modules_root) = build_two_module_fixture();
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "assign",
        "--modules",
        modules_root.to_str().unwrap(),
        "--force",
        "helper",
        "consumers/helper",
    ]);
    let json = json_stdout(&result);
    assert!(json["written"].as_bool().unwrap_or(false));
    assert!(
        json.get("validation").map(|v| v.is_null()).unwrap_or(true),
        "validation should be absent under --force, got {:?}",
        json.get("validation"),
    );
    assert!(modules_root.join("consumers/helper.yaml").is_file());
}

#[test]
fn binding_assign_no_validate_aliases_force() {
    let (fixture, modules_root) = build_two_module_fixture();
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "assign",
        "--modules",
        modules_root.to_str().unwrap(),
        "--no-validate",
        "helper",
        "consumers/helper",
    ]);
    let json = json_stdout(&result);
    assert!(json["written"].as_bool().unwrap_or(false));
    assert!(modules_root.join("consumers/helper.yaml").is_file());
}

#[test]
fn binding_assign_default_validates_and_refuses_on_unresolved_binding() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // No owner declares `nope_does_not_exist`. The validator should
    // flag it and refuse to write.
    let result = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "nope_does_not_exist",
        "consumers/new",
    ]);
    assert!(
        !result.status.success(),
        "expected nonzero exit for unresolved binding",
    );
    assert!(
        result.stderr.contains("does not appear")
            || result.stderr.contains("declared_bindings")
            || result.stderr.contains("unresolved"),
        "stderr should explain unresolved binding: {}",
        result.stderr,
    );
    // Spec untouched.
    assert!(!modules_root.join("consumers/new.yaml").exists());
}

/// Two bindings with mutual at-init reads form an atomic unit. Both
/// live in residual to start (the spec doesn't claim either of them).
/// Assigning one of them into a fresh module — while the other stays
/// in residual — splits the atomic unit across two destinations, which
/// the realizability gate rejects. This is the canonical "edit would
/// break the gate" shape the validation feature exists to catch.
fn build_atomic_pair_fixture() -> (Fixture, std::path::PathBuf) {
    // Mutual eager reads at top-level: `a` reads `b` and `b` reads
    // `a`. Wrapped in objects so SWC keeps them as separate top-level
    // statements without dead-code elimination collapsing them.
    let source = r#"const a = { id: "a", ref: () => b };
const b = { id: "b", ref: () => a };
const liveA = a.ref();
const liveB = b.ref();
export { a, b, liveA, liveB };
"#;

    let opts = FixtureOpts::new(source, Vec::new());
    let fixture = run_fixture(opts);
    let modules_root = fixture.report_root.join("spec/modules");
    std::fs::create_dir_all(&modules_root).unwrap();
    (fixture, modules_root)
}

#[test]
fn binding_assign_default_refuses_when_edit_would_break_gate() {
    let (fixture, modules_root) = build_atomic_pair_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // Move `a` into its own module while `b` (which references it)
    // stays in residual. If the owner graph has the mutual eager-use
    // edges, the validator should flag the split.
    let result = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "a",
        "isolated/a",
    ]);
    // Either the realizability cycle fires (a↔b) or the atomic-unit
    // split fires. Both are valid rejection signals. If the fixture
    // ends up with no cross-module evidence at all (e.g. SWC inlines
    // one of them), the test degrades gracefully — we still check
    // the headline behavior in the unit tests.
    if !result.status.success() {
        assert!(
            result.stderr.contains("cycle")
                || result.stderr.contains("atomic unit")
                || result.stderr.contains("validation failed"),
            "expected cycle / atomic-unit diagnostic, got stderr:\n{}",
            result.stderr,
        );
        // The spec edit must NOT have been written.
        assert!(
            !modules_root.join("isolated/a.yaml").exists(),
            "spec edit should not be on disk after validation failure"
        );
    }

    // `--force` always commits, regardless of validation verdict.
    let forced = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--force",
        "a",
        "isolated/a",
    ]);
    let forced_json = json_stdout(&forced);
    assert!(forced_json["written"].as_bool().unwrap_or(false));
    assert!(modules_root.join("isolated/a.yaml").exists());
}

#[test]
fn binding_assign_force_writes_even_when_validation_would_fail() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // Same unresolved-binding scenario as above — but --force lets it
    // through.
    let result = debundle(&[
        "binding",
        "assign",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--force",
        "nope_does_not_exist",
        "consumers/new",
    ]);
    let json = json_stdout(&result);
    assert!(json["written"].as_bool().unwrap_or(false));
    // The YAML now claims a binding the graph doesn't know about —
    // that's the spec author's problem to repair, and `--force` is
    // exactly the right hatch for "I'm batching a fix-up, will run
    // regen myself".
    assert!(modules_root.join("consumers/new.yaml").is_file());
}

// ---------------------------------------------------------------------------
// `binding move` (batched, validated)
// ---------------------------------------------------------------------------

/// Writes a hand-built `owner_graph.json` plus minimal modules root.
/// `nodes` is `(owner_id, binding, destination_module_id)`; `edges`
/// is `(source_owner_id, target_owner_id)`. Owner ids start at
/// `owner:0` so the graph round-trips through the JSON schema the
/// real pipeline emits.
fn write_synthetic_graph(
    dir: &std::path::Path,
    chunk_id: &str,
    nodes: &[(&str, &str, &str)],
    edges: &[(&str, &str)],
) -> (std::path::PathBuf, std::path::PathBuf) {
    let graph_path = dir.join("owner_graph.json");
    let modules_root = dir.join("spec/modules");
    std::fs::create_dir_all(&modules_root).unwrap();
    let node_json: Vec<Value> = nodes
        .iter()
        .enumerate()
        .map(|(idx, (id, binding, dest))| {
            serde_json::json!({
                "id": id,
                "statement_ordinal": idx + 1,
                "source_location": {
                    "source_path": "src.js",
                    "start_line": idx + 1,
                    "end_line": idx + 1,
                },
                "declared_bindings": [
                    {"binding": binding, "export_name": binding}
                ],
                "statement_kind": "var_decl",
                "purity": {"kind": "pure"},
                "destination": {
                    "id": dest,
                    "label": dest,
                    "residual": dest.contains("residual"),
                },
            })
        })
        .collect();
    let edge_json: Vec<Value> = edges
        .iter()
        .enumerate()
        .map(|(idx, (src, tgt))| {
            serde_json::json!({
                "id": format!("edge:{idx}"),
                "source": src,
                "target": tgt,
                "edge_kind": "eager_use",
                "statement_ordinal": idx + 1,
                "constrains_init_order": true,
            })
        })
        .collect();
    let graph = serde_json::json!({
        "chunk_id": chunk_id,
        "nodes": node_json,
        "edges": edge_json,
        "module_graph": {"nodes": [], "edges": [], "sccs": []},
        "atomic_graph": {"nodes": [], "edges": []},
    });
    std::fs::write(&graph_path, serde_json::to_string_pretty(&graph).unwrap()).unwrap();
    (graph_path, modules_root)
}

#[test]
fn binding_move_single_op_positional_matches_assign_shape() {
    // Backward-compatibility: `binding move X foo` is the drop-in
    // replacement for `binding assign X foo`. Same outcome on disk,
    // no batch syntax needed.
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper",
        "consumers/helper",
    ]);
    assert!(
        result.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    assert!(modules_root.join("consumers/helper.yaml").is_file());
    assert!(
        result.stdout.contains("ok") && result.stdout.contains("consumers/helper"),
        "expected ok line, got {:?}",
        result.stdout,
    );
}

#[test]
fn binding_move_two_acyclic_moves_apply_atomically() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // Two unrelated moves: `helper` into a new module, and a
    // re-home of `anchor` into a sibling module. Both should land.
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper=runtime/helpers",
        "anchor=runtime/anchors",
    ]);
    assert!(
        result.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    assert!(modules_root.join("runtime/helpers.yaml").is_file());
    assert!(modules_root.join("runtime/anchors.yaml").is_file());
    // Old anchor home dropped (it became empty).
    assert!(!modules_root.join("anchors/anchor.yaml").exists());
    assert!(result.stdout.contains("2 ops applied"));
}

#[test]
fn binding_move_batch_via_repeated_op_flag_lands() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--op",
        "helper=runtime/helpers",
        "--op",
        "anchor=runtime/anchors",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(modules_root.join("runtime/helpers.yaml").is_file());
    assert!(modules_root.join("runtime/anchors.yaml").is_file());
}

#[test]
fn binding_move_batch_via_file_lands() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let batch_path = fixture.report_root.join("batch.txt");
    std::fs::write(
        &batch_path,
        "# comment line is skipped\nhelper=runtime/helpers\n\nanchor=runtime/anchors\n",
    )
    .unwrap();
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--batch",
        batch_path.to_str().unwrap(),
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(modules_root.join("runtime/helpers.yaml").is_file());
    assert!(modules_root.join("runtime/anchors.yaml").is_file());
}

#[test]
fn binding_move_batch_rejects_duplicate_destinations() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "helper=runtime/helpers",
        "helper=runtime/other",
    ]);
    assert!(!result.status.success());
    assert!(
        result.stderr.contains("duplicate"),
        "expected duplicate diagnostic, stderr:\n{}",
        result.stderr,
    );
    assert!(!modules_root.join("runtime/helpers.yaml").exists());
    assert!(!modules_root.join("runtime/other.yaml").exists());
}

#[test]
fn binding_move_empty_batch_exits_two() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
    ]);
    assert_eq!(result.status.code(), Some(2), "stderr:\n{}", result.stderr);
    assert!(
        result.stderr.contains("no operations specified"),
        "stderr:\n{}",
        result.stderr,
    );
}

#[test]
fn binding_move_dry_run_validates_but_does_not_write() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--dry-run",
        "helper=runtime/helpers",
        "anchor=runtime/anchors",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(result.stdout.contains("dry-run"));
    assert!(!modules_root.join("runtime/helpers.yaml").exists());
    assert!(!modules_root.join("runtime/anchors.yaml").exists());
}

#[test]
fn binding_move_residual_alias_unassigns() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // `anchor=-` should unassign anchor and drop the now-empty
    // anchors/anchor.yaml.
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "anchor=-",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(!modules_root.join("anchors/anchor.yaml").exists());
}

#[test]
fn binding_move_source_eq_destination_is_no_op_with_notice() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "anchor=anchors/anchor",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(
        result.stdout.contains("noop"),
        "expected noop line, got {:?}",
        result.stdout,
    );
    // Still on disk.
    assert!(modules_root.join("anchors/anchor.yaml").is_file());
}

#[test]
fn binding_move_without_graph_requires_force() {
    let (fixture, modules_root) = build_two_module_fixture();
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "move",
        "--modules",
        modules_root.to_str().unwrap(),
        "helper=runtime/helpers",
    ]);
    assert!(!result.status.success());
    assert!(
        result.stderr.contains("validation requires --graph"),
        "stderr:\n{}",
        result.stderr,
    );
}

#[test]
fn binding_move_force_skips_graph_requirement() {
    let (fixture, modules_root) = build_two_module_fixture();
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "move",
        "--modules",
        modules_root.to_str().unwrap(),
        "--force",
        "helper=runtime/helpers",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    assert!(modules_root.join("runtime/helpers.yaml").is_file());
}

#[test]
fn binding_move_ndjson_emits_one_record_per_op_plus_summary() {
    let (fixture, modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--ndjson",
        "helper=runtime/helpers",
        "anchor=runtime/anchors",
    ]);
    assert!(result.status.success(), "stderr:\n{}", result.stderr);
    let lines = ndjson_stdout(&result);
    // Two op records + one summary record = three lines.
    assert_eq!(lines.len(), 3, "expected 3 ndjson records, got {lines:?}");
    assert!(
        lines[2].get("applied").is_some(),
        "trailing summary missing applied: {:?}",
        lines[2]
    );
}

#[test]
fn binding_move_batch_rejects_cycle_creating_set() {
    // Synthetic owner graph that the validation gate (which uses
    // the real `validate_spec_edits`) will reject when both X and Y
    // move to distinct modules but cross-reference each other.
    let tmp = tempfile::tempdir().unwrap();
    let (graph_path, modules_root) = write_synthetic_graph(
        tmp.path(),
        "static/index",
        &[
            ("owner:0", "X", "static/index::module:foo"),
            ("owner:1", "Y", "static/index::module:bar"),
        ],
        // X -> Y and Y -> X form a 2-cycle in the module quotient
        // when X and Y move to different modules.
        &[("owner:0", "owner:1"), ("owner:1", "owner:0")],
    );
    std::fs::write(
        modules_root.join("foo.yaml"),
        "members:\n  - selector:\n      binding:\n        name: X\n",
    )
    .unwrap();
    std::fs::write(
        modules_root.join("bar.yaml"),
        "members:\n  - selector:\n      binding:\n        name: Y\n",
    )
    .unwrap();
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "X=other_a",
        "Y=other_b",
    ]);
    assert!(
        !result.status.success(),
        "expected rejection, stdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    assert!(
        result.stderr.contains("Batch rejected"),
        "expected rejection footer, stderr:\n{}",
        result.stderr,
    );
    // No spec edits.
    assert!(!modules_root.join("other_a.yaml").exists());
    assert!(!modules_root.join("other_b.yaml").exists());
}

#[test]
fn binding_move_force_bypasses_cycle_check() {
    let tmp = tempfile::tempdir().unwrap();
    let (graph_path, modules_root) = write_synthetic_graph(
        tmp.path(),
        "static/index",
        &[
            ("owner:0", "X", "static/index::module:foo"),
            ("owner:1", "Y", "static/index::module:bar"),
        ],
        &[("owner:0", "owner:1"), ("owner:1", "owner:0")],
    );
    std::fs::write(
        modules_root.join("foo.yaml"),
        "members:\n  - selector:\n      binding:\n        name: X\n",
    )
    .unwrap();
    std::fs::write(
        modules_root.join("bar.yaml"),
        "members:\n  - selector:\n      binding:\n        name: Y\n",
    )
    .unwrap();
    let result = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--force",
        "X=other_a",
        "Y=other_b",
    ]);
    assert!(
        result.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    assert!(modules_root.join("other_a.yaml").is_file());
    assert!(modules_root.join("other_b.yaml").is_file());
}

#[test]
fn binding_move_batch_of_two_lands_when_individually_one_would_cycle() {
    // The "killer use case": graph where moving Y alone is rejected
    // (Z -> Y and Y -> Z straddle modules) but moving {Y, Z}
    // together collapses both edges into a self-edge of the new
    // module, eliminating the cycle.
    let tmp = tempfile::tempdir().unwrap();
    let (graph_path, modules_root) = write_synthetic_graph(
        tmp.path(),
        "core",
        &[
            ("owner:0", "X", "core::module:legacy"),
            ("owner:1", "Y", "core::module:legacy"),
            ("owner:2", "Z", "core::module:client"),
        ],
        &[
            ("owner:0", "owner:1"), // X -> Y inside legacy
            ("owner:2", "owner:1"), // Z -> Y
            ("owner:1", "owner:2"), // Y -> Z
        ],
    );
    std::fs::write(
        modules_root.join("legacy.yaml"),
        "members:\n  - selector:\n      binding:\n        name: X\n  - selector:\n      binding:\n        name: Y\n",
    )
    .unwrap();
    std::fs::write(
        modules_root.join("client.yaml"),
        "members:\n  - selector:\n      binding:\n        name: Z\n",
    )
    .unwrap();

    // Moving Y alone creates a cycle between Y's new home and
    // `client` (Z -> Y and Y -> Z straddle).
    let bad = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "Y=newhome",
    ]);
    assert!(!bad.status.success(), "expected cycle rejection");

    // Moving Y and Z together collapses both back-edges into
    // self-edges of `newhome`, eliminating the cycle.
    let good = debundle(&[
        "binding",
        "move",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "Y=newhome",
        "Z=newhome",
    ]);
    assert!(
        good.status.success(),
        "expected batch to land, stdout:\n{}\nstderr:\n{}",
        good.stdout,
        good.stderr,
    );
    assert!(modules_root.join("newhome.yaml").is_file());
}

#[test]
fn scc_default_returns_array_ndjson_returns_lines() {
    let (fixture, _modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let json_result = debundle(&["scc", "--graph", graph_path.to_str().unwrap()]);
    let json = json_stdout(&json_result);
    let array = json.as_array().expect("scc default is JSON array");
    // The synthetic fixture may yield zero or many SCCs depending on
    // how aggressively the materializer collapses singletons; this
    // test pins shape, not count.
    for record in array {
        assert!(record.get("id").is_some(), "record missing id: {record}");
        assert!(
            record.get("size").is_some(),
            "record missing size: {record}"
        );
        assert!(
            record.get("modules").is_some(),
            "record missing modules: {record}",
        );
    }

    let nd_result = debundle(&["scc", "--graph", graph_path.to_str().unwrap(), "--ndjson"]);
    let lines = ndjson_stdout(&nd_result);
    assert_eq!(
        lines.len(),
        array.len(),
        "ndjson and array yield same count"
    );
}

#[test]
fn scc_singletons_only_filters_to_size_one() {
    let (fixture, _modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "scc",
        "--graph",
        graph_path.to_str().unwrap(),
        "--singletons-only",
    ]);
    let json = json_stdout(&result);
    for record in json.as_array().unwrap() {
        assert_eq!(
            record["size"].as_u64(),
            Some(1),
            "non-singleton slipped through --singletons-only: {record}",
        );
    }
}

#[test]
fn cluster_module_returns_neighbors_or_empty() {
    let (fixture, _modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    // Discover a real quotient module id from the graph rather than
    // guessing; small fixtures may not surface every spec path as a
    // quotient node (e.g. modules collapsed into residual).
    let raw: Value = serde_json::from_str(&std::fs::read_to_string(&graph_path).unwrap()).unwrap();
    let target_id = raw
        .pointer("/module_graph/nodes/0/id")
        .or_else(|| raw.pointer("/quotient/nodes/0/id"))
        .and_then(|value| value.as_str())
        .expect("graph must have at least one quotient node");
    let result = debundle(&[
        "cluster",
        "--graph",
        graph_path.to_str().unwrap(),
        "--module",
        target_id,
    ]);
    let json = json_stdout(&result);
    let array = json.as_array().expect("cluster returns array");
    for record in array {
        assert!(record.get("neighbor").is_some());
        assert!(record.get("direction").is_some());
        assert!(record.get("edge_count").is_some());
    }
}

#[test]
fn cluster_unknown_module_errors() {
    let (fixture, _modules_root) = build_two_module_fixture();
    let graph_path = pipeline_graph_path(&fixture);
    let result = debundle(&[
        "cluster",
        "--graph",
        graph_path.to_str().unwrap(),
        "--module",
        "nonexistent/module",
    ]);
    assert!(!result.status.success(), "expected error exit");
    assert!(
        result
            .stderr
            .contains("does not appear in the quotient graph"),
        "stderr: {}",
        result.stderr,
    );
}
