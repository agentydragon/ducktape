//! Minified/readable names through real graph production and query commands.

use debundle_e2e_support::GraphFixture;
use peel::resolve_binding_owners;
use serde_json::json;

#[test]
fn describe_and_inspect_source_accept_both_name_forms() {
    let fixture = GraphFixture::new(
        "const XOe = class PluginSettingsAccessor {};\nconst YOe = XOe;\n",
        &[(
            "ui/plugins.yaml",
            "members: [{name: PluginSettingsAccessor, selector: {binding: {name: XOe}}}]\n",
        )],
    );
    for name in ["XOe", "PluginSettingsAccessor"] {
        let report = fixture.json(&["describe", name]);
        assert_eq!(report["owner_ids"], json!(["owner:0"]));
        let report = fixture.json(&["inspect-source", "--id", name, "--view", "raw"]);
        let statements = report["files"][0]["statements"].as_array().unwrap();
        assert_eq!(statements.len(), 1);
        assert!(
            statements[0]["source"]
                .as_str()
                .unwrap()
                .contains("class PluginSettingsAccessor")
        );
    }
    let graph = fixture.owner_graph();
    for name in ["XOe", "PluginSettingsAccessor"] {
        let owners = resolve_binding_owners(&graph, name);
        assert_eq!(owners.len(), 1);
        assert_eq!(owners[0].id, "owner:0");
    }
}

#[test]
fn minified_match_precedes_readable_collision_without_hiding_it() {
    // The readable collision is in an earlier owner, so source ordering alone
    // cannot satisfy the name resolver's minified-first priority contract.
    let fixture = GraphFixture::new(
        "const ZZZ = 2;\nconst Collide = 1;\n",
        &[
            (
                "a.yaml",
                "members: [{name: Collide, selector: {binding: {name: ZZZ}}}]\n",
            ),
            (
                "b.yaml",
                "members: [{selector: {binding: {name: Collide}}}]\n",
            ),
        ],
    );
    let graph = fixture.owner_graph();
    let owners = resolve_binding_owners(&graph, "Collide");
    assert_eq!(
        owners.iter().map(|o| o.id.as_str()).collect::<Vec<_>>(),
        ["owner:1", "owner:0"]
    );
    let report = fixture.json(&["describe", "Collide"]);
    assert_eq!(report["owner_ids"].as_array().unwrap().len(), 2);
}
