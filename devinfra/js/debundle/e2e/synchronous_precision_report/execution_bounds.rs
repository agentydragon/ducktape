//! Call depth, recursion, callback pool and expansion bounds report the actual limit without changing output.

use super::{assert_reason, execution, execution_from_graph, global_reach};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn nested_unknown_call_keeps_lexical_callsite_and_deduplicates_cached_execution() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "function factory() { return Number('3'); }\nconst result = factory() + factory();\nconsole.log(result);",
        vec![],
    ).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "6\n");
    let graph = fixture.dependency_graph();
    let result = execution_from_graph(&graph, "result");
    let loss = assert_reason(&result, serde_json::json!({"code": "unknown_call_target"}));
    assert_eq!(loss["reach"], global_reach("unknown_callee"));
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "const object = { valueOf() { return 3; } };\nconst result = +object;\nconsole.log(result);",
        vec![],
    ).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(
        &result,
        serde_json::json!({
            "code": "unsupported_operation", "operation": "implicit_coercion"
        }),
    );
    assert_eq!(loss["reach"], global_reach("unmodelled_operation"));
    assert_eq!(loss["statement_ordinal"], 1);
    assert_eq!(loss["source_location"]["start_line"], 2);
}

#[test]
fn single_call_root_depth_limit_does_not_invent_a_caller_remainder() {
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
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(
        &result,
        serde_json::json!({"code": "invocation_depth_limit", "limit": 64}),
    );
    // The truncated chain calls only local functions.
    assert_eq!(
        loss["reach"],
        serde_json::json!({"kind": "bounded"}),
        "{result:#}"
    );
    assert_eq!(loss["statement_ordinal"], 64);
    assert_eq!(loss["source_location"]["start_line"], 65);
    assert_eq!(
        result["precision_losses"].as_array().unwrap().len(),
        1,
        "the exact terminal root keeps the child's depth loss without adding an opaque/global caller remainder: {result:#}"
    );
}

#[test]
fn compound_call_chains_preserve_output_at_the_depth_limit() {
    // The literal suffix is supported by the ordered kernel even when the
    // primitive operator requires the separate conservative fallback.
    for tail in ["seed + 1", "8"] {
        let mut source = String::from("const seed = 7;\n");
        for index in 0..66 {
            source.push_str(&format!(
                "function step{index}() {{ return (step{}(), {tail}); }}\n",
                index + 1
            ));
        }
        source.push_str(&format!(
            "function step66() {{ return {tail}; }}\nconst marker = 1;\nconst result = step0();\nconsole.log(result, marker);\n",
        ));
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );

        assert_preserved_entry_output(&fixture, "8 1\n");
        let result = execution(&fixture, "result");
        let loss = assert_reason(
            &result,
            serde_json::json!({"code": "invocation_depth_limit", "limit": 64}),
        );
        assert_eq!(
            loss["reach"],
            serde_json::json!({"kind": "bounded"}),
            "{result:#}"
        );
    }
}

#[test]
fn compound_call_root_keeps_its_unexecuted_remainder_fallback() {
    let mut source = String::from("const seed = 7;\n");
    for index in 0..66 {
        source.push_str(&format!(
            "function step{index}() {{ return step{}(); }}\n",
            index + 1
        ));
    }
    source.push_str("function step66() { return 3; }\nfunction tail() { return seed; }\nconst result = (step0(), tail());\nconsole.log(result);\n");
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "7\n");
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({"code": "invocation_depth_limit", "limit": 64}),
    );
    assert!(
        result["precision_losses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|loss| {
                loss["reason"]["code"] == "unsupported_operation"
                    && loss["reason"]["operation"] == "opaque_execution"
            }),
        "the second root operand still needs conservative remainder handling: {result:#}"
    );
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
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(&source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&extra_chunks),
    );

    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(
        result["precision_losses"],
        serde_json::json!([]),
        "{result:#}"
    );
    // The whole chain runs below the bound, so the root call seals its owner.
    assert_eq!(
        result["binding_effect_authority"]["authority"], "operation_receipt",
        "{result:#}"
    );
}

#[test]
fn finite_recursive_function_reports_cycle_without_changing_runtime_result() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "function factory(count) { return count ? factory(count - 1) : 3; }\nconst result = factory(2);\nconst following = 4;\nconsole.log(result + following);",
        vec![],
    ).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "7\n");
    let graph = fixture.dependency_graph();
    let result = execution_from_graph(&graph, "result");
    let loss = assert_reason(&result, serde_json::json!({"code": "recursive_invocation"}));
    assert_eq!(loss["reach"], serde_json::json!({"kind": "bounded"}));
    assert_eq!(loss["statement_ordinal"], 0);
    assert_eq!(
        execution_from_graph(&graph, "following"),
        serde_json::json!({
            "binding_effect_authority": {
                "authority": "legacy_execution", "complete": true
            },
            "complete": true, "precision_losses": []
        })
    );
}

#[test]
fn large_callback_pool_reports_the_actual_pool_bound() {
    let callbacks = (0..65).map(|_| "() => 3").collect::<Vec<_>>().join(", ");
    let source =
        format!("const result = JSON.stringify([{callbacks}]);\nconsole.log(result.length);\n");
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "326\n");
    let result = execution(&fixture, "result");
    let loss = assert_reason(
        &result,
        serde_json::json!({"code": "callback_pool_limit", "limit": 64, "pool_size": 65}),
    );
    assert_eq!(loss["reach"], global_reach("escaped_callback_pool"));
}

#[test]
fn small_callback_pool_formals_keep_callbacks_and_unknown_values() {
    for relay in [
        "function relay(candidate) { candidate(); }",
        "function factory(captured) { return function relay(candidate) { candidate(); captured(); }; }\nconst relay = factory(read);",
    ] {
        let source = format!(
            "function read() {{ return later; }}\n{relay}\nconst result = JSON.stringify([relay, read]);\nconst later = 7;\nconsole.log(result);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "[null,null]\n");
        // Localize Top to this formal's invocation, not JSON.stringify or
        // the captured callback's separate invocation.
        let graph = fixture.dependency_graph();
        let result_execution = execution_from_graph(&graph, "result");
        let candidate_location = serde_json::json!({
            "source_path": "static/app.js",
            "start_line": 2,
            "end_line": 2,
            "start_column": relay.find("candidate();").unwrap() + 1,
        });
        let matching = result_execution["precision_losses"]
            .as_array()
            .expect("precision losses")
            .iter()
            .filter(|loss| {
                loss["reason"] == serde_json::json!({"code": "unknown_call_target"})
                    && loss["statement_ordinal"] == 1
                    && loss["source_location"] == candidate_location
            })
            .count();
        assert_eq!(
            matching, 1,
            "formal invocation lost its unknown target at {candidate_location}: {result_execution}"
        );
        // The directly pooled reader also retains its early read. Argument
        // forwarding equality follows from the unchanged callbacks + Top set.
        let result = source_unit_for_binding(&graph, "result");
        let later = source_unit_for_binding(&graph, "later");
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == result
                    && edge.target == later
                    && edge.binding.as_deref() == Some("later")
            }),
            "pool lost the early reader"
        );
    }
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
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(result["precision_losses"], serde_json::json!([]));
    assert_eq!(
        result["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    assert_eq!(
        result["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        241
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
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({
            "code": "invocation_expansion_limit", "limit": 256
        }),
    );
}
