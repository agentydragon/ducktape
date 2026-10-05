//! Query the artifacts emitted from real JS/spec inputs, through the CLI.

use debundle_e2e_support::GraphFixture;
use serde_json::json;

fn fixture() -> GraphFixture {
    GraphFixture::new("const ZZ = class PaymentError {};\nconst aa = ZZ;\n", &[])
}

#[test]
fn atoms_coverage_summary_and_proposals_describe_the_real_graph() {
    let fixture = fixture();
    let atoms = fixture.json(&["atoms"]);
    assert_eq!(atoms["units"].as_array().unwrap().len(), 2);
    let coverage = fixture.json(&["coverage"]);
    assert_eq!(
        coverage["summary"]["total_patch_sets"],
        coverage["rows"].as_array().unwrap().len()
    );
    assert!(
        coverage["rows"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row.get("matching_proposal_ids").is_none())
    );
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
fn source_match_claims_combine_with_members_in_either_yaml_order() {
    let members = "members: [{selector: {binding: {name: beta}}}]\n";
    let matches =
        "source_matches: [{match: 'let alpha = 1;', bindings: [{local: alpha, name: Alpha}]}]\n";
    for yaml in [format!("{members}{matches}"), format!("{matches}{members}")] {
        let fixture = GraphFixture::new(
            "let alpha = 1;\nfunction beta() { alpha = 2; }\n",
            &[("features/state.yaml", &yaml)],
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
        assert!(
            stderr.contains(&format!("{kind} id {id:?} not found")),
            "{stderr}"
        );
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
