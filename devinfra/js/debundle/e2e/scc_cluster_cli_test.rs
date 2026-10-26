//! Module-quotient queries over graph artifacts from real JS/specs.

use debundle_e2e_support::GraphFixture;

fn fixture() -> GraphFixture {
    GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconst isolated = 7;\n",
        &[
            (
                "ui/plugins.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}]",
            ),
            (
                "middle.yaml",
                "members: [{selector: {binding: {name: middle}}}]",
            ),
            (
                "isolated.yaml",
                "members: [{selector: {binding: {name: isolated}}}]",
            ),
        ],
    )
}

#[test]
fn scc_listing_and_filters_agree_on_cycles_and_singletons() {
    let fixture = fixture();
    let all = fixture.json(&["scc"]);
    let cycles = fixture.json(&["scc", "--cycles-only"]);
    let cycle = &cycles["sccs"][0];
    assert_eq!(cycles["sccs"].as_array().unwrap().len(), 1);
    assert_eq!(cycle["is_cycle"], true);
    assert_eq!(cycle["realizable"], false);
    let labels = cycle["labels"].as_array().unwrap();
    assert_eq!(labels.len(), 2);
    assert!(labels.iter().any(|l| l == "ui/plugins"));
    assert!(labels.iter().any(|l| l == "middle"));
    assert!(all["sccs"].as_array().unwrap().contains(cycle));
    let singletons = fixture.json(&["scc", "--singletons-only"]);
    assert!(
        singletons["sccs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["labels"][0] == "isolated")
    );
    assert_eq!(
        all["sccs"].as_array().unwrap().len(),
        singletons["sccs"].as_array().unwrap().len() + 1
    );
    assert_eq!(fixture.json(&["scc", "--binding", "first"]), cycles);
    let isolated = fixture.json(&["scc", "--binding", "isolated"]);
    assert_eq!(isolated["sccs"].as_array().unwrap().len(), 1);
    assert_eq!(isolated["sccs"][0]["labels"][0], "isolated");
    assert_eq!(isolated["sccs"][0]["is_cycle"], false);
    assert_eq!(isolated["sccs"][0]["realizable"], true);
}

#[test]
fn cluster_reports_neighbors_and_accepts_the_binding_flag_alias() {
    let fixture = fixture();
    let report = fixture.json(&["cluster", "first"]);
    assert_eq!(fixture.json(&["cluster", "--binding", "first"]), report);
    assert_eq!(report["home_module"]["label"], "ui/plugins");
    assert!(!report["home_module"]["id"].as_str().unwrap().is_empty());
    for direction in ["incoming_modules", "outgoing_modules"] {
        assert!(
            report[direction]
                .as_array()
                .unwrap()
                .iter()
                .any(|m| m["label"] == "middle")
        );
    }
}

#[test]
fn source_unit_queries_refuse_minified_readable_name_ambiguity() {
    let fixture = GraphFixture::new(
        "const a = 1; const b = 2;",
        &[
            (
                "first.yaml",
                "members: [{name: b, selector: {binding: {name: a}}}]",
            ),
            (
                "second.yaml",
                "members: [{name: c, selector: {binding: {name: b}}}]",
            ),
        ],
    );
    for command in ["scc", "cluster"] {
        let out = fixture.command(&[command, "--binding", "b", "--format", "json"]);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(!out.status.success());
        assert!(
            stderr.contains("ambiguous") && stderr.contains("unit:0") && stderr.contains("unit:1"),
            "{stderr}"
        );
        assert!(
            out.stdout.is_empty(),
            "failure must not emit a partial report"
        );
    }
}
