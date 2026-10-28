//! Exact Bun host timing assertions stay opt-in and shape-specific.

use debundle_e2e_support::*;
use std::collections::BTreeSet;

fn policy(operations: &[HostNoReentryOperation]) -> HostNoReentry {
    HostNoReentry {
        runtime: HostNoReentryRuntime::BunStandalone1_3_10,
        operations: operations.iter().copied().collect::<BTreeSet<_>>(),
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
