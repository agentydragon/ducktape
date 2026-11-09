//! Report-only effect translation validation: operation receipts for local
//! calls, callbacks and their retained prefix dependencies.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use sha2::{Digest, Sha256};

#[test]
fn local_cell_read_effects_compose_without_an_allocation_result() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .expect("compound local-call initializer");
    assert!(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution summary")
            .complete,
        "the missing proof concerns effect domains, not execution: {result:#?}"
    );
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "the lexical domains are certified by executed local-call receipts: {execution:#}"
    );
    assert_eq!(
        execution["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "both source call-sites contribute to the root receipt: {execution:#}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "seed")
        })
        .expect("seed source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "the observed seed read remains a graph edge: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only the output call remains uncovered: {report:#}"
        );
    }
}

#[test]
fn single_local_call_seals_real_lexical_read_and_call_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .expect("single-call initializer");
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "one actual local call must have operation evidence rather than legacy completion authority: {execution:#}"
    );
    let receipt = &execution["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["owner"], 2);
    assert_eq!(receipt["binding"], "result");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 1, "{receipt:#}");
    assert_eq!(calls[0]["caller"]["kind"], "root_initializer");
    assert_eq!(calls[0]["caller"]["owner"], receipt["owner"]);
    assert_eq!(calls[0]["call_span"], receipt["initializer_span"]);
    assert_eq!(
        calls[0]["caller"]["initializer_span"],
        receipt["initializer_span"]
    );
    assert_eq!(calls[0]["target"]["kind"], "callable");
    assert_eq!(calls[0]["target"]["owner"], 1);
    let seed = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 0)
        .unwrap();
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
                && edge.constrains_init_order
        }),
        "the observed child read must retain its ordering edge: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn inline_callee_creation_executes_with_exact_operation_receipts() {
    for (case, expression) in [
        ("arrow", "((value) => value)((() => 42)())"),
        (
            "anonymous function",
            "(function(value) { return value; })((function() { return 42; })())",
        ),
    ] {
        let source = format!("const result = {expression};\nconsole.log(result);\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        assert_inline_operation_receipt(&fixture, "result", 2, case);
    }
}

#[test]
fn inline_callee_invocation_keeps_captured_reads_and_refuses_unsupported_bodies() {
    let captured = run_fixture(
        FixtureOpts::new(
            "const seed = 41;\nconst result = ((value) => value)((() => seed)());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&captured, "41\n");
    assert_inline_operation_receipt(&captured, "result", 2, "captured-read");
    let graph = captured.dependency_graph();
    let result = source_unit_for_binding(&graph, "result");
    let seed = source_unit_for_binding(&graph, "seed");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result
                && edge.target == seed
                && edge.binding.as_deref() == Some("seed")
                && edge.constrains_init_order
        }),
        "invoking the inline arrow must retain its captured read: {graph:#?}"
    );

    let unsupported = run_fixture(
        FixtureOpts::new(
            "const result = ((value) => value)((() => { for (let index = 0; index < 1; index++) {} return 42; })());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&unsupported, "42\n");
    let execution = binding_execution_report(&unsupported, "result");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "unsupported callable syntax must keep the root from receiving local-operation authority: {execution:#}"
    );

    let named = run_fixture(
        FixtureOpts::new(
            "const result = (function identity(value) { return value; })((function constant() { return 42; })());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&named, "42\n");
    let execution = binding_execution_report(&named, "result");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "named function self scopes remain outside operation receipts: {execution:#}"
    );
}

#[test]
fn single_aliased_call_keeps_complete_but_uncovered_fallback() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = alias();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 4)
        .unwrap();
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an identifier alias may execute completely without a selected direct-declaration receipt: {execution:#}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 0)
        .unwrap();
    let later = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 1)
        .unwrap();
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "the actual seed read remains observed: {graph:#?}"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == later.id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result.id.as_str())
                    })
                })
        }),
        "the dormant closure needs graph fallback even though the alias call completed: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "the unreceipted result and output remain unknown: {report:#}"
        );
    }
}

#[test]
fn ordered_callback_return_retains_the_selected_call_and_read_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst holder = { callback: () => early };\nfunction mutate() { holder.callback = () => later; return 0; }\nfunction take(value) { return value; }\nconst selected = take(holder.callback, mutate());\nconst result = selected();\nconst later = (result, 42);\nconsole.log(result, later, holder.callback());\n",
        vec![
            logical_module("a_record", &[Member::new("early"), Member::new("holder")]),
            logical_module("m_mutator", &[Member::new("mutate")]),
            logical_module("n_take", &[Member::new("take")]),
            logical_module(
                "z_observed",
                &[Member::new("selected"), Member::new("result")],
            ),
            logical_module("x_later", &[Member::new("later")]),
            logical_module_with_anon(
                "observe",
                &[],
                &["console.log(result, later, holder.callback());"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42 42\n");
    let graph = fixture.dependency_graph();
    let selected = binding_execution_report(&fixture, "selected");
    assert_eq!(
        selected["binding_effect_authority"]["authority"], "operation_receipt",
        "{selected:#}"
    );
    assert_eq!(
        selected["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "the mutating argument and forwarding call both execute: {selected:#}"
    );
    let result = binding_execution_report(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(
        result["binding_effect_authority"]["authority"], "operation_receipt",
        "{result:#}"
    );
    let receipt = &result["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["binding"], "result");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 1, "{receipt:#}");
    assert_eq!(calls[0]["caller"]["kind"], "root_initializer");
    assert_eq!(calls[0]["caller"]["owner"], receipt["owner"]);
    assert_eq!(calls[0]["call_span"], receipt["initializer_span"]);
    assert_eq!(calls[0]["target"]["kind"], "callable");
    assert_eq!(
        calls[0]["target"]["owner"], 1,
        "the selected callback belongs to holder's original definition: {receipt:#}"
    );
    let result_id = source_unit_for_binding(&graph, "result");
    let early_id = source_unit_for_binding(&graph, "early");
    let holder_id = source_unit_for_binding(&graph, "holder");
    let later_id = source_unit_for_binding(&graph, "later");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result_id
                && edge.target == early_id
                && edge.binding.as_deref() == Some("early")
                && edge.constrains_init_order
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(holder_id)
                    })
                })
        }),
        "the actual selected body read remains ordered: {graph:#?}"
    );
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == result_id && edge.target == later_id && edge.constrains_init_order
        }),
        "the replacement callback must stay deferred during result initialization: {graph:#?}"
    );
}

#[test]
fn default_callback_and_parameter_read_have_receipts_without_eager_returned_body_reads() {
    const SOURCE: &str = "const value = 1;\nfunction factory(callback = input => input) { callback(0); return () => value; }\nconst created = factory();\nconsole.log(created());\nexport { value, factory, created };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("create", &[Member::new("created")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    let graph = fixture.dependency_graph();
    let created = binding_execution_report(&fixture, "created");
    assert_eq!(created["complete"], true, "{created:#}");
    assert_eq!(
        created["binding_effect_authority"]["authority"], "operation_receipt",
        "{created:#}"
    );
    let receipt = &created["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["binding"], "created");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(
        calls.len(),
        2,
        "the factory and actual default callback invocation both contribute: {receipt:#}"
    );
    let root_call = calls
        .iter()
        .find(|call| call["caller"]["kind"] == "root_initializer")
        .unwrap();
    assert_eq!(root_call["caller"]["owner"], receipt["owner"]);
    assert_eq!(root_call["call_span"], receipt["initializer_span"]);
    assert_eq!(root_call["target"]["owner"], 1);
    let callback_call = calls
        .iter()
        .find(|call| call["caller"]["kind"] == "callable")
        .unwrap();
    assert_eq!(callback_call["caller"], root_call["target"]);
    assert_eq!(callback_call["target"]["owner"], 1);
    assert_ne!(callback_call["target"]["key"], root_call["target"]["key"]);
    let start = SOURCE.find("callback(0)").unwrap();
    assert_eq!(
        callback_call["call_span"],
        serde_json::json!({"start": start + 1, "end": start + 1 + "callback(0)".len()})
    );
    let created_id = source_unit_for_binding(&graph, "created");
    let value_id = source_unit_for_binding(&graph, "value");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == created_id && edge.target == value_id && edge.constrains_init_order
        }),
        "returning the closure must not execute its module-cell read: {graph:#?}"
    );
}

#[test]
fn ordered_forwarding_does_not_borrow_provenance_from_an_equal_memo_argument() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nfunction forward(value) { return value; }\nconst selected = (forward(readSeed), forward(alias));\nvar result = selected();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("forward"),
                Member::new("selected"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let selected = binding_execution_report(&fixture, "selected");
    assert_eq!(selected["complete"], true, "{selected:#}");
    assert_eq!(
        selected["binding_effect_authority"]["authority"], "operation_receipt",
        "{selected:#}"
    );
    assert_eq!(
        selected["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "both forwards execute with the same callable value but different selection evidence: {selected:#}"
    );
    let result = binding_execution_report(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(
        result["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an actual forwarding return cannot invent the missing second argument's origin: {result:#}"
    );
    let result_id = source_unit_for_binding(&graph, "result");
    let seed_id = source_unit_for_binding(&graph, "seed");
    let later_id = source_unit_for_binding(&graph, "later");
    assert!(
        graph.edges.iter().any(|edge| edge.source == result_id
            && edge.target == seed_id
            && edge.binding.as_deref() == Some("seed")),
        "the observed seed read remains: {graph:#?}"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result_id
                && edge.target == later_id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result_id)
                    })
                })
        }),
        "the unproved selected value retains its assumed closure fallback: {graph:#?}"
    );
}

#[test]
fn default_parameter_reads_and_writes_keep_later_parameters_in_tdz() {
    for default in ["later", "(later = 1)"] {
        let source = format!(
            "function factory(first = {default}, later = 2) {{ return first; }}\nconst result = factory();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[Member::new("factory"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_error(&fixture, "ReferenceError");
        let result = binding_execution_report(&fixture, "result");
        assert_eq!(
            result["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
            "an abrupt default cannot acquire normal root authority: {default} / {result:#}"
        );
    }
}

#[test]
fn complete_aliased_local_calls_without_a_receipt_keep_fallback_authority() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = (alias(), alias());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("aliased local-call root");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("root execution report"),
    )
    .unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "the alias executes but has no selected direct-call receipt, so complete cannot skip fallback: {execution:#}"
    );
    let later = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "later")
        })
        .expect("deferred closure's later binding source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == later.id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result.id.as_str())
                    })
                })
        }),
        "the dead deferred closure contributes a fallback-only later dependency; the caller role identifies unresolved-call closure promotion: {graph:#?}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "seed")
        })
        .expect("seed source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "known seed access stays in the graph when the receipt is absent: {graph:#?}"
    );
}

#[test]
fn local_call_receipts_retain_conditional_prefix_dependencies_when_reordered() {
    const SOURCE: &str = "const native = new Map();\nconst seed = 41;\nfunction readSeed() { return seed; }\nconst marker = 1;\nvar result = (readSeed(), readSeed());\nconsole.log(result, marker, native.size);\n";
    let input = native_constructor_input(
        SOURCE,
        "const native = new Map();",
        "native",
        NativeOperationKind::MapEmpty,
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("native"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input]),
    );
    assert_preserved_entry_output(&fixture, "41 1 0\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["swapped_pairs"], 1,
        "only result crosses marker; the native owner stays first: {report:#}"
    );
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["relocation_dependencies"]["native_operation_inputs"],
        serde_json::json!([{ "defining_chunk": "static/app", "input_index": 0 }]),
        "the reordered result needs the established prefix conditions, including memoized child use: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn local_call_receipts_retain_consumed_host_prefix_prerequisites_when_reordered() {
    // A static import links before evaluation even at this later source
    // position; keeping its ordinal later isolates result crossing marker.
    const SOURCE: &str = "const native = new Map();\nconst gate = Promise.resolve();\nconst seed = 41;\nfunction readSeed() { return seed; }\nconst marker = 1;\nvar result = (readSeed(), readSeed());\nimport { reflect } from './reflect.js';\nconsole.log(result, marker, Boolean(gate), native.size);\n";
    const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
    let map_input = native_constructor_input(
        SOURCE,
        "const native = new Map();",
        "native",
        NativeOperationKind::MapEmpty,
    );
    let promise_input = NativeOperationInputContract {
        selector: declared_source_unit("const gate = Promise.resolve();", "gate"),
        operation: NativeOperationKind::PromiseResolve,
        audited_source_sha256: Sha256::digest(SOURCE.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Neutral fixture audits this exact empty Promise.resolve call.".to_string()),
    };
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("native"),
                    Member::new("gate"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(&[("static/reflect", REFLECT_SOURCE)])
        .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)])
        .with_native_operation_inputs("static/app", vec![map_input, promise_input])
        .with_host_behavior(HostBehavior {
            reentry_free_operations: [ReentryFreeOperation::PromiseResolve].into_iter().collect(),
            ..HostBehavior::default()
        }),
    );
    assert_preserved_entry_output_with_bun(&fixture, "41 1 true 0\n");
    let graph = fixture.dependency_graph();
    let gate = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "gate")
        })
        .expect("selected host initializer");
    assert!(!gate.purity.is_pure(), "host timing does not grant purity");
    let execution = serde_json::to_value(
        gate.synchronous_execution
            .as_ref()
            .expect("host execution report"),
    )
    .unwrap();
    let native_uses = execution["native_operation_input_uses"]
        .as_array()
        .expect("actual selected native input use");
    assert_eq!(native_uses.len(), 1, "{execution:#}");
    assert_eq!(
        native_uses[0]["evidence"]["operation"], "promise_resolve",
        "{execution:#}"
    );
    assert_eq!(
        native_uses[0]["evidence"]["selected_binding"], "gate",
        "{execution:#}"
    );
    assert_eq!(
        native_uses[0]["evidence"]["reference"]["input_index"], 1,
        "{execution:#}"
    );
    assert!(
        execution["native_integrity_checks"]
            .as_array()
            .is_some_and(|checks| checks.iter().any(|check| {
                check["guard"] == "host_promise_resolve"
                    && check["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| roots.iter().any(|root| root == "global_object"))
            })),
        "the reflective source must make same-site native identity necessary: {execution:#}"
    );
    let host_uses = execution["author_host_contract_uses"]
        .as_array()
        .expect("actual host contract use");
    assert_eq!(host_uses.len(), 1, "{execution:#}");
    assert_eq!(
        host_uses[0]["operation"], "promise_resolve",
        "{execution:#}"
    );
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("local-call result");
    assert!(
        result
            .synchronous_execution
            .as_ref()
            .expect("result execution")
            .complete
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["swapped_pairs"], 1,
        "only result crosses marker; both native/host owners stay earlier: {report:#}"
    );
    assert_eq!(
        report["decision"]["status"], "accepted",
        "the result must already have positive receipts: {report:#}"
    );
    let native_prerequisites = report["relocation_dependencies"]["native_operation_inputs"]
        .as_array()
        .expect("known Map prerequisite proves nonempty result relocation evidence");
    assert!(
        native_prerequisites
            .iter()
            .any(|input| input["defining_chunk"] == "static/app" && input["input_index"] == 0),
        "{report:#}"
    );
    assert_eq!(
        report["relocation_dependencies"],
        serde_json::json!({
            "native_operation_inputs": [
                { "defining_chunk": "static/app", "input_index": 0 },
                { "defining_chunk": "static/app", "input_index": 1 },
            ],
            "host_behavior": [{ "reentry_free_operation": "promise_resolve" }],
        }),
        "the result's positive/memoized call receipts must retain the actually consumed host and same-site native prerequisites: {report:#}"
    );
}

#[test]
fn later_rebinding_does_not_erase_an_earlier_local_call_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nreadSeed = () => 0;\nconsole.log(result, readSeed());\n",
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
            &["readSeed = () => 0;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41 0\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "only later rebinding and output remain uncovered: {report:#}"
        );
    }
}

#[test]
fn local_call_effect_receipts_refuse_unmodeled_operations_and_prologues() {
    for (seed, helper, invocation, lexical_authority, unknown) in [
        (
            "41",
            "function readSeed(unused) { return seed; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed(unused = seed) { return seed; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed(...unused) { return seed; }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { void arguments; return seed; }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed(unused) { return seed; }",
            "readSeed(seed)",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed() { const local = seed; return local; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "({value: 41})",
            "function readSeed() { return seed.value.toString(); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { return Math.abs(-41); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { return globalThis.Number(41); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
    ] {
        let source = format!(
            "const seed = {seed};\n{helper}\nvar result = ({invocation}, {invocation});\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown,
                "only proved private-cell operations can close the initializer; output remains unknown for {helper} / {invocation}: {report:#}"
            );
        }
        let graph = fixture.dependency_graph();
        let result = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "result")
            })
            .expect("result owner for refused local-call receipt");
        let execution = serde_json::to_value(
            result
                .synchronous_execution
                .as_ref()
                .expect("execution report"),
        )
        .unwrap();
        assert_eq!(
            execution["binding_effect_authority"]["authority"], lexical_authority,
            "only modeled lexical operations and prologues supply binding receipts: {helper} / {invocation}: {execution:#}"
        );
    }
}

#[test]
fn imported_cell_reads_do_not_receive_local_module_cell_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { sep as seed } from 'node:path';\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[Member::new("readSeed"), Member::new("result")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "/\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "{report:#}"
    );
}

#[test]
fn refused_call_receipts_preserve_actual_argument_evaluation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nfunction actual() { console.log('argument'); return seed; }\nvar result = (readSeed(actual()), readSeed(actual()));\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("actual"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "argument\nargument\n41\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "actual call arguments remain observed without sealing unsupported nested effects: {execution:#}"
    );
}

#[test]
fn abrupt_local_call_receipt_cannot_complete_root_initialization() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { throw seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_error(&fixture, "41");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an abrupt operation never seals lexical coverage: {execution:#}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
}
