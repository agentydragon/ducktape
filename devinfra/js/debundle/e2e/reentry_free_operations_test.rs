//! Audited `host_behavior.reentry_free_operations` stay opt-in and shape-specific.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn policy(operations: &[ReentryFreeOperation]) -> HostBehavior {
    HostBehavior {
        reentry_free_operations: operations.iter().copied().collect(),
        ..HostBehavior::default()
    }
}

fn split<'a>(source: &'a str, binding: &'static str) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "host/value",
                &[Member::new(binding)],
                &["globalThis.saved = () => later;"],
            ),
            logical_module("host/later", &[Member::new("later")]),
        ],
    )
}

const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
const REFLECT_CHUNKS: &[(&str, &str)] = &[("static/reflect", REFLECT_SOURCE)];
const REFLECT_FILES: &[(&str, &str)] = &[("static/reflect.js", REFLECT_SOURCE)];
const PROMISE_REENTRY_SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst gate = Promise.resolve();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst laterValue = factory();\nglobalThis.unrelatedKey = 'safe';\nconsole.log(Boolean(gate), laterValue.name, LaterError.name, globalThis.unrelatedKey);\nexport { gate, factory, LaterError, reflect };\n";

fn source_hash(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn promise_input(source: &str, binding: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with(&format!("const {binding} =")))
        .expect("fixture has a direct Promise initializer");
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(
            serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [{binding}]\n",
                declaration.trim()
            ))
            .expect("valid declaration source match"),
        ),
        operation: NativeOperationKind::PromiseResolve,
        audited_source_sha256: source_hash(source),
        note: Some("Neutral fixture audits this one Promise initializer.".to_string()),
    }
}

fn promise_reentry_split() -> FixtureOpts<'static> {
    FixtureOpts::new(
        PROMISE_REENTRY_SOURCE,
        vec![
            logical_module_with_anon(
                "gate",
                &[Member::new("gate")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    )
    .with_extra_chunks(REFLECT_CHUNKS)
    .with_extra_files(REFLECT_FILES)
}

fn native_input_uses(graph: &Value) -> Vec<Value> {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect()
}

fn host_contract_uses(graph: &Value) -> Vec<Value> {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["author_host_contract_uses"]
                .as_array()
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect()
}

fn unknown_receiver_witness(program_graph: &Value) -> &Value {
    program_graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .find(|witness| {
            witness["operation"] == "mutation_call"
                && witness["receiver_roots"]
                    .as_array()
                    .is_some_and(|roots| roots.iter().any(|root| root == "unknown_global_value"))
                && witness["source_location"]["source_path"] == "static/reflect.js"
        })
        .unwrap_or_else(|| {
            panic!("reflective helper's unknown receiver is absent: {program_graph}")
        })
}

fn node_for_binding<'a>(graph: &'a Value, binding: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|candidate| candidate["binding"] == binding)
                })
        })
        .unwrap_or_else(|| panic!("no source node for {binding}: {graph}"))
}

fn has_unknown_call_target(node: &Value) -> bool {
    node["synchronous_execution"]["precision_losses"]
        .as_array()
        .is_some_and(|losses| {
            losses
                .iter()
                .any(|loss| loss["reason"]["code"] == "unknown_call_target")
        })
}

fn assert_host_call_remains_impure(fixture: &Fixture, binding: &str) {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|entry| entry.binding == binding)
        })
        .expect("host call source unit");
    assert!(!node.purity.is_pure(), "host timing must not grant purity");
}

fn assert_candidate_schedule(
    fixture: &Fixture,
    emitted_order: &[usize],
    swapped_pairs: u64,
    scope: &str,
) {
    let candidate = fixture.candidate_relocation_assessment();
    assert_eq!(candidate["status"], "accepted_within_checked_scope");
    assert_eq!(candidate["decision"]["scope"], scope);
    assert_eq!(candidate["emitted_order"], serde_json::json!(emitted_order));
    assert_eq!(candidate["swapped_pairs"], swapped_pairs);
}

#[test]
fn async_local_storage_schedule_is_safe_without_operation_evidence() {
    let source = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const store = new M();
const later = 42;
console.log(Boolean(store), later);
"#;
    let unaudited = run_fixture(split(source, "store"));
    assert_candidate_schedule(
        &unaudited,
        &[1, 2, 3, 0, 4],
        3,
        "statement_order_commutation",
    );
    assert!(
        host_contract_uses(&serde_json::to_value(unaudited.dependency_graph()).unwrap()).is_empty()
    );
    assert_host_call_remains_impure(&unaudited, "store");
    assert_preserved_entry_output(&unaudited, "true 42\n");

    let fixture = run_fixture(
        split(source, "store")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
    );
    assert_candidate_schedule(&fixture, &[1, 2, 3, 0, 4], 3, "statement_order_commutation");
    assert_host_call_remains_impure(&fixture, "store");
    assert_preserved_entry_output_with_bun(&fixture, "true 42\n");
}

#[test]
fn promise_resolve_schedule_is_safe_without_host_timing_evidence() {
    let source = r#"globalThis.saved = () => later;
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    let unaudited = run_fixture(split(source, "gate"));
    assert_candidate_schedule(&unaudited, &[0, 1, 2, 3], 0, "no_reordering");
    assert!(
        host_contract_uses(&serde_json::to_value(unaudited.dependency_graph()).unwrap()).is_empty()
    );
    assert_host_call_remains_impure(&unaudited, "gate");
    assert_preserved_entry_output(&unaudited, "true 42\n");

    let fixture = run_fixture(
        split(source, "gate").with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    assert_candidate_schedule(&fixture, &[0, 1, 2, 3], 0, "no_reordering");
    assert_host_call_remains_impure(&fixture, "gate");
    assert_preserved_entry_output_with_bun(&fixture, "true 42\n");
}

#[test]
fn audited_build_identity_and_consumed_operation_are_reported() {
    for (source, operation, emitted_order, swapped_pairs, scope) in [
        (
            r#"import { AsyncLocalStorage as Storage } from "node:async_hooks";
globalThis.saved = () => later;
const value = new Storage();
const later = 42;
console.log(Boolean(value), later);
"#,
            ReentryFreeOperation::AsyncLocalStorageConstruct,
            &[1, 2, 3, 0, 4][..],
            3,
            "statement_order_commutation",
        ),
        (
            r#"globalThis.saved = () => later;
const value = Promise.resolve();
const later = 42;
console.log(Boolean(value), later);
"#,
            ReentryFreeOperation::PromiseResolve,
            &[0, 1, 2, 3][..],
            0,
            "no_reordering",
        ),
    ] {
        let unaudited = run_fixture(split(source, "value"));
        assert_candidate_schedule(&unaudited, emitted_order, swapped_pairs, scope);
        assert!(
            host_contract_uses(&serde_json::to_value(unaudited.dependency_graph()).unwrap())
                .is_empty()
        );
        assert_host_call_remains_impure(&unaudited, "value");
        assert_preserved_entry_output(&unaudited, "true 42\n");

        let fixture = run_fixture(split(source, "value").with_host_behavior(policy(&[operation])));
        assert_candidate_schedule(&fixture, emitted_order, swapped_pairs, scope);
        assert_host_call_remains_impure(&fixture, "value");
        assert_preserved_entry_output_with_bun(&fixture, "true 42\n");
        let graph = fixture.dependency_graph();
        let uses: Vec<_> = graph
            .nodes
            .iter()
            .filter_map(|node| node.synchronous_execution.as_ref())
            .flat_map(|report| &report.author_host_contract_uses)
            .collect();
        assert_eq!(uses.len(), 1, "only the consumed operation is evidence");
        assert_eq!(uses[0].operation, operation);
        assert!(uses[0].source_location.is_some());
    }
}

#[test]
fn audited_timing_keeps_user_arguments_in_their_original_order() {
    let als = r#"import { AsyncLocalStorage as Storage } from "node:async_hooks";
globalThis.saved = () => later;
const value = new Storage({ get name() { globalThis.saved(); return "name"; } });
const later = 42;
console.log(Boolean(value), later);
"#;
    let fixture = run_fixture(split(als, "value").with_host_behavior(policy(&[
        ReentryFreeOperation::AsyncLocalStorageConstruct,
        ReentryFreeOperation::PromiseResolve,
    ])));
    assert_candidate_schedule(&fixture, &[1, 2, 3, 0, 4], 3, "statement_order_commutation");
    assert_host_call_remains_impure(&fixture, "value");
    assert!(
        host_contract_uses(&serde_json::to_value(fixture.dependency_graph()).unwrap()).is_empty()
    );
    assert_preserved_entry_error(&fixture, "ReferenceError");

    let thenable = r#"globalThis.saved = () => later;
const value = Promise.resolve({ get then() { globalThis.saved(); } });
const later = 42;
console.log(Boolean(value), later);
"#;
    let thenable_fixture = run_fixture(split(thenable, "value").with_host_behavior(policy(&[
        ReentryFreeOperation::AsyncLocalStorageConstruct,
        ReentryFreeOperation::PromiseResolve,
    ])));
    assert_candidate_schedule(&thenable_fixture, &[0, 1, 2, 3], 0, "no_reordering");
    assert_host_call_remains_impure(&thenable_fixture, "value");
    assert!(
        host_contract_uses(&serde_json::to_value(thenable_fixture.dependency_graph()).unwrap())
            .is_empty()
    );
    assert_preserved_entry_error(&thenable_fixture, "ReferenceError");
}

#[test]
fn replaced_promise_resolve_still_blocks_an_unsafe_schedule() {
    let source = r#"globalThis.saved = () => later;
Promise.resolve = () => { globalThis.saved(); };
const value = Promise.resolve();
const later = 42;
console.log(Boolean(value), later);
"#;
    let rejected = run_rejection_fixture(split(source, "value").with_host_behavior(policy(&[
        ReentryFreeOperation::AsyncLocalStorageConstruct,
        ReentryFreeOperation::PromiseResolve,
    ])));
    assert_eq!(
        rejected.candidate_relocation_assessment()["status"],
        "blocked_by_effect"
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["emitted_order"],
        serde_json::json!([0, 2, 3, 1, 4])
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["swapped_pairs"],
        2
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["host/value", "host/later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
}

#[test]
fn unused_operation_assertion_is_not_evidence() {
    let fixture = run_fixture(
        FixtureOpts::new("const value = 42; console.log(value);", vec![])
            .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    assert!(
        fixture
            .dependency_graph()
            .nodes
            .iter()
            .filter_map(|node| node.synchronous_execution.as_ref())
            .all(|report| report.author_host_contract_uses.is_empty())
    );
}

#[test]
fn node_promise_hook_is_a_real_counterexample_and_bun_stub_is_observed() {
    let source = r#"import { createHook } from "async_hooks";
let calls = 0;
const hook = createHook({ init(_id, type) { if (type === "PROMISE") calls++; } });
hook.enable();
const before = calls;
Promise.resolve();
const delta = calls - before;
hook.disable();
console.log(delta);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "1\n");
    assert_preserved_entry_output_with_bun(&fixture, "0\n");
    assert_compiled_bun_entry_output(&fixture, "0\n");
}

#[test]
fn another_host_operation_does_not_create_operation_evidence() {
    let als = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const store = new M();
const later = 42;
console.log(Boolean(store), later);
"#;
    let als_fixture = run_fixture(
        split(als, "store").with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    assert_candidate_schedule(
        &als_fixture,
        &[1, 2, 3, 0, 4],
        3,
        "statement_order_commutation",
    );
    let als_graph = serde_json::to_value(als_fixture.dependency_graph()).unwrap();
    assert!(host_contract_uses(&als_graph).is_empty());
    assert_host_call_remains_impure(&als_fixture, "store");
    assert_preserved_entry_output(&als_fixture, "true 42\n");

    let promise = r#"globalThis.saved = () => later;
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    let promise_fixture = run_fixture(
        split(promise, "gate")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
    );
    assert_candidate_schedule(&promise_fixture, &[0, 1, 2, 3], 0, "no_reordering");
    let promise_graph = serde_json::to_value(promise_fixture.dependency_graph()).unwrap();
    assert!(host_contract_uses(&promise_graph).is_empty());
    assert_host_call_remains_impure(&promise_fixture, "gate");
    assert_preserved_entry_output(&promise_fixture, "true 42\n");
}

#[test]
fn async_local_storage_local_alias_is_not_a_host_import() {
    let source = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const Alias = M;
const store = new Alias();
const later = 42;
console.log(Boolean(store), later);
"#;
    expect_atomic_conflict_rejection(
        split(source, "store")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
        &["host/value", "host/later"],
        &[],
    );
}

#[test]
fn promise_operation_evidence_is_opt_in_even_when_the_schedule_is_safe() {
    let emitted_order = &[1, 2, 4, 3, 5, 0, 6, 7, 8];
    let control = run_fixture(promise_reentry_split());
    assert_candidate_schedule(&control, emitted_order, 6, "statement_order_commutation");
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&serde_json::to_value(control.program_graph()).unwrap());
    assert!(has_unknown_call_target(node_for_binding(
        &control_graph,
        "gate"
    )));
    assert!(native_input_uses(&control_graph).is_empty());
    assert!(host_contract_uses(&control_graph).is_empty());
    assert_host_call_remains_impure(&control, "gate");
    assert_preserved_entry_output(&control, "true LaterError LaterError safe\n");

    let native_only = run_fixture(promise_reentry_split().with_native_operation_inputs(
        "static/app",
        vec![promise_input(PROMISE_REENTRY_SOURCE, "gate")],
    ));
    assert_candidate_schedule(
        &native_only,
        emitted_order,
        6,
        "statement_order_commutation",
    );
    let native_only_graph = serde_json::to_value(native_only.dependency_graph()).unwrap();
    assert!(
        native_input_uses(&native_only_graph).is_empty(),
        "native identity alone must not report an admitted invocation"
    );
    assert!(host_contract_uses(&native_only_graph).is_empty());
    assert!(has_unknown_call_target(node_for_binding(
        &native_only_graph,
        "gate"
    )));
    assert_host_call_remains_impure(&native_only, "gate");
    assert_preserved_entry_output(&native_only, "true LaterError LaterError safe\n");
    assert!(
        node_for_binding(&native_only_graph, "gate")["synchronous_execution"]["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|usage| {
                usage["conclusion"] == "native_own_data_read"
                    && usage["native_operation_input_refs"]
                        .as_array()
                        .is_some_and(|refs| !refs.is_empty())
            }),
        "the lookup identity must retain its exact author dependency"
    );

    let host_only = run_fixture(
        promise_reentry_split().with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    assert_candidate_schedule(&host_only, emitted_order, 6, "statement_order_commutation");
    let host_only_graph = serde_json::to_value(host_only.dependency_graph()).unwrap();
    assert!(native_input_uses(&host_only_graph).is_empty());
    assert!(host_contract_uses(&host_only_graph).is_empty());
    assert!(has_unknown_call_target(node_for_binding(
        &host_only_graph,
        "gate"
    )));
    assert_host_call_remains_impure(&host_only, "gate");
    assert_preserved_entry_output(&host_only, "true LaterError LaterError safe\n");

    let fixture = run_fixture(
        promise_reentry_split()
            .with_native_operation_inputs(
                "static/app",
                vec![promise_input(PROMISE_REENTRY_SOURCE, "gate")],
            )
            .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    assert_candidate_schedule(&fixture, emitted_order, 6, "statement_order_commutation");
    assert_host_call_remains_impure(&fixture, "gate");
    assert_preserved_entry_output_with_bun(&fixture, "true LaterError LaterError safe\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const entry = await import('./static/app/entry.js');\nconsole.log(Boolean(entry.gate), entry.factory().name, entry.LaterError.name);\nconst receiver = [];\nconst target = Object.getPrototypeOf(receiver);\nconst key = '__native_operation_input_probe__';\nconst before = Object.getOwnPropertyDescriptor(target, key);\ntry { entry.reflect(receiver, key); console.log(target[key]); } finally { if (before) Object.defineProperty(target, key, before); else delete target[key]; }\n",
        "true LaterError LaterError\nchanged\n",
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let native_uses = native_input_uses(&graph);
    assert_eq!(
        native_uses.len(),
        1,
        "only the selected call consumes input"
    );
    assert_eq!(native_uses[0]["evidence"]["operation"], "promise_resolve");
    assert_eq!(native_uses[0]["evidence"]["selected_binding"], "gate");
    assert_eq!(
        native_uses[0]["source_location"]["source_path"],
        "static/app.js"
    );
    let host_uses = host_contract_uses(&graph);
    assert_eq!(host_uses.len(), 1, "the timing assertion is consumed once");
    assert_eq!(host_uses[0]["operation"], "promise_resolve");

    let witness = unknown_receiver_witness(&program_graph);
    assert!(
        witness["affected_roots"]
            .as_array()
            .is_some_and(|roots| roots.iter().any(|root| root == "global_object")),
        "the author premise must not erase the original unknown receiver witness: {witness}"
    );
    assert!(
        witness["value_input_refs"]
            .as_array()
            .is_none_or(Vec::is_empty),
        "native identity is not a receiver refinement: {witness}"
    );
    assert!(
        node_for_binding(&graph, "gate")["synchronous_execution"]["native_integrity_checks"]
            .as_array()
            .is_some_and(|checks| checks.iter().any(|check| {
                check["guard"] == "host_promise_resolve"
                    && check["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| roots.iter().any(|root| root == "global_object"))
            })),
        "the input must not mint a global-object integrity fact: {graph}"
    );
}

#[test]
fn unselected_promise_resolve_keeps_unknown_call_evidence_on_a_safe_schedule() {
    let source = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst gate = Promise.resolve();\nvar other = Promise.resolve();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(Boolean(gate), Boolean(other), factory().name);\nexport { gate, other, factory, LaterError, reflect };\n";
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "gate",
                &[Member::new("gate")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("other", &[Member::new("other")]),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    )
    .with_extra_chunks(REFLECT_CHUNKS)
    .with_extra_files(REFLECT_FILES)
    .with_native_operation_inputs("static/app", vec![promise_input(source, "gate")])
    .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve]));
    let fixture = run_fixture(opts);
    assert_candidate_schedule(
        &fixture,
        &[1, 2, 3, 5, 4, 0, 6, 7],
        6,
        "statement_order_commutation",
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(has_unknown_call_target(node_for_binding(&graph, "other")));
    let uses = native_input_uses(&graph);
    assert_eq!(uses.len(), 1);
    assert_eq!(uses[0]["evidence"]["selected_binding"], "gate");
    let host_uses = host_contract_uses(&graph);
    assert_eq!(host_uses.len(), 1);
    assert_eq!(host_uses[0]["operation"], "promise_resolve");
    assert_eq!(host_uses[0]["source_location"]["start_line"], 3);
    assert_host_call_remains_impure(&fixture, "other");
    assert_preserved_entry_output(&fixture, "true true LaterError\n");
}

#[test]
fn promise_resolve_input_rejects_wrong_hash_unsupported_shapes_and_known_mutations() {
    let source = "const gate = Promise.resolve();\nexport { gate };\n";
    let mut wrong_hash = promise_input(source, "gate");
    wrong_hash.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        FixtureOpts::new(
            source,
            vec![logical_module("selected", &[Member::new("gate")])],
        )
        .with_native_operation_inputs("static/app", vec![wrong_hash]),
        &["audited_source_sha256"],
    );

    for (source, expected) in [
        (
            "const gate = Promise.resolve();\nconst Promise = function() {};\nexport { gate };\n",
            "shadowed Promise",
        ),
        (
            "const gate = Promise.resolve(1);\nexport { gate };\n",
            "unsupported Promise.resolve call shape",
        ),
        (
            "const gate = Promise.resolve(...[]);\nexport { gate };\n",
            "Promise.resolve() call",
        ),
        (
            "const gate = Promise.resolve?.();\nexport { gate };\n",
            "Promise.resolve()",
        ),
        (
            "const gate = Promise['resolve']();\nexport { gate };\n",
            "direct uncomputed Promise.resolve() call",
        ),
        (
            "const resolve = Promise.resolve;\nconst gate = resolve();\nexport { gate };\n",
            "direct uncomputed Promise.resolve() call",
        ),
        (
            "const original = globalThis.Promise;\nglobalThis.Promise = function Replacement() {};\nglobalThis.Promise = original;\nconst gate = Promise.resolve();\nexport { gate };\n",
            "global Promise replacement",
        ),
        (
            "Object.defineProperty(globalThis, 'Promise', { configurable: true, get() { return function Replacement() {}; } });\nconst gate = Promise.resolve();\nexport { gate };\n",
            "global Promise replacement",
        ),
        (
            "const original = Promise.resolve;\nPromise.resolve = function replacement() {};\nPromise.resolve = original;\nconst gate = Promise.resolve();\nexport { gate };\n",
            "Promise.resolve overwrite",
        ),
        (
            "Object.defineProperty(Promise, 'resolve', { configurable: true, get() { return function replacement() {}; } });\nconst gate = Promise.resolve();\nexport { gate };\n",
            "Promise.resolve overwrite",
        ),
        (
            "const original = Promise.resolve;\ndelete Promise.resolve;\nPromise.resolve = original;\nconst gate = Promise.resolve();\nexport { gate };\n",
            "Promise.resolve overwrite",
        ),
        (
            "const gate = Promise.resolve();\nconst original = Promise.resolve;\nPromise.resolve = function replacement() {};\nPromise.resolve = original;\nexport { gate };\n",
            "Promise.resolve overwrite",
        ),
        (
            "const original = Promise.resolve;\nfunction replaceAndRestore() { Promise.resolve = function replacement() {}; Promise.resolve = original; }\nconst gate = Promise.resolve();\nexport { gate, replaceAndRestore };\n",
            "Promise.resolve overwrite",
        ),
    ] {
        let input = promise_input(source, "gate");
        let opts = FixtureOpts::new(
            source,
            vec![logical_module("selected", &[Member::new("gate")])],
        )
        .with_native_operation_inputs("static/app", vec![input.clone()]);
        // Shape checks run before host timing; source-level mutation checks do
        // not depend on whether a runtime policy is present.
        expect_rejection_containing_all(opts, &[expected]);
    }
}

#[test]
fn promise_resolve_input_rejects_known_mutation_in_another_analyzed_chunk() {
    const MUTATION: &str = "const original = globalThis.Promise;\nglobalThis.Promise = function Replacement() {};\nglobalThis.Promise = original;\nexport {};\n";
    let source = "import './mutation.js';\nconst gate = Promise.resolve();\nexport { gate };\n";
    let opts = FixtureOpts::new(
        source,
        vec![logical_module("selected", &[Member::new("gate")])],
    )
    .with_extra_chunks(&[("static/mutation", MUTATION)])
    .with_native_operation_inputs("static/app", vec![promise_input(source, "gate")]);
    expect_rejection_containing_all(opts, &["global Promise replacement", "static/mutation"]);
}

#[test]
fn promise_resolve_local_alias_stays_conservative() {
    let source = r#"globalThis.saved = () => later;
const P = Promise;
const gate = P.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    expect_atomic_conflict_rejection(
        split(source, "gate").with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
        &["host/value", "host/later"],
        &[],
    );
}

#[test]
fn equivalent_promise_resolve_syntaxes_keep_their_safe_schedule() {
    for source in [
        r#"globalThis.saved = () => later;
const gate = Promise["resolve"]();
const later = 42;
console.log(Boolean(gate), later);
"#,
        r#"globalThis.saved = () => later;
const gate = Promise.resolve?.();
const later = 42;
console.log(Boolean(gate), later);
"#,
        r#"globalThis.saved = () => later;
const gate = Promise.resolve(...[]);
const later = 42;
console.log(Boolean(gate), later);
"#,
    ] {
        let fixture = run_fixture(
            split(source, "gate")
                .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
        );
        assert_candidate_schedule(&fixture, &[0, 1, 2, 3], 0, "no_reordering");
        assert_host_call_remains_impure(&fixture, "gate");
        assert_preserved_entry_output(&fixture, "true 42\n");
    }
}

#[test]
fn replaced_promise_resolve_wrapper_keeps_its_unsafe_schedule_blocked() {
    let source = r#"globalThis.saved = () => later;
const original = Promise.resolve;
Promise.resolve = () => { globalThis.saved(); return original.call(Promise); };
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    let rejected = run_rejection_fixture(
        split(source, "gate").with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    let candidate = rejected.candidate_relocation_assessment();
    assert_eq!(candidate["status"], "blocked_by_effect");
    assert_eq!(
        candidate["emitted_order"],
        serde_json::json!([1, 0, 3, 4, 2, 5])
    );
    assert_eq!(candidate["swapped_pairs"], 3);
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["host/value", "host/later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
}
