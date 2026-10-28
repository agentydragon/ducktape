//! Module-quotient queries over graph artifacts from real JS/specs.

use debundle_e2e_support::GraphFixture;
use serde_json::Value;
use std::fs;

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

fn binding_module_yaml(binding: &str) -> String {
    let mut yaml = String::from("members: [{selector: {binding: {name: ");
    yaml.push_str(binding);
    yaml.push_str("}}}]\n");
    yaml
}

fn many_neighbor_fixture(count: usize) -> GraphFixture {
    let mut source = String::new();
    let mut modules = Vec::new();
    let dependencies = (0..count)
        .map(|index| {
            let binding = format!("dependency{index}");
            source.push_str(&format!("const {binding} = {index};\n"));
            modules.push((
                format!("incoming/neighbor_{index:02}.yaml"),
                binding_module_yaml(&binding),
            ));
            binding
        })
        .collect::<Vec<_>>();
    source.push_str(&format!("const anchor = {};\n", dependencies.join(" + ")));
    modules.push(("home.yaml".to_string(), binding_module_yaml("anchor")));
    for index in 0..count {
        let binding = format!("consumer{index}");
        source.push_str(&format!("const {binding} = anchor;\n"));
        modules.push((
            format!("outgoing/neighbor_{index:02}.yaml"),
            binding_module_yaml(&binding),
        ));
    }
    let module_refs = modules
        .iter()
        .map(|(path, yaml)| (path.as_str(), yaml.as_str()))
        .collect::<Vec<_>>();
    GraphFixture::new(&source, &module_refs)
}

#[test]
fn cluster_filters_before_per_direction_limits_and_reports_the_boundary() {
    let fixture = many_neighbor_fixture(24);
    let bounded = fixture.json(&["cluster", "anchor"]);
    assert_eq!(bounded["limits"]["limit"], 20);
    for (field, section) in [
        ("incoming_modules", "incoming_modules"),
        ("outgoing_modules", "outgoing_modules"),
    ] {
        assert_eq!(bounded[field].as_array().unwrap().len(), 20);
        assert_eq!(bounded["limits"]["sections"][section]["total"], 24);
        assert_eq!(bounded["limits"]["sections"][section]["emitted"], 20);
        assert_eq!(bounded["limits"]["sections"][section]["truncated"], true);
    }

    let limited = fixture.json(&["cluster", "anchor", "--limit", "3"]);
    assert_eq!(limited["limits"]["limit"], 3);
    assert_eq!(limited["incoming_modules"].as_array().unwrap().len(), 3);
    assert_eq!(
        limited["limits"]["sections"]["incoming_modules"]["total"],
        24
    );
    assert_eq!(
        limited["limits"]["sections"]["incoming_modules"]["emitted"],
        3
    );

    let incoming = fixture.json(&["cluster", "anchor", "--direction", "incoming"]);
    assert_eq!(incoming["incoming_modules"].as_array().unwrap().len(), 20);
    assert_eq!(
        incoming["limits"]["sections"]["incoming_modules"]["total"],
        24
    );
    assert_eq!(incoming["outgoing_modules"].as_array().unwrap().len(), 0);
    assert_eq!(
        incoming["limits"]["sections"]["outgoing_modules"]["total"],
        0
    );

    let reason_filtered = fixture.json(&["cluster", "anchor", "--all", "--reason", "eager_use"]);
    assert_eq!(reason_filtered["limits"]["limit"], 0);
    assert_eq!(
        reason_filtered["limits"]["sections"]["incoming_modules"]["total"],
        24
    );
    assert_eq!(
        reason_filtered["limits"]["sections"]["outgoing_modules"]["total"],
        24
    );

    let no_matches = fixture.json(&["cluster", "anchor", "--reason", "lazy_use"]);
    assert_eq!(
        no_matches["limits"]["sections"]["incoming_modules"]["total"],
        0
    );
    assert_eq!(
        no_matches["limits"]["sections"]["outgoing_modules"]["total"],
        0
    );

    let text = fixture.command(&["cluster", "anchor", "--limit", "1", "--format", "text"]);
    assert!(text.status.success());
    let text = String::from_utf8_lossy(&text.stdout);
    assert!(text.contains("binding=anchor home=home"), "{text}");
    assert!(
        text.contains("showing 1 of 24 rows in incoming_modules"),
        "{text}"
    );
    let ndjson = fixture.command(&["cluster", "anchor", "--limit", "1", "--format", "ndjson"]);
    assert!(ndjson.status.success());
    let ndjson: Value = serde_json::from_slice(&ndjson.stdout).unwrap();
    let json = fixture.json(&["cluster", "anchor", "--limit", "1"]);
    assert_eq!(ndjson, json);

    let zero = fixture.command(&["cluster", "anchor", "--limit", "0", "--format", "json"]);
    assert_eq!(zero.status.code(), Some(1));
    let zero: Value = serde_json::from_slice(&zero.stdout).unwrap();
    assert_eq!(zero["reason"], "query_failed");
    assert!(
        zero["message"]
            .as_str()
            .unwrap()
            .contains("--limit must be greater than zero")
    );

    assert_eq!(
        fixture.json(&["cluster", "anchor", "--all"])["incoming_modules"]
            .as_array()
            .unwrap()
            .len(),
        24
    );
}

#[test]
fn cluster_and_shared_queries_reject_unknown_typed_prefixes_before_graph_work() {
    let fixture = GraphFixture::new("const alpha = 1;\n", &[]);
    let missing_graph = fixture.root().join("not-present.json");
    let missing_graph = missing_graph.to_str().unwrap();

    let single = fixture.command(&[
        "describe",
        "module:7",
        "--graph",
        missing_graph,
        "--format",
        "json",
    ]);
    let batch_path = fixture.root().join("ids.json");
    fs::write(&batch_path, r#"["alpha", "module:7"]"#).unwrap();
    let batch_path = batch_path.to_str().unwrap();
    let batch = fixture.command(&[
        "describe",
        "--batch",
        batch_path,
        "--graph",
        missing_graph,
        "--format",
        "ndjson",
    ]);
    let inspect = fixture.command(&[
        "inspect-source",
        "--id",
        "module:7",
        "--graph",
        missing_graph,
        "--format",
        "json",
    ]);
    for (out, format) in [(single, "json"), (batch, "ndjson"), (inspect, "json")] {
        assert_eq!(out.status.code(), Some(1));
        let stdout = String::from_utf8(out.stdout).unwrap();
        let failure: Value = if format == "ndjson" {
            serde_json::from_str(stdout.lines().next().unwrap()).unwrap()
        } else {
            serde_json::from_str(&stdout).unwrap()
        };
        assert_eq!(failure["reason"], "unsupported_identifier_prefix");
        assert_eq!(failure["identifier"], "module:7");
        assert_eq!(failure["prefix"], "module");
        assert_eq!(failure["suggestion"], "logical:7");
        assert!(
            failure["accepted_prefixes"]
                .as_array()
                .unwrap()
                .contains(&Value::from("logical:"))
        );
        assert!(
            failure["examples"]
                .as_array()
                .unwrap()
                .contains(&Value::from("logical:7"))
        );
        assert!(String::from_utf8_lossy(&out.stderr).contains("logical:7"));
    }

    let zero_limit = fixture.command(&[
        "describe",
        "alpha",
        "--limit",
        "0",
        "--graph",
        missing_graph,
        "--format",
        "json",
    ]);
    assert_eq!(zero_limit.status.code(), Some(1));
    let zero_limit: Value = serde_json::from_slice(&zero_limit.stdout).unwrap();
    assert_eq!(zero_limit["reason"], "query_failed");
    assert!(
        zero_limit["message"]
            .as_str()
            .unwrap()
            .contains("--limit must be greater than zero")
    );

    let typed = fixture.command(&[
        "inspect-source",
        "--id",
        "unit:0",
        "--graph",
        missing_graph,
        "--format",
        "json",
    ]);
    assert_eq!(typed.status.code(), Some(1));
    let typed: Value = serde_json::from_slice(&typed.stdout).unwrap();
    assert_eq!(typed["reason"], "io");
    for id in [
        "logical:7",
        "atomic:3",
        "diagnostic:example",
        "extend:example",
        "auto_partition_0007",
        "alpha",
    ] {
        let out = fixture.command(&["describe", id, "--graph", missing_graph, "--format", "json"]);
        assert_eq!(out.status.code(), Some(1), "{id}");
        let failure: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(failure["reason"], "io", "{id}: {failure:#}");
    }

    let colon_path = GraphFixture::new(
        "const alpha = 1;\n",
        &[(
            "module:7.yaml",
            "members: [{selector: {binding: {name: alpha}}}]\n",
        )],
    );
    let described = colon_path.json(&["describe", "module:7"]);
    assert_eq!(described["query"]["kind"], "module");
    assert_eq!(described["query"]["value"], "module:7");
    let inspected = colon_path.json(&["inspect-source", "--id", "module:7"]);
    assert_eq!(inspected["selected_source_unit_count"], 1);
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
        let failure: Value = serde_json::from_slice(&out.stdout)
            .expect("one structured failure without a partial query report");
        assert_eq!(failure["reason"], "query_failed");
        assert_eq!(failure["message"].as_str().unwrap(), stderr.trim());
    }
}
