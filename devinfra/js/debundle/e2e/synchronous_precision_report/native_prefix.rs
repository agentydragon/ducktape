//! Native-write prefix observations and mutation witnesses explain cross-chunk dependencies.

use super::{execution, execution_from_graph};
use debundle_e2e_graph_support::{FixtureGraphExt, GraphFixture};
use debundle_e2e_support::*;
use serde_json::Value;

#[test]
fn property_absence_failure_reports_the_cross_chunk_write() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "import './mutation.js';\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });\nconsole.log(result.value);",
        vec![],
    ).with_import_order(&["residual"]).with_extra_chunks(&[("static/mutation", "export function mutate() { const alias = globalThis.slot; alias.value = 1; }")]));
    assert_entry_output(&fixture, "3\n");
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let witnesses = program_graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses");
    let witness = witnesses
        .iter()
        .find(|witness| witness["receiver_roots"] == serde_json::json!(["unknown_global_value"]))
        .expect("unknown global receiver witness");
    assert_eq!(witness["chunk_id"], "static/mutation");
    assert_eq!(witness["statement_ordinal"], 0);
    assert_eq!(
        witness["source_location"]["source_path"],
        "static/mutation.js"
    );
    assert_eq!(witness["source_location"]["start_line"], 1);
    assert_eq!(witness["operation"], "property_assignment");
    assert_eq!(witness["through_local_alias"], true);
    assert_eq!(
        witness["affected_roots"],
        serde_json::json!(["global_object"])
    );
    let result = execution(&fixture, "result");
    assert!(
        result["native_prototype_property_checks"]
            .as_array()
            .expect("evaluated property guards")
            .iter()
            .any(|check| check["property"] == "value"
                && check["intact"] == false
                && check["blocking_mutation_count"] == 1),
        "{result}"
    );
}

#[test]
fn unrelated_object_write_does_not_supply_native_mutation_evidence() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import './mutation.js';\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });\nconsole.log(result.value);",
            vec![],
        ).with_import_order(&["residual"])
        .with_extra_chunks(&[("static/mutation", "const object = {}; export function mutate() { object.flag = 1; }")]),
    );
    assert_entry_output(&fixture, "3\n");
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        program_graph.get("native_integrity_witnesses").is_none(),
        "{program_graph}"
    );
    assert!(
        execution(&fixture, "result")
            .get("native_integrity_checks")
            .is_none()
    );
}

fn array_prefix_fixture_with_graphs(write: &str) -> Fixture {
    let source = format!(
        "function mutate() {{ Array.flag = 1; }}\n\
         {write}\n\
         const arrayResult = [1, 2].map(value => value + 1);\n\
         console.log(arrayResult.join(','));"
    );
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "2,3\n");
    fixture
}

fn prototype_prefix_fixture_with_graphs(write: &str) -> Fixture {
    let source = format!(
        "function mutate() {{ Object.prototype.prefixProbe = 1; }}\n\
         {write}\n\
         class Box {{ constructor(config) {{ this.prefixProbe = config.prefixProbe; }} }}\n\
         const result = new Box({{ prefixProbe: 3 }});\n\
         console.log(result.prefixProbe);"
    );
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "3\n");
    fixture
}

fn prefix_observation(execution: &Value, guard: &str, property: Option<&str>) -> Value {
    execution["native_prefix_observations"]
        .as_array()
        .expect("evaluated native prefix observations")
        .iter()
        .find(|observation| {
            observation["guard"] == guard
                && property.map_or(observation.get("property").is_none(), |property| {
                    observation["property"] == property
                })
        })
        .unwrap_or_else(|| panic!("missing {guard} {property:?} in {execution}"))
        .clone()
}

fn assert_completed_native_integrity_inference(execution: &Value) {
    assert_eq!(execution["complete"], true, "{execution}");
    assert_eq!(
        execution["precision_losses"],
        serde_json::json!([]),
        "{execution}"
    );
    assert!(
        execution["inference_uses"].as_array().is_some_and(|uses| {
            uses.iter().any(|use_| {
                use_["conclusion"] == "native_integrity"
                    && use_["derivation_rules"]
                        .as_array()
                        .is_some_and(|rules| rules.contains(&serde_json::json!("native_integrity")))
            })
        }),
        "missing completed native-integrity inference in {execution}"
    );
}

#[test]
fn unused_deferred_native_writer_is_absent_from_prefix_observations() {
    let fixture = array_prefix_fixture_with_graphs("");
    let array = execution(&fixture, "arrayResult");
    let observed = prefix_observation(&array, "array_intrinsics", None);
    assert_eq!(
        observed["prior_affected_roots"],
        serde_json::json!([]),
        "{array}"
    );
    assert_eq!(observed["uncertain_execution"], false);
    // The callback's now-supported `value + 1` body completes with an
    // explicit native-integrity inference. That whole-call evidence is
    // separate from the runtime prefix observation above.
    assert_completed_native_integrity_inference(&array);
    let fixture = prototype_prefix_fixture_with_graphs("");
    let result = execution(&fixture, "result");
    let observed = prefix_observation(&result, "ordinary_object_prototype", Some("prefixProbe"));
    assert_eq!(
        observed["prior_blocking_prototype_mutations"], 0,
        "{result}"
    );
    assert!(
        result["native_prototype_property_checks"]
            .as_array()
            .is_some()
    );
}

#[test]
fn foreign_dynamic_scope_starts_active_despite_a_local_deferred_write() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import './mutation.js';\nfunction local() { globalThis.slot.value = 1; }\nclass Box { constructor(config) { this.prefixProbe = config.prefixProbe; } }\nconst result = new Box({ prefixProbe: 3 });\nconsole.log(result.prefixProbe);",
            vec![],
        ).with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/mutation",
            "export function uncertain() { eval('0'); }",
        )]),
    );
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    let observed = prefix_observation(&result, "ordinary_object_prototype", Some("prefixProbe"));
    assert!(
        observed["prior_affected_roots"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("global_object")),
        "{result}"
    );
}

#[test]
fn invoked_and_direct_native_writes_are_present_before_later_guards() {
    for write in ["mutate();", "Array.flag = 1;"] {
        let fixture = array_prefix_fixture_with_graphs(write);
        let array = execution(&fixture, "arrayResult");
        let observed = prefix_observation(&array, "array_intrinsics", None);
        assert!(
            observed["prior_affected_roots"]
                .as_array()
                .unwrap()
                .contains(&serde_json::json!("array")),
            "{write}: {array}"
        );
    }
    for write in ["mutate();", "Object.prototype.prefixProbe = 1;"] {
        let fixture = prototype_prefix_fixture_with_graphs(write);
        let result = execution(&fixture, "result");
        let observed =
            prefix_observation(&result, "ordinary_object_prototype", Some("prefixProbe"));
        assert!(
            observed["prior_blocking_prototype_mutations"]
                .as_u64()
                .unwrap()
                >= 1,
            "{write}: {result}"
        );
    }
}

#[test]
fn array_guard_references_a_direct_native_write_without_unknown_alias_widening() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import './mutation.js';\nconst result = [1, 2].map(value => value + 1);\nconsole.log(result.join(','));",
            vec![],
        ).with_import_order(&["residual"])
        .with_extra_chunks(&[("static/mutation", "export function mutate() { Array.flag = 1; }")]),
    );
    assert_entry_output(&fixture, "2,3\n");
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let witnesses = program_graph["native_integrity_witnesses"]
        .as_array()
        .unwrap();
    assert_eq!(witnesses.len(), 1, "{program_graph}");
    assert_eq!(witnesses[0]["receiver_roots"], serde_json::json!(["array"]));
    assert_eq!(witnesses[0]["affected_roots"], serde_json::json!(["array"]));
    assert_eq!(witnesses[0]["through_local_alias"], false);
    let result = execution(&fixture, "result");
    // Foreign writes remain conservatively active with their known Array
    // root. The completed callback introduces no unknown alias widening.
    assert_completed_native_integrity_inference(&result);
    let observed = prefix_observation(&result, "array_intrinsics", None);
    assert!(
        observed["prior_affected_roots"] == serde_json::json!(["array"])
            && observed["uncertain_execution"] == false,
        "{result}"
    );
}

#[test]
fn describe_bounds_cross_chunk_prototype_mutations() {
    let fixture = GraphFixture::with_dependency(
        "import './dependency.js';\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });\nconsole.log(result.value);",
        "export function mutate() { globalThis.slot.value = 1; globalThis.other.value = 2; }",
        &[],
    );
    let bounded = fixture.json(&["describe", "result", "--limit", "1"]);
    assert_eq!(
        bounded["native_prototype_mutations"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        bounded["native_prototype_mutations"][0]["chunk_id"],
        "dependency"
    );
    assert_eq!(
        bounded["limits"]["sections"]["native_prototype_mutations"],
        serde_json::json!({"total": 2, "emitted": 1, "truncated": true})
    );
    let mutation = &bounded["native_prototype_mutations"][0];
    assert_eq!(mutation["properties"], serde_json::json!(["value"]));
    assert_eq!(mutation["source_location"]["start_line"], 1);
    assert!(mutation.get("span").is_none());
    let all = fixture.json(&["describe", "result", "--all"]);
    assert_eq!(
        all["native_prototype_mutations"].as_array().unwrap().len(),
        2
    );
    assert_eq!(
        all["limits"]["sections"]["native_prototype_mutations"],
        serde_json::json!({"total": 2, "emitted": 2, "truncated": false})
    );
}

#[test]
fn mutation_witness_keeps_operation_and_known_native_alias_identity() {
    for (body, operation, alias) in [
        (
            "const alias = Array; alias.flag = 1;",
            "property_assignment",
            true,
        ),
        ("Array.flag++;", "update", false),
        ("delete Array.flag;", "delete", false),
        (
            "Object.defineProperty(Array, 'flag', { value: 1 });",
            "mutation_call",
            false,
        ),
    ] {
        let source = format!("export function mutate() {{ {body} }}");
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new("import './mutation.js';\nconsole.log(3);", vec![])
                .with_extra_chunks(&[("static/mutation", &source)]),
        );
        let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
        let witnesses = program_graph["native_integrity_witnesses"]
            .as_array()
            .unwrap();
        assert_eq!(witnesses.len(), 1, "{program_graph}");
        assert_eq!(witnesses[0]["operation"], operation);
        assert_eq!(witnesses[0]["through_local_alias"], alias);
        assert_eq!(witnesses[0]["receiver_roots"], serde_json::json!(["array"]));
        assert_eq!(
            witnesses[0]["source_location"]["source_path"],
            "static/mutation.js"
        );
    }
}

#[test]
fn refined_global_key_witness_explains_its_cross_chunk_native_dependency() {
    for replacement in [
        "export const value = 1;",
        "export function replace() { Symbol.for = () => 'Array'; }",
    ] {
        let fixture = run_fixture_with_graphs(FixtureOpts::new("import './mutation.js';\nconsole.log(3);", vec![]).with_extra_chunks(&[
            ("static/mutation", "import './replacement.js';\nconst key = Symbol.for('example.registry');\nexport function mutate() { globalThis[key] = 1; }"),
            ("static/replacement", replacement),
        ]));
        let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
        let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
        let witnesses = program_graph["native_integrity_witnesses"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let writes: Vec<_> = witnesses
            .iter()
            .filter(|witness| witness["chunk_id"] == "static/mutation")
            .collect();
        if replacement.starts_with("export const") {
            assert!(writes.is_empty(), "{graph}");
        } else {
            assert_eq!(writes.len(), 1, "{graph}");
            assert_eq!(writes[0]["statement_ordinal"], 2);
            assert_eq!(writes[0]["source_location"]["start_line"], 3);
            assert_eq!(
                writes[0]["affected_roots"],
                serde_json::json!(["global_object"])
            );
            assert!(
                writes[0]["invalidated_key_dependencies"]
                    .as_array()
                    .unwrap()
                    .contains(&serde_json::json!("symbol")),
                "{graph}"
            );
            assert!(
                witnesses
                    .iter()
                    .any(|witness| witness["chunk_id"] == "static/replacement"
                        && witness["affected_roots"] == serde_json::json!(["symbol"]))
            );
        }
    }
}

#[test]
fn describe_follows_key_premises_to_the_other_chunk_write() {
    let fixture = GraphFixture::with_dependency(
        "import './dependency.js';\nexport function replace() { Symbol.for = () => 'Array'; }\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });",
        "const key = Symbol.for('example.registry');\nexport function mutate() { globalThis[key] = 1; }",
        &[],
    );
    let report = fixture.json(&["describe", "result", "--all"]);
    let witnesses = report["native_integrity_witnesses"].as_array().unwrap();
    assert!(
        report["native_prototype_mutations"]
            .as_array()
            .unwrap()
            .iter()
            .any(|mutation| mutation["chunk_id"] == "dependency"
                && mutation["properties"].is_null()
                && mutation["key_dependencies"]
                    .as_array()
                    .is_some_and(
                        |dependencies| dependencies.contains(&serde_json::json!("symbol"))
                    ))
    );
    assert!(witnesses.iter().any(|witness| witness["chunk_id"] == "main"
        && witness["affected_roots"] == serde_json::json!(["symbol"])));
}

#[test]
fn untracked_identity_paths_keep_prototype_absence_conservative() {
    for capability in [
        "host(({}).__proto__);",
        "host(() => ({}).__proto__);",
        "host({ get value() { return ({}).__proto__; } });",
        "host({ get value() { return Object.prototype; } });",
        "const { __proto__: prototype } = {}; host(prototype);",
        "host(() => () => Object.prototype);",
    ] {
        let source = format!(
            "function expose() {{ {capability} }}\nclass Box {{ constructor(config) {{ this.value = config.value; }} }}\nconst result = new Box({{value: 3}});"
        );
        let fixture = GraphFixture::new(&source, &[]);
        let report = fixture.json(&["describe", "result", "--all"]);
        assert!(
            report["native_prototype_mutations"]
                .as_array()
                .unwrap()
                .iter()
                .any(|mutation| mutation["kind"] == "escape"
                    && mutation["statement_ordinal"] == 0
                    && mutation["properties"].is_null()),
            "{capability}: {report}"
        );
    }
}

#[test]
fn silent_primitive_push_invalidates_contents_without_inventing_a_getter() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "const record = [0];\nconst first = record.push(1);\nconst second = record.push(2);\nconst copied = record.map(value => value);\nconst snapshot = Object.getOwnPropertyDescriptors(record);\nconsole.log(copied.join(','), snapshot.length.value);\n",
        vec![],
    ).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "0,1,2 3\n");
    let graph = fixture.dependency_graph();
    for binding in ["first", "second"] {
        assert_eq!(
            execution_from_graph(&graph, binding)["complete"],
            true,
            "{binding}"
        );
    }
    for binding in ["copied", "snapshot"] {
        assert_eq!(
            execution_from_graph(&graph, binding)["complete"],
            false,
            "{binding}"
        );
    }
}
