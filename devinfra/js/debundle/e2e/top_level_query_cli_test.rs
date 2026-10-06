//! Query the artifacts emitted from real JS/spec inputs, through the CLI.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn fixture() -> GraphFixture {
    GraphFixture::new("const ZZ = class PaymentError {};\nconst aa = ZZ;\n", &[])
}

#[test]
fn atoms_summary_and_proposals_describe_the_real_graph() {
    let fixture = fixture();
    let atoms = fixture.json(&["atoms"]);
    assert_eq!(atoms["units"].as_array().unwrap().len(), 2);
    let summary = fixture.json(&["graph-summary"]);
    assert_eq!(summary["owner_count"], 2);
    assert_eq!(summary["atomic_unit_count"], 2);
    assert!(summary.get("proposal_count").is_none());
    assert!(summary.get("diagnostic_count").is_none());
    let proposals = fixture.json(&["modules", "propose", "--size-cap-lines", "10000"]);
    assert!(!proposals["proposals"].as_array().unwrap().is_empty());
}

#[test]
fn describe_and_show_source_dispatch_bindings_and_module_ids() {
    let fixture = fixture();
    for id in ["ZZ", "owner:0", "atomic:0"] {
        let report = fixture.json(&["describe", id]);
        assert_eq!(report["owner_ids"], json!(["owner:0"]), "{id}");
        assert_eq!(report["atomic_units"][0]["id"], "atomic:0");
    }
    let graph = fixture.owner_graph();
    let destination = &graph.nodes[0].destination;
    let report = fixture.json(&["describe", destination.as_str()]);
    assert_eq!(report["owner_ids"], json!(["owner:0", "owner:1"]));
    let report = fixture.json(&["show-source", "ZZ", "--context-lines", "1"]);
    assert_eq!(report["slices"].as_array().unwrap().len(), 1);
    assert!(
        report["slices"][0]["text"]
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
    assert_eq!(row["status"], "complete_units");
    assert_eq!(row["complete_unit_ids"], json!(["atomic:0"]));
    assert_eq!(row["missing_anonymous_owner_ids"], json!([]));
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
    assert_eq!(row["status"], "complete_units");
    assert_eq!(row["requested_binding_ids"], json!(["alpha", "beta"]));
    assert_eq!(row["complete_unit_ids"], json!(["atomic:0"]));
    assert_eq!(row["missing_binding_ids"], json!([]));
    assert_eq!(row["missing_owner_ids"], json!([]));
    let describe = fixture.json(&["describe", "features/state"]);
    assert_eq!(describe["owner_ids"], json!(["owner:0", "owner:1"]));
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
        assert_eq!(report["owner_ids"], json!(["owner:0"]));
        assert_eq!(
            report["atomic_units"][0]["anonymous_statement_owner_ids"],
            json!(["owner:0"])
        );
    }
    let report = fixture.json(&[
        "show-source",
        "auto_partition/auto_partition_0187",
        "--context-lines",
        "0",
    ]);
    assert_eq!(report["slices"].as_array().unwrap().len(), 1);
    assert!(
        report["slices"][0]["text"]
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
    // Whichever partition the heuristic chooses, neither ambiguous owner is
    // addressable by a unique selector, and no containing proposal is landable.
    let mut unaddressable: Vec<_> = proposals
        .iter()
        .flat_map(|p| {
            p["unaddressable_anonymous_owner_ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|id| id.as_str().unwrap())
        })
        .collect();
    unaddressable.sort_unstable();
    assert_eq!(unaddressable, ["owner:0", "owner:1"]);
    assert!(proposals.iter().all(|p| p["landable_today"] == false));
}

#[test]
fn stale_proposal_and_diagnostic_ids_report_their_kind_and_recovery_command() {
    let fixture = fixture();
    for (id, kind) in [
        ("auto_partition_0499", "proposal"),
        ("diagnostic:size_cap_0001", "diagnostic"),
    ] {
        let out = fixture.command(&["show-source", id]);
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
/// claimed (and renamed) by a module. The residual units are `ZZ`, `aa`, `cc`.
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

fn unit_bindings(report: &Value) -> Vec<&str> {
    report["units"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|unit| unit["members"].as_array().unwrap())
        .map(|member| member["binding"].as_str().unwrap())
        .collect()
}

#[test]
fn atoms_filters_residual_and_renamed_units_and_groups_them_by_destination() {
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
        assert_eq!(unit_bindings(&report), expected, "{flags:?}");
        assert!(report.get("groups").is_none(), "{flags:?}");
    }

    let report = fixture.json(&["atoms", "--by-destination"]);
    let groups: BTreeMap<&str, &Value> = report["groups"]
        .as_array()
        .unwrap()
        .iter()
        .map(|group| (group["destination"].as_str().unwrap(), &group["unit_ids"]))
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
    assert_eq!(patch_row["status"], "complete_units");
    assert_eq!(patch_row["complete_unit_ids"], json!(["atomic:0"]));
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
    assert_eq!(report["owner_ids"], json!(["owner:0"]));
    assert_eq!(
        field_of_each(&report, "incoming_edges", "source"),
        ["owner:1", "owner:3"]
    );
    assert_eq!(
        field_of_each(&report, "neighbor_owners", "id"),
        ["owner:1", "owner:3"]
    );
    assert_eq!(report["binding_homes"][0]["source_kind"], "binding_patch");
    assert_eq!(report["atomic_units"][0]["id"], "atomic:0");
    assert!(report.get("factorize_proposals").is_none());
    assert!(report.get("factorize_diagnostics").is_none());

    let report = fixture.json(&["describe", "ZZ", "--include-proposals"]);
    assert_eq!(
        report["factorize_proposals"][0]["proposed_module_id"],
        "auto_partition_0000"
    );
    assert!(report["factorize_diagnostics"].is_array());
}

#[test]
fn describe_limit_truncates_each_section_and_reports_its_total() {
    let report = patched_fixture().json(&["describe", "ZZ", "--limit", "1"]);
    assert_eq!(report["limits"]["limit"], 1);
    let sections = &report["limits"]["sections"];
    for section in ["incoming_edges", "neighbor_owners"] {
        assert_eq!(report[section].as_array().unwrap().len(), 1, "{section}");
        assert_eq!(
            sections[section],
            json!({"total": 2, "emitted": 1, "truncated": true}),
            "{section}"
        );
    }
    assert_eq!(
        sections["owner_ids"],
        json!({"total": 1, "emitted": 1, "truncated": false})
    );
}

#[test]
fn show_source_context_is_clamped_to_the_source_file() {
    let fixture = patched_fixture();
    for (id, context, start, end) in [("aa", "0", 2, 2), ("aa", "1", 1, 3), ("ZZ", "5", 1, 4)] {
        let report = fixture.json(&["show-source", id, "--context-lines", context]);
        let slice = &report["slices"][0];
        assert_eq!(slice["context_start_line"], start, "{id} +{context}");
        assert_eq!(slice["context_end_line"], end, "{id} +{context}");
        assert_eq!(
            slice["text"],
            PATCHED_SOURCE
                .lines()
                .skip(start - 1)
                .take(end - start + 1)
                .collect::<Vec<_>>()
                .join("\n"),
            "{id} +{context}"
        );
    }
}

#[test]
fn modules_propose_limit_keeps_a_prefix_and_reports_the_total() {
    let fixture = patched_fixture();
    // A one-line cap keeps the three residual units as separate proposals.
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
