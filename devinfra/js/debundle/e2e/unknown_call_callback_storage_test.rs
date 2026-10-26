//! Callback values retained by globals, objects, and same-source_unit assignments
//! remain observable at an opaque call's source prefix.

use debundle_e2e_support::*;

fn split_later(source: &'static str, callsite_bindings: &[&'static str]) -> FixtureOpts<'static> {
    let callsite_members: Vec<_> = callsite_bindings
        .iter()
        .map(|name| Member::new(name))
        .collect();
    FixtureOpts::new(
        source,
        vec![
            logical_module("callbacks/invocation", &callsite_members),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn assert_later_is_atomic_with_callsite(source: &'static str, callsite_bindings: &[&'static str]) {
    expect_atomic_conflict_rejection(
        split_later(source, callsite_bindings),
        &["callbacks/invocation", "values/later"],
        &[],
    );
}

#[test]
fn global_callback_is_observed_by_a_later_bare_opaque_call() {
    assert_later_is_atomic_with_callsite(
        r#"const callback = () => later;
globalThis.saved = callback;
const result = host();
const later = 42;
export { result, later };
"#,
        &["result"],
    );
}

#[test]
fn nested_callback_argument_is_observed_by_opaque_call() {
    assert_later_is_atomic_with_callsite(
        r#"const result = host({ nested: { read: () => later } });
const later = 42;
export { result, later };
"#,
        &["result"],
    );
}

#[test]
fn callback_survives_object_spread_before_opaque_call() {
    assert_later_is_atomic_with_callsite(
        r#"const payload = { nested: { read: () => later } };
const forwarded = { ...payload };
const result = host(forwarded);
const later = 42;
export { result, later };
"#,
        &["result"],
    );
}

const SAME_SOURCE_UNIT_SOURCE: &str = r#"var reader;
globalThis.saved = () => reader();
globalThis.host = () => {
  try { globalThis.saved(); }
  catch (error) { globalThis.observed = error.name; }
};
const outcome = (reader = () => later, host());
const later = 42;
console.log(globalThis.observed);
export { outcome, later };
"#;

#[test]
fn same_source_unit_assignment_is_visible_to_unknown_call_callback() {
    assert_later_is_atomic_with_callsite(SAME_SOURCE_UNIT_SOURCE, &["outcome", "reader"]);
}

#[test]
fn assignment_before_opaque_call_publishes_the_exported_var_callback() {
    const PEER: &str = r#"import { reader } from "./app.js";
globalThis.host = () => {
  try { globalThis.observed = reader(); }
  catch (error) { globalThis.observed = error.name; }
};
export {};
"#;
    const PROVIDER: &str = r#"import "./peer.js";
export var reader;
const outcome = (reader = () => later, host());
const later = 42;
console.log(globalThis.observed);
export { outcome, later };
"#;
    let peer = [("static/peer", PEER)];
    let peer_modules = [("static/peer", Vec::new())];
    let fixture = run_fixture(
        FixtureOpts::new(PROVIDER, vec![])
            .with_extra_chunks(&peer)
            .with_extra_chunk_logical_modules(&peer_modules),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        split_later(PROVIDER, &["reader", "outcome"]).with_extra_chunks(&peer),
        &["callbacks/invocation", "values/later"],
        &[],
    );
}

#[test]
fn provider_peer_cycle_rejects_split_and_colocation_preserves_tdz() {
    const PEER: &str = r#"import "./app.js";
globalThis.host = () => {
  try { globalThis.saved(); }
  catch (error) { globalThis.observed = error.name; }
};
export {};
"#;
    const PROVIDER: &str = r#"import "./peer.js";
globalThis.saved = () => later;
const outcome = host();
const later = 42;
console.log(globalThis.observed);
export { outcome, later };
"#;

    let peer = [("static/peer", PEER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            PROVIDER,
            vec![
                logical_module("callbacks/invocation", &[Member::new("outcome")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peer),
        &["callbacks/invocation", "values/later"],
        &[],
    );

    let peer_modules = [("static/peer", Vec::new())];
    let fixture = run_fixture(
        FixtureOpts::new(PROVIDER, vec![])
            .with_extra_chunks(&peer)
            .with_extra_chunk_logical_modules(&peer_modules),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    assert_all_emitted_js_checks(&fixture);
}
