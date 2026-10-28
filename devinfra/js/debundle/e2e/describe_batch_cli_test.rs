//! A batch uses the same query semantics as separate real CLI invocations.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};
use std::fs;

fn fixture() -> GraphFixture {
    GraphFixture::new(
        "const seed = 1;\nconst value = seed + 1;\nconst extra = value + 1;\nconsole.log(extra);\n",
        &[(
            "home/values.yaml",
            "members: [{selector: {binding: {name: seed}}, name: initialValue}, {selector: {binding: {name: value}}}]\n",
        )],
    )
}

fn census_fixture() -> GraphFixture {
    GraphFixture::new(
        "const selected = 0;\n\
         const directReader = selected;\n\
         function calledAtInit() { return selected; }\n\
         calledAtInit();\n\
         function later() { return selected; }\n\
         const anotherDirectReader = selected;\n",
        &[],
    )
}

fn batch_file(fixture: &GraphFixture, ids: &[&str]) -> String {
    let path = fixture.root().join("describe-ids.json");
    fs::write(&path, serde_json::to_vec(ids).unwrap()).unwrap();
    path.to_str().unwrap().to_string()
}

fn assert_equivalent(fixture: &GraphFixture, ids: &[&str], options: &[&str]) {
    let path = batch_file(fixture, ids);
    let mut args = vec!["describe", "--batch", &path];
    args.extend(options);
    let batch = fixture.json(&args);
    let results = batch["results"].as_array().unwrap();
    assert_eq!(results.len(), ids.len());
    for (id, result) in ids.iter().zip(results) {
        let mut args = vec!["describe", id];
        args.extend(options);
        assert_eq!(*result, fixture.json(&args), "{id}");
    }
}

#[test]
fn batch_preserves_order_duplicates_and_every_selection_kind() {
    let fixture = fixture();
    let module = fixture.dependency_graph().nodes[0].destination.to_string();
    let ids = [
        "seed",
        "initialValue",
        "unit:0",
        "atomic:0",
        "home/values",
        &module,
        "seed",
    ];
    assert_equivalent(&fixture, &ids, &[]);
    assert_equivalent(&fixture, &ids, &["--limit", "1"]);
    assert_equivalent(&fixture, &ids, &["--all"]);
    assert_equivalent(
        &fixture,
        &ids,
        &[
            "--direction",
            "incoming",
            "--reason",
            "eager_use",
            "--limit",
            "1",
        ],
    );
}

#[test]
fn census_is_single_batch_equivalent_and_counts_internal_edge_incidence() {
    let fixture = census_fixture();
    let ids = ["selected", "unit:0"];
    let options = [
        "--direction",
        "incoming",
        "--reason",
        "eager_use",
        "--limit",
        "1",
    ];
    assert_equivalent(&fixture, &ids, &options);

    let limited = fixture.json(&[
        "describe",
        "selected",
        "--direction",
        "incoming",
        "--reason",
        "eager_use",
        "--limit",
        "1",
    ]);
    assert_eq!(limited["incoming_edges"].as_array().unwrap().len(), 1);
    assert_eq!(limited["limits"]["sections"]["incoming_edges"]["total"], 3);
    assert_eq!(
        limited["edge_census"]["incoming"].as_array().unwrap().len(),
        2
    );
    assert_eq!(limited["edge_census"]["outgoing"], json!([]));

    let internal = GraphFixture::new(
        "const seed = 1;\nconst value = seed + 1;\n",
        &[(
            "owners.yaml",
            "members: [{selector: {binding: {name: seed}}}, {selector: {binding: {name: value}}}]\n",
        )],
    );
    let report = internal.json(&["describe", "owners"]);
    assert_eq!(report["source_unit_ids"], json!(["unit:0", "unit:1"]));
    let expected_incidence = json!([
        {"edge_kind": "eager_use", "role": "direct", "count": 1}
    ]);
    assert_eq!(report["edge_census"]["incoming"], expected_incidence);
    assert_eq!(report["edge_census"]["outgoing"], expected_incidence);
    assert_eq!(report["limits"]["sections"]["incoming_edges"]["total"], 1);
    assert_eq!(report["limits"]["sections"]["outgoing_edges"]["total"], 1);

    let owned = ["owners"];
    assert_equivalent(&internal, &owned, &["--limit", "1"]);
}

#[test]
fn proposal_results_do_not_enable_proposals_for_later_plain_queries() {
    let fixture = fixture();
    let proposals = fixture.json(&["modules", "propose"]);
    let proposal = proposals["proposals"][0]["proposed_module_id"]
        .as_str()
        .unwrap();
    let ids = [proposal, "seed", proposal];
    assert_equivalent(&fixture, &ids, &[]);
    assert_equivalent(&fixture, &ids, &["--include-proposals"]);
}

#[test]
fn batch_preserves_joint_source_matches_and_anonymous_module_claims() {
    let fixture = GraphFixture::new(
        "let alpha = 1;\nfunction beta() { alpha = 2; }\nconsole.log(\"task\");\n",
        &[
            (
                "features/state.yaml",
                "members: [{selector: {binding: {name: beta}}}]\n\
                 source_matches: [{match: 'let alpha = 1;', bindings: [{local: alpha, name: Alpha}]}]\n",
            ),
            (
                "features/log.yaml",
                "anonymous_statements: [{match: 'console.log(\"task\");'}]\n",
            ),
        ],
    );
    let ids = [
        "features/state",
        "features/log",
        "Alpha",
        "features/state",
        "unit:2",
    ];
    assert_equivalent(&fixture, &ids, &[]);
    assert_equivalent(&fixture, &ids, &["--limit", "1"]);
}

#[test]
fn ndjson_is_one_complete_report_per_requested_id() {
    let fixture = fixture();
    let ids = ["seed", "value", "unit:2"];
    let path = batch_file(&fixture, &ids);
    let output = fixture.command(&["describe", "--batch", &path, "--format", "ndjson"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let lines: Vec<Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let expected: Vec<_> = ids
        .iter()
        .map(|id| fixture.json(&["describe", id]))
        .collect();
    assert_eq!(lines, expected);
}

#[test]
fn failed_item_returns_only_failure_without_partial_results() {
    let fixture = fixture();
    let path = batch_file(&fixture, &["seed", "unit:999999", "value"]);
    for format in ["json", "ndjson"] {
        let output = fixture.command(&["describe", "--batch", &path, "--format", format]);
        assert_eq!(output.status.code(), Some(1));
        let error: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(error.get("results").is_none());
        assert_eq!(error["reason"], "source_unit_not_found");
        assert!(error["message"].as_str().unwrap().contains("item 2"));
        assert!(String::from_utf8_lossy(&output.stderr).contains("item 2"));
    }
}

#[test]
fn batch_checks_input_compatibility_before_any_result() {
    let fixture = fixture();
    let path = batch_file(&fixture, &["seed", "value"]);
    fs::write(fixture.source_path(), "const seed = 100;\n").unwrap();
    let output = fixture.command(&["describe", "--batch", &path, "--format", "json"]);
    assert_eq!(output.status.code(), Some(1));
    let error: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(error["kind"], "graph_compatibility");
    assert_eq!(error["mismatch"]["reason"], "file");
}

#[test]
fn batch_input_errors_and_conflicting_single_id_are_rejected() {
    let fixture = fixture();
    let path = batch_file(&fixture, &[]);
    let output = fixture.command(&["describe", "--batch", &path, "--format", "json"]);
    assert_eq!(output.status.code(), Some(1));
    let _: Value = serde_json::from_slice(&output.stdout).unwrap();
    fs::write(&path, br#"["seed", 123]"#).unwrap();
    let output = fixture.command(&["describe", "--batch", &path, "--format", "json"]);
    assert_eq!(output.status.code(), Some(1));
    let _: Value = serde_json::from_slice(&output.stdout).unwrap();
    let output = fixture.command(&["describe", "seed", "--batch", &path]);
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn resolved_invocation_replays_the_explicit_batch_file() {
    let fixture = fixture();
    let path = batch_file(&fixture, &["seed", "value"]);
    let record = fixture.json(&["describe", "--batch", &path, "--print-invocation"]);
    assert_eq!(record["mode"], "graph_snapshot_read");
    assert_eq!(record["requested_proof"], "graph_snapshot_input");
    assert_eq!(record["will_execute"], false);
    let batch_argument = record["arguments"]
        .as_array()
        .unwrap()
        .iter()
        .find(|argument| argument["name"] == "batch_file")
        .unwrap();
    assert_eq!(batch_argument["value"]["paths"], json!([&path]));
    let argv = record["argv"].as_array().unwrap();
    assert!(
        argv.windows(2)
            .any(|pair| pair == [json!("--batch"), json!(&path)])
    );
    let output = std::process::Command::new(argv[0].as_str().unwrap())
        .args(argv[1..].iter().map(|arg| arg.as_str().unwrap()))
        .current_dir(record["cwd"].as_str().unwrap())
        .env_remove("DEBUNDLE_GRAPH")
        .env_remove("DEBUNDLE_MODULES")
        .env_remove("DEBUNDLE_SOURCE_ROOT")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let replay: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(replay, fixture.json(&["describe", "--batch", &path]));
}
