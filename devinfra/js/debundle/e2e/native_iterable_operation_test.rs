use debundle_e2e_graph_support::{FixtureGraphExt, GraphFixture, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn same_declaration_primitive_set_spread_preserves_entry_and_exports() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("before");
var seed = new Set(["a", "b"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);
export {};
"#,
        vec![logical_module(
            "copied",
            &[
                Member::new("seed"),
                Member::new("sibling"),
                Member::new("copied"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "before\nafter 3\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == "copied")
        })
        .unwrap();
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
fn duplicate_primitives_keep_native_deduplication() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("before");
var seed = new Set(["a", "a", "b"]), sibling = new Set(["x", "x"]), copied = new Set([...seed, "b", "c", "c"]);
console.log("after", copied.size, [...copied].join(","));
"#,
        vec![logical_module(
            "copied",
            &[
                Member::new("seed"),
                Member::new("sibling"),
                Member::new("copied"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "before\nafter 3 a,b,c\n");
}

#[test]
fn inferred_set_spread_interval_is_a_canonical_placement_constraint() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        r#"var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log(copied.size);
"#,
        vec![
            logical_module("source", &[Member::new("seed"), Member::new("sibling")]),
            logical_module("copy", &[Member::new("copied")]),
        ],
    ));
    assert!(!rejected.atomic_group_conflicts().is_empty());
}

#[test]
fn replaced_set_iterator_keeps_its_callback_effects() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("before");
Set.prototype[Symbol.iterator] = function () { console.log("iterate"); return ["replacement"][Symbol.iterator](); };
var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "before\niterate\nafter 2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == "copied")
        })
        .unwrap();
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn set_spread_reads_the_source_at_its_initialization_prefix() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var seed = new Set(["a"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
const updated = seed.add("late");
console.log([...copied].join(","), [...seed].join(","));
"#,
        vec![
            logical_module(
                "collections",
                &[
                    Member::new("seed"),
                    Member::new("sibling"),
                    Member::new("copied"),
                ],
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
    let fixture = run_fixture(FixtureOpts::new(
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
    ));
    assert_preserved_entry_output(&fixture, "iterator getter\nvalue\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == "copied")
        })
        .unwrap();
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn argument_throw_stops_evaluation_after_the_observed_prefix() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const seed = new Set(["a"]);
try {
    new Set([...seed, (() => { console.log("tail"); throw new Error("stop"); })(), console.log("never")]);
} catch (error) { console.log(error.message); }
console.log("after");
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "tail\nstop\nafter\n");
}

#[test]
fn side_effecting_spread_source_runs_once_and_keeps_the_protocol_fallback() {
    // Node proves emitted observable behavior. Incomplete analysis below is
    // not a claim that the flattened interpreter projected the operand once.
    let fixture = run_fixture(FixtureOpts::new(
        r#"let calls = 0;
function source() { calls++; console.log("source", calls); return new Set(["a"]); }
const copied = new Set([...source(), "b"]);
console.log("after", calls, copied.size);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "source 1\nafter 1 2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let copied = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == "copied")
        })
        .unwrap();
    assert_eq!(copied["synchronous_execution"]["complete"], false);
}

#[test]
fn replaced_set_adder_preserves_abrupt_completion() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"Set.prototype.add = function (value) { console.log("add", value); throw new Error("stop"); };
try { new Set(["first", "second"]); } catch (error) { console.log(error.message); }
console.log("after");
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "add first\nstop\nafter\n");
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
    let copied = fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding.as_ref() == "copied")
        })
        .unwrap()
        .id;
    let batch = fixture.root().join("refine.json");
    std::fs::write(
        &batch,
        serde_json::to_vec(&serde_json::json!([{ "unit_id": copied, "module": "copy" }])).unwrap(),
    )
    .unwrap();
    fixture.assert_rejected_unchanged(
        &["modules", "refine", "--batch", batch.to_str().unwrap()],
        &["atomic"],
    );
}
