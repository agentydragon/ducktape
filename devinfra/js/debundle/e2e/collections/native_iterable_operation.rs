use debundle_e2e_graph_support::{
    FixtureGraphExt, GraphFixture, json_node_declaring, source_unit_for_binding,
};
use debundle_e2e_support::*;

#[test]
fn same_declaration_primitive_set_spread_preserves_entry_and_exports() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"console.log("before");
var seed = new Set(["a", "b"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);
export {};
"#,
        vec![logical_module(
            "copied",
            &binding_members(["seed", "sibling", "copied"]),
        )],
    ));
    assert_preserved_entry_output(&fixture, "before\nafter 3\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = json_node_declaring(&graph, "copied");
    if let Some(output) = std::env::var_os("TEST_UNDECLARED_OUTPUTS_DIR") {
        std::fs::write(
            std::path::PathBuf::from(output).join("primitive_set_copied.json"),
            serde_json::to_vec_pretty(copied).unwrap(),
        )
        .unwrap();
    }
    assert_eq!(copied["synchronous_execution"]["complete"], true);
    assert!(
        !copied["synchronous_execution"]["precision_losses"]
            .as_array()
            .is_some_and(|losses| !losses.is_empty()),
        "{copied}"
    );
    assert!(
        copied["synchronous_execution"]["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|proof| proof["conclusion"] == "primitive_set_iteration"),
        "{copied}"
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/copied.js",
        &["copied"],
        &[],
    );
}

#[test]
fn inferred_set_spread_interval_is_a_canonical_placement_constraint() {
    let rejected = run_rejection_fixture_with_graphs(FixtureOpts::new(
        r#"var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log(copied.size);
"#,
        vec![
            logical_module("source", &binding_members(["seed", "sibling"])),
            logical_module("copy", &[Member::new("copied")]),
        ],
    ));
    assert!(!rejected.atomic_group_conflicts().is_empty());
}

#[test]
fn replaced_set_iterator_keeps_its_callback_effects() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"console.log("before");
Set.prototype[Symbol.iterator] = function () { console.log("iterate"); return ["replacement"][Symbol.iterator](); };
var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);
"#,
        vec![],
    ).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "before\niterate\nafter 2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = json_node_declaring(&graph, "copied");
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn set_spread_reads_the_source_at_its_initialization_prefix() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
const updated = seed.add("late");
console.log([...copied].join(","), [...seed].join(","));
"#,
        vec![
            logical_module(
                "collections",
                &binding_members(["seed", "sibling", "copied"]),
            ),
            logical_module("update", &[Member::new("updated")]),
            logical_module_with_anon(
                "observe",
                &[],
                &["console.log([...copied].join(\",\"), [...seed].join(\",\"));"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "a,c a,late\n");
    let graph_report = fixture.dependency_graph();
    let copied = source_unit_for_binding(&graph_report, "copied").to_owned();
    let updated = source_unit_for_binding(&graph_report, "updated").to_owned();
    let graph = serde_json::to_value(graph_report).unwrap();
    assert!(
        graph["edges"].as_array().unwrap().iter().any(|edge| {
            edge["source"] == updated
                && edge["target"] == copied
                && edge["edge_kind"] == "sequenced"
        }),
        "the copy's SetContents read must stay before the later source write: {graph}"
    );
}

#[test]
fn accessor_iterable_keeps_the_unknown_protocol_fallback() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"const source = {
    get [Symbol.iterator]() {
        console.log("iterator getter");
        return function* () { yield "value"; };
    },
};
const copied = new Set(source);
console.log([...copied].join(","));
"#,
            vec![],
        )
        .with_import_order(&["residual"]),
    );
    assert_preserved_entry_output(&fixture, "iterator getter\nvalue\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = json_node_declaring(&graph, "copied");
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn side_effecting_spread_source_runs_once_and_keeps_the_protocol_fallback() {
    // Node proves emitted observable behavior. Incomplete analysis below is
    // not a claim that the flattened interpreter projected the operand once.
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"let calls = 0;
function source() { calls++; console.log("source", calls); return new Set(["a"]); }
const copied = new Set([...source(), "b"]);
console.log("after", calls, copied.size);
"#,
            vec![],
        )
        .with_import_order(&["residual"]),
    );
    assert_preserved_entry_output(&fixture, "source 1\nafter 1 2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = json_node_declaring(&graph, "copied");
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn graph_backed_refinement_cannot_split_the_proved_set_interval() {
    let fixture = GraphFixture::new(
        "var seed = new Set(['a']), sibling = new Set(['x']), copied = new Set([...seed, 'c']); console.log(copied.size);",
        &[(
            "sets.yaml",
            "members: [{selector: {binding: {name: seed}}}, {selector: {binding: {name: sibling}}}, {selector: {binding: {name: copied}}}]\n",
        )],
    );
    let graph = fixture.dependency_graph();
    let copied = source_unit_for_binding(&graph, "copied");
    let batch = fixture.root().join("refine.json");
    std::fs::write(
        &batch,
        serde_json::to_vec(&serde_json::json!([{ "unit_id": copied, "module": "copy" }])).unwrap(),
    )
    .unwrap();
    let order = fixture.write_final_import_order(&["copy", "sets"]);
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--assignments",
            batch.to_str().unwrap(),
            "--import-order",
            order.to_str().unwrap(),
        ],
        &["atomic"],
    );
}
