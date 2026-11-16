//! Audited `host_behavior.reentry_free_operations` stay opt-in and shape-specific.

use debundle_e2e_graph_support::reflective_provider::{
    REFLECT_CHUNKS, REFLECT_FILES, unknown_receiver_witness,
};
use debundle_e2e_graph_support::{
    FixtureGraphExt, json_has_unknown_call_target, json_node_declaring,
};
use debundle_e2e_support::*;

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

fn split_with_publication_phase<'a>(source: &'a str, binding: &'static str) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("host/value", &[Member::new(binding)]),
            logical_module_with_anon(
                "host/later",
                &[Member::new("later")],
                &["globalThis.saved = () => later;"],
            ),
        ],
    )
}

fn colocated_with_publication<'a>(source: &'a str, binding: &'static str) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "host/all",
            &[Member::new(binding), Member::new("later")],
            &["globalThis.saved = () => later;"],
        )],
    )
}

const PROMISE_REENTRY_SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst gate = Promise.resolve();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst laterValue = factory();\nglobalThis.unrelatedKey = 'safe';\nconsole.log(Boolean(gate), laterValue.name, LaterError.name, globalThis.unrelatedKey);\nexport { gate, factory, LaterError, reflect };\n";

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
    let rejected = assert_conservative_schedule_refusal(
        split_with_publication_phase(source, "store")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
        "evaluation_schedule_unknown_reordered",
        "true 42\n",
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    let fixture = run_fixture_with_graphs(
        colocated_with_publication(source, "store")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
    );
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
    let rejected = assert_conservative_schedule_refusal(
        split_with_publication_phase(source, "gate")
            .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
        "evaluation_schedule_unknown_reordered",
        "true 42\n",
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    let fixture = run_fixture_with_graphs(
        colocated_with_publication(source, "gate")
            .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
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
            ReentryFreeOperation::AsyncLocalStorageConstruct,
        ),
        (
            r#"globalThis.saved = () => later;
const value = Promise.resolve();
const later = 42;
console.log(Boolean(value), later);
"#,
            ReentryFreeOperation::PromiseResolve,
        ),
    ] {
        expect_atomic_conflict_rejection(
            split(source, "value"),
            &["host/value", "host/later"],
            &[],
        );
        let rejected = assert_conservative_schedule_refusal(
            split_with_publication_phase(source, "value").with_host_behavior(policy(&[operation])),
            "evaluation_schedule_unknown_reordered",
            "true 42\n",
        );
        assert_eq!(
            rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
            true
        );
        let fixture = run_fixture_with_graphs(
            colocated_with_publication(source, "value").with_host_behavior(policy(&[operation])),
        );
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
            split(source, "value").with_host_behavior(policy(&[
                ReentryFreeOperation::AsyncLocalStorageConstruct,
                ReentryFreeOperation::PromiseResolve,
            ])),
            &["host/value", "host/later"],
            &[],
        );
    }
}

#[test]
fn unused_operation_assertion_is_not_evidence() {
    let fixture = run_fixture_with_graphs(
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
fn one_host_operation_does_not_enable_the_other() {
    let als = r#"import { AsyncLocalStorage as M } from "async_hooks";
globalThis.saved = () => later;
const store = new M();
const later = 42;
console.log(Boolean(store), later);
"#;
    expect_atomic_conflict_rejection(
        split(als, "store").with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
        &["host/value", "host/later"],
        &[],
    );
    let promise = r#"globalThis.saved = () => later;
const gate = Promise.resolve();
const later = 42;
console.log(Boolean(gate), later);
"#;
    expect_atomic_conflict_rejection(
        split(promise, "gate")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
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
        split(source, "store")
            .with_host_behavior(policy(&[ReentryFreeOperation::AsyncLocalStorageConstruct])),
        &["host/value", "host/later"],
        &[],
    );
}

#[test]
fn promise_host_timing_requires_proved_native_identity() {
    let rejected = run_rejection_fixture_with_graphs(
        promise_reentry_split().with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
    );
    unknown_receiver_witness(&serde_json::to_value(rejected.program_graph()).unwrap());
    assert!(
        rejected.atomic_group_conflicts().iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["gate", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "opaque native identity must retain the split conflict under the host timing assertion"
    );
    assert!(json_has_unknown_call_target(json_node_declaring(
        &serde_json::to_value(rejected.dependency_graph()).unwrap(),
        "gate"
    )));
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
                .with_host_behavior(policy(&[ReentryFreeOperation::PromiseResolve])),
            &["host/value", "host/later"],
            &[],
        );
    }
}
