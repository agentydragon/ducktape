//! Exact Bun host timing assertions stay opt-in and shape-specific.

use debundle_e2e_support::*;
use std::collections::BTreeSet;

fn policy(operations: &[HostNoReentryOperation]) -> HostNoReentry {
    HostNoReentry {
        runtime: HostNoReentryRuntime::BunStandalone1_3_10,
        operations: operations.iter().copied().collect::<BTreeSet<_>>(),
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
