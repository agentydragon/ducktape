//! Query the artifacts emitted from real JS/spec inputs, through the CLI.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;

fn fixture() -> GraphFixture {
    GraphFixture::new("const ZZ = class PaymentError {};\nconst aa = ZZ;\n", &[])
}

#[test]
fn inspect_source_graph_id_selects_only_its_statement_on_one_line() {
    let fixture = GraphFixture::new("const a = 1; const b = 2; console.log(a, b);", &[]);
    let report = fixture.json(&["inspect-source", "--id", "unit:1", "--view", "raw"]);
    assert_eq!(
        report["files"][0]["statements"].as_array().unwrap().len(),
        1
    );
    let statement = &report["files"][0]["statements"][0];
    assert_eq!(statement["index"], 1);
    assert_eq!(statement["source_unit_ids"], json!(["unit:1"]));
    assert_eq!(statement["source"], "const b = 2;");
}

#[test]
fn inspect_source_maps_split_graph_ordinals_to_one_raw_body_item() {
    let fixture = GraphFixture::new(
        "const a = 1, b = 2; console.log(a, b);",
        &[(
            "foo.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let report = fixture.json(&["inspect-source", "--id", "unit:1", "--view", "raw"]);
    let statement = &report["files"][0]["statements"][0];
    assert_eq!(statement["index"], 0);
    assert_eq!(statement["source_unit_ids"], json!(["unit:1"]));
    assert_eq!(statement["source"], "const a = 1, b = 2;");
    let module = fixture.json(&["inspect-source", "--id", "foo", "--view", "raw"]);
    assert_eq!(
        module["files"][0]["statements"].as_array().unwrap().len(),
        1
    );
    assert_eq!(
        module["files"][0]["statements"][0]["source_unit_ids"],
        json!(["unit:0", "unit:1"])
    );
}

#[test]
fn inspect_source_keeps_separated_module_statements_separate() {
    let fixture = GraphFixture::new(
        "const a = 1;\nconst b = 2;\nconst c = 3;\nconsole.log(a, b, c);\n",
        &[(
            "foo.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: c}}}]\n",
        )],
    );
    let report = fixture.json(&["inspect-source", "--id", "foo", "--view", "raw"]);
    let statements = report["files"][0]["statements"].as_array().unwrap();
    assert_eq!(statements.len(), 2);
    assert_eq!(statements[0]["index"], 0);
    assert_eq!(statements[1]["index"], 2);
    assert_eq!(statements[0]["source"], "const a = 1;");
    assert_eq!(statements[1]["source"], "const c = 3;");
}

#[test]
fn inspect_source_bounds_long_lines_and_serialized_output() {
    let source = format!("const a = \"{}\"; const b = 1;", "x".repeat(2000));
    let fixture = GraphFixture::new(&source, &[]);
    let report = fixture.json(&[
        "inspect-source",
        "--id",
        "unit:0",
        "--view",
        "raw",
        "--max-line-bytes",
        "32",
    ]);
    let statement = &report["files"][0]["statements"][0];
    assert!(statement["source"].as_str().unwrap().len() <= 32);
    assert!(statement["omitted_source_bytes"].as_u64().unwrap() > 1900);
    assert!(statement["byte_end"].as_u64().unwrap() > 2000);

    let out = fixture.command(&[
        "inspect-source",
        "--id",
        "unit:0",
        "--view",
        "raw",
        "--max-line-bytes",
        "0",
        "--max-output-bytes",
        "1200",
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(out.stdout.len() <= 1200, "{} bytes", out.stdout.len());
    let bounded: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(bounded["files"][0]["statements"][0]["index"], 0);
    assert!(
        bounded["files"][0]["statements"][0]["omitted_source_bytes"]
            .as_u64()
            .unwrap()
            > 0
    );
    let text = fixture.command(&[
        "inspect-source",
        "--id",
        "unit:0",
        "--max-line-bytes",
        "0",
        "--max-output-bytes",
        "1200",
        "--format",
        "text",
    ]);
    assert!(
        text.status.success(),
        "{}",
        String::from_utf8_lossy(&text.stderr)
    );
    assert!(text.stdout.len() <= 1200, "{} bytes", text.stdout.len());
    assert!(String::from_utf8_lossy(&text.stdout).contains("body[0] bytes="));
}

#[test]
fn inspect_source_direct_range_uses_raw_body_indices_and_default_item_limit() {
    let source = (0..40)
        .map(|index| format!("const a{index} = {index};"))
        .collect::<Vec<_>>()
        .join("\n");
    let fixture = GraphFixture::new(&source, &[]);
    let path = fixture.source_path();
    let path = path.to_str().unwrap();
    let selected = fixture.json(&[
        "inspect-source",
        "--source-file",
        path,
        "--statements",
        "39",
        "--view",
        "raw",
    ]);
    let statement = &selected["files"][0]["statements"][0];
    assert_eq!(statement["index"], 39);
    assert_eq!(statement["source"], "const a39 = 39;");
    assert_eq!(statement["source_unit_ids"], json!([]));

    let all = fixture.json(&["inspect-source", "--source-file", path]);
    assert_eq!(all["files"][0]["statements"].as_array().unwrap().len(), 32);
    assert_eq!(all["omitted_items"], 8);
}

#[test]
fn inspect_source_rejects_stale_graph_source_and_old_command() {
    let fixture = GraphFixture::new("const a = 1;\n", &[]);
    fs::write(fixture.source_path(), "\nconst a = 1;\n").unwrap();
    let out = fixture.command(&["inspect-source", "--id", "unit:0"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("regenerate the dependency graph"));
    let out = fixture.command(&["show-source", "unit:0"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("unrecognized subcommand"));
}

#[test]
fn atoms_summary_and_proposals_describe_the_real_graph() {
    let fixture = fixture();
    let atoms = fixture.json(&["atoms"]);
    assert_eq!(atoms["atomic_groups"].as_array().unwrap().len(), 2);
    let summary = fixture.json(&["graph-summary"]);
    assert_eq!(summary["source_unit_count"], 2);
    assert_eq!(summary["atomic_group_count"], 2);
    assert!(summary.get("proposal_count").is_none());
    assert!(summary.get("diagnostic_count").is_none());
    let proposals = fixture.json(&["modules", "propose", "--size-cap-lines", "10000"]);
    assert!(!proposals["proposals"].as_array().unwrap().is_empty());
}

#[test]
fn describe_and_inspect_source_dispatch_bindings_and_module_ids() {
    let fixture = fixture();
    for id in ["ZZ", "unit:0", "atomic:0"] {
        let report = fixture.json(&["describe", id]);
        assert_eq!(report["source_unit_ids"], json!(["unit:0"]), "{id}");
        assert_eq!(report["atomic_groups"][0]["id"], "atomic:0");
        if id == "atomic:0" {
            assert_eq!(report["query"]["kind"], "atomic_group");
        }
    }
    let graph = fixture.dependency_graph();
    let destination = &graph.nodes[0].destination;
    let report = fixture.json(&["describe", destination.as_str()]);
    assert_eq!(report["source_unit_ids"], json!(["unit:0", "unit:1"]));
    let report = fixture.json(&["inspect-source", "--id", "ZZ"]);
    assert_eq!(
        report["files"][0]["statements"].as_array().unwrap().len(),
        1
    );
    assert!(
        report["files"][0]["statements"][0]["source"]
            .as_str()
            .unwrap()
            .contains("class PaymentError")
    );
}

#[test]
fn coverage_counts_anonymous_statement_claims() {
    let fixture = GraphFixture::new(
        "let value = 0;\nvalue = 1;\n",
        &[(
            "features/state.yaml",
            "members: [{name: Readable, selector: {binding: {name: value}}}]\nanonymous_statements: [{match: 'value = 1;'}]\n",
        )],
    );
    let report = fixture.json(&["coverage"]);
    assert_eq!(
        report["summary"],
        json!({"total_patch_sets":1,"complete_patch_sets":1,"split_patch_sets":0,"unknown_binding_count":0})
    );
    let row = &report["rows"][0];
    assert_eq!(row["path"], "features/state");
    assert_eq!(row["status"], "complete_atomic_groups");
    assert_eq!(row["complete_atomic_group_ids"], json!(["atomic:0"]));
    assert_eq!(row["missing_anonymous_source_unit_ids"], json!([]));
}

#[test]
fn source_match_claims_combine_with_members() {
    let fixture = GraphFixture::new(
        "let alpha = 1;\nfunction beta() { alpha = 2; }\n",
        &[(
            "features/state.yaml",
            "members: [{selector: {binding: {name: beta}}}]\n\
             source_matches: [{match: 'let alpha = 1;', bindings: [{local: alpha, name: Alpha}]}]\n",
        )],
    );
    let coverage = fixture.json(&["coverage"]);
    assert_eq!(coverage["summary"]["total_patch_sets"], 1);
    assert_eq!(coverage["summary"]["complete_patch_sets"], 1);
    assert_eq!(coverage["summary"]["split_patch_sets"], 0);
    let row = &coverage["rows"][0];
    assert_eq!(row["status"], "complete_atomic_groups");
    assert_eq!(row["requested_binding_ids"], json!(["alpha", "beta"]));
    assert_eq!(row["complete_atomic_group_ids"], json!(["atomic:0"]));
    assert_eq!(row["missing_binding_ids"], json!([]));
    assert_eq!(row["missing_source_unit_ids"], json!([]));
    let describe = fixture.json(&["describe", "features/state"]);
    assert_eq!(describe["source_unit_ids"], json!(["unit:0", "unit:1"]));
    assert_eq!(
        describe["bindings"]
            .as_array()
            .unwrap()
            .iter()
            .map(|b| b["binding"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["alpha", "beta"]
    );
}

#[test]
fn anonymous_only_module_path_dispatches_before_proposal_prefix() {
    let fixture = GraphFixture::new(
        "console.log(\"task\");\n",
        &[(
            "auto_partition/auto_partition_0187.yaml",
            "anonymous_statements: [{match: 'console.log(\"task\");'}]\n",
        )],
    );
    for id in ["auto_partition/auto_partition_0187", "auto_partition_0187"] {
        let report = fixture.json(&["describe", id]);
        assert_eq!(report["query"]["kind"], "module");
        assert_eq!(report["source_unit_ids"], json!(["unit:0"]));
        assert_eq!(
            report["atomic_groups"][0]["anonymous_statement_source_unit_ids"],
            json!(["unit:0"])
        );
    }
    let report = fixture.json(&[
        "inspect-source",
        "--id",
        "auto_partition/auto_partition_0187",
        "--view",
        "raw",
    ]);
    assert_eq!(
        report["files"][0]["statements"].as_array().unwrap().len(),
        1
    );
    assert!(
        report["files"][0]["statements"][0]["source"]
            .as_str()
            .unwrap()
            .contains("console.log(\"task\");")
    );
}

#[test]
fn duplicate_anonymous_statements_are_advisory_not_landable_proposals() {
    let fixture = GraphFixture::new("console.log(\"task\");\nconsole.log(\"task\");\n", &[]);
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "10000"]);
    let proposals = report["proposals"].as_array().unwrap();
    // Real sequencing edges may group both statements into one proposal.
    // Whichever assignment the heuristic chooses, neither ambiguous source_unit is
    // addressable by a unique selector, and no containing proposal is landable.
    let mut unaddressable: Vec<_> = proposals
        .iter()
        .flat_map(|p| {
            p["unaddressable_anonymous_source_unit_ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|id| id.as_str().unwrap())
        })
        .collect();
    unaddressable.sort_unstable();
    assert_eq!(unaddressable, ["unit:0", "unit:1"]);
    assert!(proposals.iter().all(|p| p["landable_today"] == false));
}

#[test]
fn stale_proposal_and_diagnostic_ids_report_their_kind_and_recovery_command() {
    let fixture = fixture();
    for (id, kind) in [
        ("auto_partition_0499", "proposal"),
        ("diagnostic:size_cap_0001", "diagnostic"),
    ] {
        let out = fixture.command(&["inspect-source", "--id", id]);
        assert!(!out.status.success());
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(stderr.contains(&format!("{kind} id {id:?}")), "{stderr}");
        assert!(stderr.contains("debundle modules propose"), "{stderr}");
    }
}

#[test]
fn describe_text_includes_binding_home_module_paths() {
    let fixture = GraphFixture::new(
        "const a = 1; console.log(a);",
        &[(
            "runtime/plugins.yaml",
            "members: [{name: Readable, selector: {binding: {name: a}}}]",
        )],
    );
    let out = fixture.command(&["describe", "a", "--format", "text"]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("runtime/plugins"), "{text}");
    let report = fixture.json(&["describe", "a"]);
    assert_eq!(report["binding_homes"][0]["path"], "runtime/plugins");
}

const PATCHED_SOURCE: &str =
    "const ZZ = class PaymentError {};\nconst aa = ZZ;\nconst bb = 2;\nconst cc = ZZ;\n";

/// `ZZ` carries a binding patch and is read at init by `aa` and `cc`; `bb` is
/// claimed (and renamed) by a module. The residual owners are `ZZ`, `aa`, `cc`.
fn patched_fixture() -> GraphFixture {
    GraphFixture::new(
        PATCHED_SOURCE,
        &[
            (
                "../binding_patches.yaml",
                "members:\n  - name: PaymentError\n    selector:\n      binding:\n        name: ZZ\n",
            ),
            (
                "home/bb.yaml",
                "members: [{name: Beta, selector: {binding: {name: bb}}}]\n",
            ),
        ],
    )
}

/// The string at `field` of every element of `report[key]`.
fn field_of_each<'a>(report: &'a Value, key: &str, field: &str) -> Vec<&'a str> {
    report[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item[field].as_str().unwrap())
        .collect()
}

fn atomic_group_bindings(report: &Value) -> Vec<&str> {
    report["atomic_groups"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|atomic_group| atomic_group["members"].as_array().unwrap())
        .map(|member| member["binding"].as_str().unwrap())
        .collect()
}

#[test]
fn atoms_filters_residual_and_renamed_atomic_groups_and_groups_them_by_destination() {
    let fixture = patched_fixture();
    let cases: [(&[&str], &[&str]); 4] = [
        (&[], &["ZZ", "aa", "bb", "cc"]),
        (&["--residual-only"], &["ZZ", "aa", "cc"]),
        (&["--readable-only"], &["bb"]),
        (&["--residual-only", "--readable-only"], &[]),
    ];
    for (flags, expected) in cases {
        let args = [&["atoms"][..], flags].concat();
        let report = fixture.json(&args);
        assert_eq!(atomic_group_bindings(&report), expected, "{flags:?}");
        assert!(report.get("destination_groups").is_none(), "{flags:?}");
    }

    let report = fixture.json(&["atoms", "--by-destination"]);
    let groups: BTreeMap<&str, &Value> = report["destination_groups"]
        .as_array()
        .unwrap()
        .iter()
        .map(|group| {
            (
                group["destination"].as_str().unwrap(),
                &group["atomic_group_ids"],
            )
        })
        .collect();
    assert_eq!(groups.len(), 2);
    assert_eq!(groups["home/bb"], &json!(["atomic:2"]));
    assert!(
        groups
            .values()
            .any(|ids| **ids == json!(["atomic:0", "atomic:1", "atomic:3"])),
        "{groups:?}"
    );
}

#[test]
fn coverage_matches_proposals_to_patch_sets_only_on_request() {
    let fixture = patched_fixture();
    let row = |report: &Value, path: &str| {
        report["rows"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["path"] == path)
            .unwrap_or_else(|| panic!("no {path} row in {report}"))
            .clone()
    };

    let plain = fixture.json(&["coverage"]);
    let patch_row = row(&plain, "binding_patches");
    assert_eq!(patch_row["status"], "complete_atomic_groups");
    assert_eq!(patch_row["complete_atomic_group_ids"], json!(["atomic:0"]));
    assert!(patch_row.get("matching_proposal_ids").is_none());

    let proposed = fixture.json(&["coverage", "--include-proposals"]);
    assert_eq!(
        row(&proposed, "binding_patches")["matching_proposal_ids"],
        json!(["auto_partition_0000"])
    );
    assert_eq!(
        row(&proposed, "home/bb")["matching_proposal_ids"],
        json!([])
    );
}

#[test]
fn describe_joins_graph_and_spec_context_and_runs_the_proposer_only_on_request() {
    let fixture = patched_fixture();
    let report = fixture.json(&["describe", "ZZ"]);
    assert_eq!(report["source_unit_ids"], json!(["unit:0"]));
    assert_eq!(
        field_of_each(&report, "incoming_edges", "source"),
        ["unit:1", "unit:3"]
    );
    assert_eq!(
        field_of_each(&report, "neighbor_source_units", "id"),
        ["unit:1", "unit:3"]
    );
    assert_eq!(report["binding_homes"][0]["source_kind"], "binding_patch");
    assert_eq!(report["atomic_groups"][0]["id"], "atomic:0");
    assert!(report.get("module_proposals").is_none());
    assert!(report.get("module_proposal_diagnostics").is_none());

    let report = fixture.json(&["describe", "ZZ", "--include-proposals"]);
    assert_eq!(
        report["module_proposals"][0]["proposed_module_id"],
        "auto_partition_0000"
    );
    assert!(report["module_proposal_diagnostics"].is_array());
}

#[test]
fn describe_limit_truncates_each_section_and_reports_its_total() {
    let report = patched_fixture().json(&["describe", "ZZ", "--limit", "1"]);
    assert_eq!(report["limits"]["limit"], 1);
    let sections = &report["limits"]["sections"];
    for section in ["incoming_edges", "neighbor_source_units"] {
        assert_eq!(report[section].as_array().unwrap().len(), 1, "{section}");
        assert_eq!(
            sections[section],
            json!({"total": 2, "emitted": 1, "truncated": true}),
            "{section}"
        );
    }
    assert_eq!(
        sections["source_unit_ids"],
        json!({"total": 1, "emitted": 1, "truncated": false})
    );
}

#[test]
fn inspect_source_context_is_clamped_to_the_source_file() {
    let fixture = patched_fixture();
    for (id, context, indices) in [
        ("aa", "0", vec![1]),
        ("aa", "1", vec![0, 1, 2]),
        ("ZZ", "5", vec![0, 1, 2, 3]),
    ] {
        let report = fixture.json(&[
            "inspect-source",
            "--id",
            id,
            "--context-statements",
            context,
        ]);
        let actual = report["files"][0]["statements"]
            .as_array()
            .unwrap()
            .iter()
            .map(|statement| statement["index"].as_u64().unwrap() as usize)
            .collect::<Vec<_>>();
        assert_eq!(actual, indices, "{id} +{context}");
    }
}

#[test]
fn modules_propose_limit_keeps_a_prefix_and_reports_the_total() {
    let fixture = patched_fixture();
    // A one-line cap keeps the three residual owners in separate proposals.
    let args = ["modules", "propose", "--size-cap-lines", "1"];
    let all = fixture.json(&args);
    assert_eq!(all["proposals"].as_array().unwrap().len(), 3);
    assert!(all.get("limits").is_none());

    let mut limited_args = args.to_vec();
    limited_args.extend(["--limit", "1"]);
    let limited = fixture.json(&limited_args);
    assert_eq!(limited["proposals"], json!([all["proposals"][0].clone()]));
    assert_eq!(limited["proposals"][0]["landable_today"], true);
    assert_eq!(limited["limits"]["limit"], 1);
    assert_eq!(
        limited["limits"]["sections"]["proposals"],
        json!({"total": 3, "emitted": 1, "truncated": true})
    );
}
