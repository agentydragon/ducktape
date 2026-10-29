//! Exact Bun host timing assertions stay opt-in and shape-specific.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

fn policy(operations: &[HostNoReentryOperation]) -> HostNoReentry {
    HostNoReentry {
        runtime: HostNoReentryRuntime::BunStandalone1_3_10,
        operations: operations.iter().copied().collect::<BTreeSet<_>>(),
        node_builtin_module_leaves: BTreeSet::new(),
    }
}

fn build_policy(operations: &[HostNoReentryOperation]) -> HostNoReentry {
    let mut policy = policy(operations);
    // Synthetic identity exercises assertion transport, not host authentication.
    // The test's actual Bun runtime is supplied independently by Bazel.
    policy.runtime = HostNoReentryRuntime::BunStandaloneBuild {
        version: "1.3.10".to_string(),
        revision: "a".repeat(40),
        executable_sha256: "b".repeat(64),
    };
    policy
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

fn unknown_receiver_witness(graph: &Value) -> &Value {
    graph["native_integrity_witnesses"]
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
        .unwrap_or_else(|| panic!("reflective helper's unknown receiver is absent: {graph}"))
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

#[test]
fn async_local_storage_constructor_requires_exact_bun_contract() {
    let source = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const store = new M();
const later = 42;
console.log(Boolean(store), later);
"#;
    expect_atomic_conflict_rejection(split(source, "store"), &["host/value", "host/later"], &[]);
    let fixture = run_fixture(split(source, "store").with_host_no_reentry(policy(&[
        HostNoReentryOperation::AsyncLocalStorageConstruct,
    ])));
    assert_host_call_remains_impure(&fixture, "store");
    assert_preserved_entry_output_with_bun(&fixture, "true 42\n");
}

#[test]
fn promise_resolve_requires_exact_bun_contract() {
    let source = r#"globalThis.saved = () => later;
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    expect_atomic_conflict_rejection(split(source, "gate"), &["host/value", "host/later"], &[]);
    let fixture = run_fixture(
        split(source, "gate")
            .with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve])),
    );
    assert_host_call_remains_impure(&fixture, "gate");
    assert_preserved_entry_output_with_bun(&fixture, "true 42\n");
}

#[test]
fn audited_build_identity_and_consumed_operation_are_reported() {
    for (source, operation) in [
        (
            r#"import { AsyncLocalStorage as Storage } from "node:async_hooks";
globalThis.saved = () => later;
const value = new Storage();
const later = 42;
console.log(Boolean(value), later);
"#,
            HostNoReentryOperation::AsyncLocalStorageConstruct,
        ),
        (
            r#"globalThis.saved = () => later;
const value = Promise.resolve();
const later = 42;
console.log(Boolean(value), later);
"#,
            HostNoReentryOperation::PromiseResolve,
        ),
    ] {
        expect_atomic_conflict_rejection(
            split(source, "value"),
            &["host/value", "host/later"],
            &[],
        );
        let policy = build_policy(&[operation]);
        let fixture = run_fixture(split(source, "value").with_host_no_reentry(policy.clone()));
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
        assert_eq!(uses[0].runtime, policy.runtime);
        assert_eq!(uses[0].operation, operation);
        assert!(uses[0].source_location.is_some());
    }
}

#[test]
fn audited_build_does_not_certify_arguments_or_replaced_promise() {
    for source in [
        r#"import { AsyncLocalStorage as Storage } from "node:async_hooks";
globalThis.saved = () => later;
const value = new Storage({ get name() { globalThis.saved(); return "name"; } });
const later = 42;
console.log(Boolean(value), later);
"#,
        r#"globalThis.saved = () => later;
const value = Promise.resolve({ get then() { globalThis.saved(); } });
const later = 42;
console.log(Boolean(value), later);
"#,
        r#"globalThis.saved = () => later;
Promise.resolve = () => { globalThis.saved(); };
const value = Promise.resolve();
const later = 42;
console.log(Boolean(value), later);
"#,
    ] {
        expect_atomic_conflict_rejection(
            split(source, "value").with_host_no_reentry(build_policy(&[
                HostNoReentryOperation::AsyncLocalStorageConstruct,
                HostNoReentryOperation::PromiseResolve,
            ])),
            &["host/value", "host/later"],
            &[],
        );
    }
}

#[test]
fn invalid_build_identity_is_rejected_and_unused_policy_is_not_evidence() {
    let source = "const value = 42; console.log(value);";
    let mut policy = build_policy(&[HostNoReentryOperation::PromiseResolve]);
    if let HostNoReentryRuntime::BunStandaloneBuild { revision, .. } = &mut policy.runtime {
        *revision = "not-a-revision".to_string();
    }
    expect_rejection_containing_all(
        FixtureOpts::new(source, vec![]).with_host_no_reentry(policy),
        &["bun_standalone_build.revision", "40 hexadecimal characters"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_host_no_reentry(build_policy(&[HostNoReentryOperation::PromiseResolve])),
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
fn one_host_operation_does_not_enable_the_other() {
    let als = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const store = new M();
const later = 42;
console.log(Boolean(store), later);
"#;
    expect_atomic_conflict_rejection(
        split(als, "store").with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve])),
        &["host/value", "host/later"],
        &[],
    );
    let promise = r#"globalThis.saved = () => later;
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    expect_atomic_conflict_rejection(
        split(promise, "gate").with_host_no_reentry(policy(&[
            HostNoReentryOperation::AsyncLocalStorageConstruct,
        ])),
        &["host/value", "host/later"],
        &[],
    );
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
        split(source, "store").with_host_no_reentry(policy(&[
            HostNoReentryOperation::AsyncLocalStorageConstruct,
        ])),
        &["host/value", "host/later"],
        &[],
    );
}

#[test]
fn promise_native_input_and_host_timing_are_both_required_at_the_selected_site() {
    let control = run_rejection_fixture(promise_reentry_split());
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&control_graph);
    assert!(
        control.atomic_group_conflicts().iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["gate", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "the opaque receiver witness must create the baseline split conflict"
    );
    assert!(has_unknown_call_target(node_for_binding(
        &control_graph,
        "gate"
    )));

    let native_only = run_rejection_fixture(promise_reentry_split().with_native_operation_inputs(
        "static/app",
        vec![promise_input(PROMISE_REENTRY_SOURCE, "gate")],
    ));
    assert!(
        native_only.atomic_group_conflicts().iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["gate", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "native identity alone must retain the synthetic conflict"
    );
    assert!(
        native_input_uses(&serde_json::to_value(native_only.dependency_graph()).unwrap())
            .is_empty(),
        "native identity alone must not consume the input or suppress reentry"
    );

    expect_atomic_conflict_rejection(
        promise_reentry_split()
            .with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve])),
        &["gate", "later"],
        &[],
    );

    let fixture = run_fixture(
        promise_reentry_split()
            .with_native_operation_inputs(
                "static/app",
                vec![promise_input(PROMISE_REENTRY_SOURCE, "gate")],
            )
            .with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve])),
    );
    assert_host_call_remains_impure(&fixture, "gate");
    assert_preserved_entry_output_with_bun(&fixture, "true LaterError LaterError safe\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const entry = await import('./static/app/entry.js');\nconsole.log(Boolean(entry.gate), entry.factory().name, entry.LaterError.name);\nconst receiver = [];\nconst target = Object.getPrototypeOf(receiver);\nconst key = '__native_operation_input_probe__';\nconst before = Object.getOwnPropertyDescriptor(target, key);\ntry { entry.reflect(receiver, key); console.log(target[key]); } finally { if (before) Object.defineProperty(target, key, before); else delete target[key]; }\n",
        "true LaterError LaterError\nchanged\n",
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
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

    let witness = unknown_receiver_witness(&graph);
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
fn unselected_promise_resolve_keeps_its_own_reentry_fallback() {
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
    .with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve]));
    let rejected = run_rejection_fixture(opts);
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    assert!(
        rejected.atomic_group_conflicts().iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["other", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "the unselected call must retain the atomic conflict: {:#?}",
        rejected.atomic_group_conflicts()
    );
    assert!(has_unknown_call_target(node_for_binding(&graph, "other")));
    let uses = native_input_uses(&graph);
    assert_eq!(uses.len(), 1);
    assert_eq!(uses[0]["evidence"]["selected_binding"], "gate");
    assert_eq!(host_contract_uses(&graph).len(), 1);
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
fn promise_resolve_rejects_member_alias_argument_and_mutation() {
    for source in [
        r#"globalThis.saved = () => later;
const P = Promise;
const gate = P.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#,
        r#"globalThis.saved = () => later;
const gate = Promise["resolve"]();
const later = 42;
console.log(Boolean(gate), later);
"#,
        r#"globalThis.saved = () => later;
const gate = Promise.resolve({ get then() { globalThis.saved(); return undefined; } });
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
        r#"globalThis.saved = () => later;
const original = Promise.resolve;
Promise.resolve = () => { globalThis.saved(); return original.call(Promise); };
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#,
    ] {
        expect_atomic_conflict_rejection(
            split(source, "gate")
                .with_host_no_reentry(policy(&[HostNoReentryOperation::PromiseResolve])),
            &["host/value", "host/later"],
            &[],
        );
    }
}
