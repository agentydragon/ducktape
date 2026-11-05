//! A batch uses the same query semantics as separate real CLI invocations.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{TreeFixture, debundler_path, run_tree_graph_fixture};
use serde_json::{Value, json};
use std::fs;
use std::path::Path;
use std::process::Command;

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

fn joint_scope_fixture() -> debundle_e2e_support::TreeRun {
    run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "main",
                    "const target = 1;\nfunction callback() { return target; }\ncallback();\n",
                ),
                ("other", "const callback = 2;\n"),
            ],
            module_roots: &[("left", "main"), ("right", "main"), ("other", "other")],
            modules: &[
                (
                    "left/leaf.yaml",
                    "members: [{selector: {binding: {name: callback}}, name: Callback}]\n",
                ),
                (
                    "right/helper.yaml",
                    "members: [{selector: {binding: {name: target}}, name: Target}]\n",
                ),
                (
                    "other/leaf.yaml",
                    "members: [{selector: {binding: {name: callback}}, name: ForeignCallback}]\n",
                ),
            ],
        },
        &[],
    )
}

fn joint_scope_command(run: &debundle_e2e_support::TreeRun, args: &[&str]) -> std::process::Output {
    let root = run.root();
    let config = root.join("spec_config.yaml");
    joint_scope_command_with_config(run, args, &config)
}

fn joint_scope_command_with_config(
    run: &debundle_e2e_support::TreeRun,
    args: &[&str],
    config: &Path,
) -> std::process::Output {
    joint_scope_command_with_roots(run, args, &run.root().join("modules"), config)
}

fn joint_scope_command_with_roots(
    run: &debundle_e2e_support::TreeRun,
    args: &[&str],
    modules: &Path,
    config: &Path,
) -> std::process::Output {
    let root = run.root();
    let graph = root.join("out/reports/tree/main/dependency_graph.json.gz");
    let source_root = root.join("snapshot");
    Command::new(debundler_path())
        .args(args)
        .arg("--graph")
        .arg(graph)
        .arg("--modules")
        .arg(modules)
        .arg("--tree-config")
        .arg(config)
        .arg("--source-root")
        .arg(source_root)
        .arg("--format")
        .arg("json")
        .output()
        .expect("run scoped describe")
}

#[test]
fn describe_scopes_joint_module_roots_from_tree_config() {
    let run = joint_scope_fixture();
    assert!(run.result.status.success(), "{}", run.result.stderr);

    let unit = joint_scope_command(&run, &["describe", "unit:1"]);
    assert!(
        unit.status.success(),
        "{}",
        String::from_utf8_lossy(&unit.stderr)
    );
    let unit: Value = serde_json::from_slice(&unit.stdout).unwrap();
    assert_eq!(unit["source_unit_ids"], json!(["unit:1"]));
    let logical_id = unit["source_units"][0]["destination"]
        .as_str()
        .expect("source-unit destination key");
    let logical = joint_scope_command(&run, &["describe", logical_id]);
    assert!(
        logical.status.success(),
        "{}",
        String::from_utf8_lossy(&logical.stderr)
    );
    let logical: Value = serde_json::from_slice(&logical.stdout).unwrap();
    assert_eq!(logical["query"]["kind"], "module");
    assert!(
        logical["source_unit_ids"]
            .as_array()
            .unwrap()
            .contains(&json!("unit:1"))
    );

    let atomic_id = unit["atomic_groups"][0]["id"]
        .as_str()
        .expect("source-unit atomic group");
    let atomic = joint_scope_command(&run, &["describe", atomic_id]);
    assert!(
        atomic.status.success(),
        "{}",
        String::from_utf8_lossy(&atomic.stderr)
    );
    let atomic: Value = serde_json::from_slice(&atomic.stdout).unwrap();
    assert_eq!(atomic["query"]["kind"], "atomic_group");
    assert!(
        atomic["source_unit_ids"]
            .as_array()
            .unwrap()
            .contains(&json!("unit:1"))
    );
    let homes = unit["binding_homes"].as_array().unwrap();
    assert!(
        homes
            .iter()
            .any(|home| home["binding"] == "callback" && home["path"] == "left/leaf")
    );
    assert!(!homes.iter().any(|home| home["name"] == "ForeignCallback"));

    let target = joint_scope_command(&run, &["describe", "unit:0"]);
    assert!(
        target.status.success(),
        "{}",
        String::from_utf8_lossy(&target.stderr)
    );
    let target: Value = serde_json::from_slice(&target.stdout).unwrap();
    assert_eq!(target["source_unit_ids"], json!(["unit:0"]));
    let target_homes = target["binding_homes"].as_array().unwrap();
    assert!(
        target_homes
            .iter()
            .any(|home| home["binding"] == "target" && home["path"] == "right/helper")
    );

    let leaf = joint_scope_command(&run, &["describe", "leaf"]);
    assert!(
        leaf.status.success(),
        "{}",
        String::from_utf8_lossy(&leaf.stderr)
    );
    let leaf: Value = serde_json::from_slice(&leaf.stdout).unwrap();
    assert_eq!(leaf["query"]["kind"], "module");
    assert_eq!(leaf["query"]["value"], "left/leaf");
    assert_eq!(leaf["source_unit_ids"], json!(["unit:1"]));

    let helper = joint_scope_command(&run, &["describe", "right/helper"]);
    assert!(
        helper.status.success(),
        "{}",
        String::from_utf8_lossy(&helper.stderr)
    );
    let helper: Value = serde_json::from_slice(&helper.stdout).unwrap();
    assert_eq!(helper["source_unit_ids"], json!(["unit:0"]));

    let callback = joint_scope_command(&run, &["describe", "callback", "--include-proposals"]);
    assert!(
        callback.status.success(),
        "{}",
        String::from_utf8_lossy(&callback.stderr)
    );
    let callback: Value = serde_json::from_slice(&callback.stdout).unwrap();
    assert_eq!(callback["query"]["kind"], "binding");
    assert!(callback.get("module_proposals").is_some());
    assert!(callback.get("module_proposal_diagnostics").is_some());

    let batch_path = run.root().join("describe-ids.json");
    fs::write(
        &batch_path,
        serde_json::to_vec(&["unit:1", "leaf", "right/helper", "callback"]).unwrap(),
    )
    .unwrap();
    let batch_path = batch_path.to_str().unwrap();
    let batch = joint_scope_command(&run, &["describe", "--batch", batch_path]);
    assert!(
        batch.status.success(),
        "{}",
        String::from_utf8_lossy(&batch.stderr)
    );
    let batch: Value = serde_json::from_slice(&batch.stdout).unwrap();
    assert_eq!(batch["results"].as_array().unwrap().len(), 4);
    assert_eq!(batch["results"][0]["source_unit_ids"], json!(["unit:1"]));
    assert_eq!(batch["results"][1]["source_unit_ids"], json!(["unit:1"]));
    assert_eq!(batch["results"][2]["source_unit_ids"], json!(["unit:0"]));
    assert_eq!(batch["results"][3]["query"]["kind"], "binding");

    let wrong_config = run.root().join("wrong_config.yaml");
    fs::copy(run.root().join("spec_config.yaml"), &wrong_config).unwrap();
    let wrong_config =
        joint_scope_command_with_config(&run, &["describe", "unit:1"], &wrong_config);
    assert!(!wrong_config.status.success());
    let rejection: Value = serde_json::from_slice(&wrong_config.stdout).unwrap();
    assert_eq!(rejection["kind"], "graph_compatibility");

    fs::write(
        run.root().join("snapshot/main.js"),
        "const target = 2;\nfunction callback() { return target; }\ncallback();\n",
    )
    .unwrap();
    let changed_source = joint_scope_command(&run, &["describe", "unit:1"]);
    assert!(!changed_source.status.success());
    let rejection: Value = serde_json::from_slice(&changed_source.stdout).unwrap();
    assert_eq!(rejection["kind"], "graph_compatibility");
    assert_eq!(rejection["mismatch"]["reason"], "file");
}

#[test]
fn describe_resolves_joint_relations_and_anonymous_claims_across_roots() {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "main",
                    "const anchor = 1;\nfunction consumer() { return anchor; }\nconsole.log(\"tail\");\n",
                ),
                ("other", "const unrelated = 2;\n"),
            ],
            module_roots: &[("left", "main"), ("right", "main"), ("foreign", "other")],
            modules: &[
                (
                    "left/anchor.yaml",
                    "source_matches: [{match: 'const anchor = 1;', bindings: [{local: anchor, name: Anchor}]}]\n",
                ),
                (
                    "right/consumer.yaml",
                    "members: [{name: Consumer, selector: {cross_ref: {references: Anchor, kind: function_declaration}}}]\n",
                ),
                (
                    "right/log.yaml",
                    "anonymous_statements: [{match: 'console.log(\"tail\");'}]\n",
                ),
            ],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);

    let consumer = joint_scope_command(&run, &["describe", "right/consumer"]);
    assert!(
        consumer.status.success(),
        "{}",
        String::from_utf8_lossy(&consumer.stderr)
    );
    let consumer: Value = serde_json::from_slice(&consumer.stdout).unwrap();
    assert_eq!(consumer["query"]["kind"], "module");
    assert_eq!(consumer["query"]["value"], "right/consumer");
    assert_eq!(consumer["source_unit_ids"], json!(["unit:1"]));

    let anonymous = joint_scope_command(&run, &["describe", "right/log"]);
    assert!(
        anonymous.status.success(),
        "{}",
        String::from_utf8_lossy(&anonymous.stderr)
    );
    let anonymous: Value = serde_json::from_slice(&anonymous.stdout).unwrap();
    assert_eq!(anonymous["query"]["kind"], "module");
    assert_eq!(anonymous["query"]["value"], "right/log");
    assert_eq!(anonymous["source_unit_ids"], json!(["unit:2"]));
}

#[test]
fn describe_accepts_verified_exact_chunk_subtree_with_tree_config() {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "const selected = 1;\n"),
                ("other", "const other = 2;\n"),
            ],
            module_roots: &[("home", "main"), ("foreign", "other")],
            modules: &[(
                "home/leaf.yaml",
                "members: [{selector: {binding: {name: selected}}, name: Selected}]\n",
            )],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);

    let root = run.root();
    let modules = root.join("modules/home");
    let config = root.join("spec_config.yaml");
    let leaf = joint_scope_command_with_roots(&run, &["describe", "leaf"], &modules, &config);
    assert!(
        leaf.status.success(),
        "{}",
        String::from_utf8_lossy(&leaf.stderr)
    );
    let leaf: Value = serde_json::from_slice(&leaf.stdout).unwrap();
    assert_eq!(leaf["query"]["kind"], "module");
    assert_eq!(leaf["query"]["value"], "home/leaf");
    assert_eq!(leaf["source_unit_ids"], json!(["unit:0"]));
    assert!(
        leaf["binding_homes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|home| home["binding"] == "selected" && home["path"] == "home/leaf")
    );
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
