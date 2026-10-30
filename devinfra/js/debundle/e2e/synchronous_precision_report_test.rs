//! Report evidence comes from the same bounded execution used by the gate.
use debundle_e2e_support::*;
use serde_json::Value;

fn execution(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .expect("binding source unit");
    serde_json::to_value(node).expect("serialize node")["synchronous_execution"].clone()
}

fn assert_reason(execution: &Value, expected: Value) -> &Value {
    assert_eq!(execution["complete"], false, "{execution}");
    execution["precision_losses"]
        .as_array()
        .expect("losses")
        .iter()
        .find(|loss| loss["reason"] == expected)
        .unwrap_or_else(|| panic!("missing {expected} in {execution}"))
}

#[test]
fn complete_execution_has_no_precision_loss() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory() { return 3; }\nconst result = factory();\nconsole.log(result);",
        vec![],
    ));
    assert_entry_output(&fixture, "3\n");
    assert_eq!(
        execution(&fixture, "result"),
        serde_json::json!({
            "complete": true, "precision_losses": []
        })
    );
}

#[test]
fn initialized_enum_own_data_read_is_complete_and_keeps_eager_dependency() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
const selected = [Status.READY];
console.log(selected[0]);
"#,
        vec![],
    ));

    assert_preserved_entry_output(&fixture, "ready\n");
    assert_eq!(
        execution(&fixture, "selected"),
        serde_json::json!({"complete": true, "precision_losses": []})
    );

    let graph = fixture.dependency_graph();
    let read = source_unit_for_binding(&graph, "selected");
    let initializer = source_unit_for_binding(&graph, "Status");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == read
                && edge.target == initializer
                && edge.binding.as_deref() == Some("Status")
                && edge.edge_kind == analysis::DepKind::EagerUse
                && edge.constrains_init_order
        }),
        "the member-read proof must keep the binding's eager initialization dependency"
    );
}

#[test]
fn enum_member_read_proof_rejects_mutation_escape_getter_unknown_key_and_reflection() {
    let source = r#"var Changed = ((values) => ((values.READY = "ready"), values))(Changed || {});
Changed.READY = "changed";
const changed = [Changed.READY];

var Escaped = ((values) => ((values.READY = "ready"), values))(Escaped || {});
globalThis.consume = (value) => { globalThis.retained = value; };
globalThis.consume(Escaped);
const escaped = [Escaped.READY];

const Accessor = { get READY() { return "getter"; } };
const getter = [Accessor.READY];

var Dynamic = ((values) => ((values.READY = "ready"), values))(Dynamic || {});
const key = "READY";
const dynamic = [Dynamic[key]];

var Reflected = ((values) => ((values.READY = "ready"), values))(Reflected || {});
Object.defineProperty(Reflected, "READY", { get() { return "reflected"; } });
const reflected = [Reflected.READY];

console.log(changed[0], escaped[0], getter[0], dynamic[0], reflected[0]);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "changed ready getter ready reflected\n");

    for binding in ["changed", "escaped", "getter", "dynamic", "reflected"] {
        let report = execution(&fixture, binding);
        assert_eq!(report["complete"], false, "{binding}: {report}");
        assert!(
            report["precision_losses"].as_array().is_some_and(|losses| {
                losses.iter().any(|loss| {
                    loss["reason"]["code"] == "unsupported_operation"
                        && loss["reason"]["operation"] == "member_read"
                })
            }),
            "{binding} should retain the conservative member-read path: {report}"
        );
    }
}

#[test]
fn stable_ts_enum_primitive_member_is_safe_as_computed_property_key() {
    let source = r#"var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
const preferences = { [Status.READY]: true };
console.log(preferences.ready);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "true\n");

    let graph = fixture.dependency_graph();
    let preferences_unit = source_unit_for_binding(&graph, "preferences");
    let status_unit = source_unit_for_binding(&graph, "Status");
    let preferences = graph
        .nodes
        .iter()
        .find(|node| node.id == preferences_unit)
        .expect("preferences source unit");
    assert_eq!(
        serde_json::to_value(preferences).expect("serialize node")["purity"]["kind"],
        "pure"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == preferences_unit
                && edge.target == status_unit
                && edge.binding.as_deref() == Some("Status")
                && edge.edge_kind == analysis::DepKind::EagerUse
                && edge.constrains_init_order
        }),
        "the primitive enum proof must retain the eager binding dependency"
    );
}

#[test]
fn enum_property_key_proof_rejects_rebinds_mutations_unknown_keys_accessors_and_escapes() {
    let source = r#"var Rebound = ((values) => ((values.READY = "ready"), values))(Rebound || {});
let reboundCalls = 0;
Rebound = { READY: { toString() { reboundCalls++; return "rebound"; } } };
const rebound = { [Rebound.READY]: true };

var Mutated = ((values) => ((values.READY = "ready"), values))(Mutated || {});
Mutated.READY = { toString() { return "mutated"; } };
const mutated = { [Mutated.READY]: true };

var Dynamic = ((values) => ((values.READY = "ready"), values))(Dynamic || {});
const memberName = "READY";
const dynamic = { [Dynamic[memberName]]: true };

let getterCalls = 0;
const Accessor = { get READY() { getterCalls++; return "accessor"; } };
const accessor = { [Accessor.READY]: true };

var Escaped = ((values) => ((values.READY = "ready"), values))(Escaped || {});
globalThis.retainEnum = (value) => value;
globalThis.retainEnum(Escaped);
const escaped = { [Escaped.READY]: true };

console.log(rebound.rebound, reboundCalls, mutated.mutated, dynamic.ready, accessor.accessor, getterCalls, escaped.ready);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "true 1 true true true 1 true\n");

    let graph = fixture.dependency_graph();
    for binding in ["rebound", "mutated", "dynamic", "accessor", "escaped"] {
        let unit = source_unit_for_binding(&graph, binding);
        let node = graph
            .nodes
            .iter()
            .find(|node| node.id == unit)
            .expect("source unit");
        let purity = serde_json::to_value(node).expect("serialize node")["purity"].clone();
        assert_eq!(purity["kind"], "not_pure", "{binding}: {purity}");
        assert!(
            purity["reasons"].as_array().is_some_and(|reasons| {
                reasons
                    .iter()
                    .any(|reason| reason["rule"] == "to_property_key_coercion")
            }),
            "{binding} must retain the coercion concern: {purity}"
        );
    }
}

#[test]
fn enum_member_read_before_initializer_remains_a_throwing_read() {
    let source = r#"const selected = [Status.READY];
var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
console.log(selected[0]);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));

    assert_preserved_entry_error(&fixture, "TypeError");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("early", &[Member::new("selected")]),
                logical_module("enum", &[Member::new("Status")]),
            ],
        ),
        &["early", "enum"],
        &["eager_use"],
    );
}

#[test]
fn nested_unknown_call_keeps_lexical_callsite_and_deduplicates_cached_execution() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory() { return Number('3'); }\nconst result = factory() + factory();\nconsole.log(result);",
        vec![],
    ));
    assert_entry_output(&fixture, "6\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(&result, serde_json::json!({"code": "unknown_call_target"}));
    assert_eq!(loss["statement_ordinal"], 0);
    assert_eq!(loss["source_location"]["source_path"], "static/app.js");
    assert_eq!(loss["source_location"]["start_line"], 1);
    assert_eq!(loss["source_location"]["start_column"], 29);
    let matching = result["precision_losses"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|loss| loss["reason"]["code"] == "unknown_call_target")
        .count();
    assert_eq!(matching, 1, "{result}");
}

#[test]
fn unsupported_coercion_reports_its_operation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const object = { valueOf() { return 3; } };\nconst result = +object;\nconsole.log(result);",
        vec![],
    ));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(
        &result,
        serde_json::json!({
            "code": "unsupported_operation", "operation": "implicit_coercion"
        }),
    );
    assert_eq!(loss["statement_ordinal"], 1);
    assert_eq!(loss["source_location"]["start_line"], 2);
}

#[test]
fn acyclic_call_chain_reports_the_actual_depth_bound() {
    let mut source = String::new();
    for index in 0..66 {
        source.push_str(&format!(
            "function step{index}() {{ return step{}(); }}\n",
            index + 1
        ));
    }
    source.push_str(
        "function step66() { return 3; }\nconst result = step0();\nconsole.log(result);\n",
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(
        &result,
        serde_json::json!({
            "code": "invocation_depth_limit", "limit": 64
        }),
    );
    assert_eq!(loss["statement_ordinal"], 64);
    assert_eq!(loss["source_location"]["start_line"], 65);
}

#[test]
fn multichunk_acyclic_call_chain_below_depth_bound_remains_complete() {
    let depth = 60;
    let mut source = String::from("const seed = 3;\n");
    for index in 0..depth {
        let next = if index + 1 < depth {
            format!("step{}(value)", index + 1)
        } else {
            "value".to_string()
        };
        source.push_str(&format!(
            "function step{index}(value) {{ return {next}; }}\n"
        ));
    }
    source.push_str("const result = step0(seed);\nconsole.log(result);\n");
    let extra_chunks = [("static/peer", source.as_str())];
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]).with_extra_chunks(&extra_chunks));

    assert_entry_output(&fixture, "3\n");
    assert_eq!(
        execution(&fixture, "result"),
        serde_json::json!({"complete": true, "precision_losses": []})
    );
}

#[test]
fn finite_recursive_function_reports_cycle_without_changing_runtime_result() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory(count) { return count ? factory(count - 1) : 3; }\nconst result = factory(2);\nconst following = 4;\nconsole.log(result + following);",
        vec![],
    ));
    assert_entry_output(&fixture, "7\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(&result, serde_json::json!({"code": "recursive_invocation"}));
    assert_eq!(loss["statement_ordinal"], 0);
    assert_eq!(
        execution(&fixture, "following"),
        serde_json::json!({
            "complete": true, "precision_losses": []
        })
    );
}

#[test]
fn large_callback_pool_reports_the_actual_pool_bound() {
    let callbacks = (0..65).map(|_| "() => 3").collect::<Vec<_>>().join(", ");
    let source =
        format!("const result = JSON.stringify([{callbacks}]);\nconsole.log(result.length);\n");
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_entry_output(&fixture, "326\n");
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({"code": "callback_pool_limit", "limit": 64, "pool_size": 65}),
    );
}

/// Each fixture constructs `result` from a local class whose construction
/// proof fails at one check; the loss names that check.
fn assert_construction_rejection(source: &str, expected_output: &str, rejection: Value) {
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_entry_output(&fixture, expected_output);
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({"code": "unsupported_construction", "rejection": rejection}),
    );
}

#[test]
fn spread_construction_reports_spread_arguments() {
    assert_construction_rejection(
        "class Box { constructor(value) { this.value = value; } }\nconst inputs = [3];\nconst result = new Box(...inputs);\nconsole.log(result.value);",
        "3\n",
        serde_json::json!({"check": "spread_arguments"}),
    );
}

#[test]
fn aliased_class_reports_use_outside_construction() {
    assert_construction_rejection(
        "class Box { constructor(value) { this.value = value; } }\nconst alias = Box;\nconst result = new Box(3);\nconsole.log(result.value, alias === Box);",
        "3 true\n",
        serde_json::json!({"check": "class_used_outside_construction"}),
    );
}

#[test]
fn possible_object_prototype_field_reports_the_field() {
    assert_construction_rejection(
        "function define(name) { Object.defineProperty(Object.prototype, name, { value: 1 }); }\nclass Box { constructor(value) { this.value = value; } }\nconst result = new Box(3);\nconsole.log(result.value, typeof define);",
        "3 function\n",
        serde_json::json!({"check": "field_may_exist_on_object_prototype", "property": "value"}),
    );
}

#[test]
fn unknown_property_source_reports_its_parameter() {
    assert_construction_rejection(
        "class Box { constructor(config) { this.value = config.value; } }\nconst result = new Box(JSON.parse('{\"value\": 3}'));\nconsole.log(result.value);",
        "3\n",
        serde_json::json!({"check": "unchecked_property_source", "parameter": 0}),
    );
}

#[test]
fn wide_acyclic_execution_below_the_expansion_bound_remains_complete() {
    let mut source = String::new();
    let mut calls = String::new();
    for index in 0..240 {
        source.push_str(&format!("function step{index}() {{ return 3; }}\n"));
        calls.push_str(&format!("step{index}();"));
    }
    source.push_str(&format!("function factory() {{ {calls} return 3; }}\nconst result = factory();\nconsole.log(result);\n"));
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_entry_output(&fixture, "3\n");
    assert_eq!(
        execution(&fixture, "result"),
        serde_json::json!({"complete": true, "precision_losses": []})
    );
}

#[test]
fn wide_acyclic_execution_reports_the_actual_expansion_bound() {
    let mut source = String::new();
    let mut calls = String::new();
    for index in 0..260 {
        source.push_str(&format!("function step{index}() {{ return 3; }}\n"));
        calls.push_str(&format!("step{index}();"));
    }
    source.push_str(&format!("function factory() {{ {calls} return 3; }}\nconst result = factory();\nconsole.log(result);\n"));
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({
            "code": "invocation_expansion_limit", "limit": 256
        }),
    );
}

#[test]
fn property_absence_failure_reports_the_cross_chunk_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "import './mutation.js';\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });\nconsole.log(result.value);",
        vec![],
    ).with_extra_chunks(&[("static/mutation", "export function mutate() { const alias = globalThis.slot; alias.value = 1; }")]));
    assert_entry_output(&fixture, "3\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let witnesses = graph["native_integrity_witnesses"]
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
    let fixture = run_fixture(
        FixtureOpts::new(
            "import './mutation.js';\nclass Box { constructor(config) { this.value = config.value; } }\nconst result = new Box({ value: 3 });\nconsole.log(result.value);",
            vec![],
        )
        .with_extra_chunks(&[("static/mutation", "const object = {}; export function mutate() { object.flag = 1; }")]),
    );
    assert_entry_output(&fixture, "3\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(graph.get("native_integrity_witnesses").is_none(), "{graph}");
    assert!(
        execution(&fixture, "result")
            .get("native_integrity_checks")
            .is_none()
    );
}

fn array_prefix_fixture(write: &str) -> Fixture {
    let source = format!(
        "function mutate() {{ Array.flag = 1; }}\n\
         {write}\n\
         const arrayResult = [1, 2].map(value => value + 1);\n\
         console.log(arrayResult.join(','));"
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_entry_output(&fixture, "2,3\n");
    fixture
}

fn prototype_prefix_fixture(write: &str) -> Fixture {
    let source = format!(
        "function mutate() {{ Object.prototype.prefixProbe = 1; }}\n\
         {write}\n\
         class Box {{ constructor(config) {{ this.prefixProbe = config.prefixProbe; }} }}\n\
         const result = new Box({{ prefixProbe: 3 }});\n\
         console.log(result.prefixProbe);"
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
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

#[test]
fn unused_deferred_native_writer_is_absent_from_prefix_observations() {
    let fixture = array_prefix_fixture("");
    let array = execution(&fixture, "arrayResult");
    let observed = prefix_observation(&array, "array_intrinsics", None);
    assert_eq!(
        observed["prior_affected_roots"],
        serde_json::json!([]),
        "{array}"
    );
    assert_eq!(observed["uncertain_execution"], false);
    // The existing whole-program guard still rejects the Array premise.
    assert!(
        array["native_integrity_checks"].as_array().is_some(),
        "{array}"
    );
    let fixture = prototype_prefix_fixture("");
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
    let fixture = run_fixture(
        FixtureOpts::new(
            "import './mutation.js';\nfunction local() { globalThis.slot.value = 1; }\nclass Box { constructor(config) { this.prefixProbe = config.prefixProbe; } }\nconst result = new Box({ prefixProbe: 3 });\nconsole.log(result.prefixProbe);",
            vec![],
        )
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
        let fixture = array_prefix_fixture(write);
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
        let fixture = prototype_prefix_fixture(write);
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
    let fixture = run_fixture(
        FixtureOpts::new(
            "import './mutation.js';\nconst result = [1, 2].map(value => value + 1);\nconsole.log(result.join(','));",
            vec![],
        )
        .with_extra_chunks(&[("static/mutation", "export function mutate() { Array.flag = 1; }")]),
    );
    assert_entry_output(&fixture, "2,3\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let witnesses = graph["native_integrity_witnesses"].as_array().unwrap();
    assert_eq!(witnesses.len(), 1, "{graph}");
    assert_eq!(witnesses[0]["receiver_roots"], serde_json::json!(["array"]));
    assert_eq!(witnesses[0]["affected_roots"], serde_json::json!(["array"]));
    assert_eq!(witnesses[0]["through_local_alias"], false);
    let result = execution(&fixture, "result");
    assert!(
        result["native_integrity_checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|check| check["guard"] == "array_intrinsics"
                && check["affected_roots"] == serde_json::json!(["array"])),
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
        let fixture = run_fixture(
            FixtureOpts::new("import './mutation.js';\nconsole.log(3);", vec![])
                .with_extra_chunks(&[("static/mutation", &source)]),
        );
        let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
        let witnesses = graph["native_integrity_witnesses"].as_array().unwrap();
        assert_eq!(witnesses.len(), 1, "{graph}");
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
        let fixture = run_fixture(FixtureOpts::new("import './mutation.js';\nconsole.log(3);", vec![]).with_extra_chunks(&[
            ("static/mutation", "import './replacement.js';\nconst key = Symbol.for('example.registry');\nexport function mutate() { globalThis[key] = 1; }"),
            ("static/replacement", replacement),
        ]));
        let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
        let witnesses = graph["native_integrity_witnesses"]
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
fn describe_reports_a_discharged_receiver_with_constructor_provenance_and_limits() {
    let fixture = GraphFixture::new(
        "function Holder(value) { this.value = value; }\nfunction clone(value) { return new Holder(value); }\nexport function update(value, branch) {\n  var receiver = value instanceof Holder ? clone(value) : new Holder(value);\n  if (branch) { receiver.flag = true; receiver.other = true; }\n  else receiver = globalThis.slot;\n  return receiver;\n}\n",
        &[],
    );
    let bounded = fixture.json(&["describe", "update", "--limit", "1"]);
    assert_eq!(
        bounded["limits"]["sections"]["native_receiver_certificates"],
        serde_json::json!({"total": 2, "emitted": 1, "truncated": true})
    );
    let certificate = &bounded["native_receiver_certificates"][0];
    assert_eq!(certificate["receiver_binding"], "receiver");
    assert_eq!(certificate["chunk_id"], "main");
    assert_eq!(certificate["source_location"]["start_line"], 5);
    assert_eq!(certificate["initializer_location"]["start_line"], 4);
    assert_eq!(
        certificate["previously_affected_roots"],
        serde_json::json!(["global_object"])
    );
    assert_eq!(certificate["origins"][0]["kind"], "wrapper");
    assert_eq!(certificate["origins"][0]["binding"], "clone");
    assert_eq!(
        certificate["origins"][0]["source_location"]["start_line"],
        2
    );
    assert_eq!(
        certificate["origins"][0]["constructor_location"]["start_line"],
        1
    );
    assert_eq!(certificate["origins"][1]["kind"], "constructor");
    assert_eq!(certificate["origins"][1]["binding"], "Holder");
    assert!(certificate.get("span").is_none());
    let all = fixture.json(&["describe", "update", "--all"]);
    assert_eq!(
        all["native_receiver_certificates"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        all["limits"]["sections"]["native_receiver_certificates"],
        serde_json::json!({"total": 2, "emitted": 2, "truncated": false})
    );
    assert!(
        all["native_integrity_witnesses"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
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
