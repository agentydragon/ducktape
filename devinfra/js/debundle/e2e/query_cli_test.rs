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
    let _ = fixture; // keep tempdir alive
    // Move helper from residual into a new module.
    let assign = debundle(&[
        "binding",
        "assign",
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
    let helper_yaml = modules_root.join("consumers/helper.yaml");
    assert!(helper_yaml.is_file());

    // Re-assign to a different module; old YAML should disappear
    // because it becomes empty.
    let reassign = debundle(&[
        "binding",
        "assign",
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
    let _ = fixture;
    let result = debundle(&[
        "binding",
        "assign",
        "--modules",
        modules_root.to_str().unwrap(),
        "--dry-run",
        "helper",
        "consumers/helper",
    ]);
    let json = json_stdout(&result);
    assert!(json["dry_run"].as_bool().unwrap_or(false));
    assert!(!modules_root.join("consumers/helper.yaml").exists());
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
