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
        serde_json::json!({"code": "callback_pool_limit", "limit": 64}),
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
