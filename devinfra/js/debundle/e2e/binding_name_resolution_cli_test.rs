//! Minified/readable names through real graph production and query commands.

use debundle_e2e_support::GraphFixture;
use peel::resolve_binding_source_units;
use serde_json::json;

#[test]
fn describe_and_inspect_source_accept_both_name_forms() {
    let fixture = GraphFixture::new(
        "const a = class Example {};\nconst b = a;\n",
        &[(
            "values/example.yaml",
            "members: [{name: Example, selector: {binding: {name: a}}}]\n",
        )],
    );
    for name in ["a", "Example"] {
        let report = fixture.json(&["describe", name]);
        assert_eq!(report["source_unit_ids"], json!(["unit:0"]));
        let report = fixture.json(&["inspect-source", "--id", name, "--view", "raw"]);
        let statements = report["files"][0]["statements"].as_array().unwrap();
        assert_eq!(statements.len(), 1);
        assert!(
            statements[0]["source"]
                .as_str()
                .unwrap()
                .contains("class Example")
        );
    }
    let graph = fixture.dependency_graph();
    for name in ["a", "Example"] {
        let source_units = resolve_binding_source_units(&graph, name);
        assert_eq!(source_units.len(), 1);
        assert_eq!(source_units[0].id, "unit:0");
    }
}

#[test]
fn minified_match_precedes_readable_collision_without_hiding_it() {
    // The readable collision is in an earlier source unit, so source ordering alone
    // cannot satisfy the name resolver's minified-first priority contract.
    let fixture = GraphFixture::new(
        "const a = 2;\nconst b = 1;\n",
        &[
            (
                "first.yaml",
                "members: [{name: b, selector: {binding: {name: a}}}]\n",
            ),
            (
                "second.yaml",
                "members: [{selector: {binding: {name: b}}}]\n",
            ),
        ],
    );
    let graph = fixture.dependency_graph();
    let source_units = resolve_binding_source_units(&graph, "b");
    assert_eq!(
        source_units
            .iter()
            .map(|o| o.id.as_str())
            .collect::<Vec<_>>(),
        ["unit:1", "unit:0"]
    );
    let report = fixture.json(&["describe", "b"]);
    assert_eq!(report["source_unit_ids"].as_array().unwrap().len(), 2);
}
