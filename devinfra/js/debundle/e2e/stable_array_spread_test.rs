//! A local primitive array spread can preserve narrow module boundaries.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

// Keep the observable calls after all three modules initialize. This fixture
// tests spread ownership, independently of host reentry during initialization.
const SOURCE: &str = r#"const clientBefore = "before";
const first = ["a"], second = ["b"], scopes = ["base", ...first, ...second];
const apiError = scopes;
const clientAfter = apiError;
console.log(clientBefore);
console.log(scopes.join(","), clientAfter.join(","));
export { scopes, apiError, clientAfter };
"#;

const LITERAL_CONTROL: &str = r#"const clientBefore = "before";
const first = ["a"], second = ["b"], scopes = ["base", "a", "b"];
const apiError = scopes;
const clientAfter = apiError;
console.log(clientBefore);
console.log(scopes.join(","), clientAfter.join(","));
export { scopes, apiError, clientAfter };
"#;

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "client",
            &[Member::new("clientBefore"), Member::new("clientAfter")],
        ),
        logical_module("error/schema", &[Member::new("apiError")]),
        logical_module(
            "oauth/scopes",
            &[
                Member::new("first"),
                Member::new("second"),
                Member::new("scopes"),
            ],
        ),
    ]
}

#[test]
fn same_declaration_primitive_array_spreads_keep_three_modules_realizable() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(SOURCE, modules()));
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/oauth/scopes.js",
        &["scopes"],
        &[],
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["edges"]
            .as_array()
            .unwrap()
            .iter()
            .any(|edge| { edge["edge_kind"] == "co_location" }),
        "certified spread must retain a distinct producer co-location edge"
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn literal_spread_control_keeps_three_modules_realizable() {
    let fixture = run_fixture(FixtureOpts::new(LITERAL_CONTROL, modules()));
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
}

#[test]
fn spread_producers_cannot_be_materialized_in_another_module() {
    let modules = vec![
        logical_module(
            "client",
            &[Member::new("clientBefore"), Member::new("clientAfter")],
        ),
        logical_module("error/schema", &[Member::new("apiError")]),
        logical_module("oauth/first", &[Member::new("first")]),
        logical_module(
            "oauth/scopes",
            &[Member::new("second"), Member::new("scopes")],
        ),
    ];
    let rejected = run_rejection_fixture_with_graphs(FixtureOpts::new(SOURCE, modules));
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            conflict["claims"].as_array().is_some_and(|claims| {
                ["oauth/first", "oauth/scopes"]
                    .iter()
                    .all(|module| claims.iter().any(|claim| claim["module"] == *module))
            })
        }),
        "producer/consumer split must be rejected: {conflicts:#?}"
    );
}
