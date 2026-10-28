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
