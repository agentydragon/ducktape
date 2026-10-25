//! Map entry callbacks remain deferred during ordinary construction.

use debundle_e2e_support::*;

fn split_collection_later(source: &'static str, collection: &[Member]) -> FixtureOpts<'static> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("values/collection", collection),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn assert_collection_requires_later(source: &'static str, collection: &[Member]) {
    expect_atomic_conflict_rejection(
        split_collection_later(source, collection),
        &["values/collection", "values/later"],
        &[],
    );
}

#[test]
fn inline_map_entry_callback_does_not_run_at_construction() {
    let fixture = run_fixture(split_collection_later(
        r#"const registry = new Map([["read", () => later]]);
const later = 42;
console.log(registry.get("read")());
export { registry, later };
"#,
        &[Member::new("registry")],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn map_member_call_can_invoke_stored_value_callback_early() {
    assert_collection_requires_later(
        r#"const registry = new Map([["read", () => later]]);
const result = registry.get("read")();
const later = 42;
export { registry, result, later };
"#,
        &[Member::new("registry"), Member::new("result")],
    );
}

#[test]
fn opaque_consumer_can_invoke_stored_value_callback() {
    assert_collection_requires_later(
        r#"const registry = new Map([["read", () => later]]);
const result = opaqueConsumer(registry);
const later = 42;
export { registry, result, later };
"#,
        &[Member::new("registry"), Member::new("result")],
    );
}

#[test]
fn opaque_consumer_can_invoke_stored_key_callback() {
    assert_collection_requires_later(
        r#"const registry = new Map([[() => later, "value"]]);
const result = opaqueConsumer(registry);
const later = 42;
export { registry, result, later };
"#,
        &[Member::new("registry"), Member::new("result")],
    );
}

#[test]
fn entry_expression_calls_still_run_during_construction() {
    assert_collection_requires_later(
        r#"function readKey() { return later; }
function readValue() { return later; }
const registry = new Map([[readKey(), readValue()]]);
const later = 42;
export { readKey, readValue, registry, later };
"#,
        &[
            Member::new("readKey"),
            Member::new("readValue"),
            Member::new("registry"),
        ],
    );
}

#[test]
fn custom_iterable_keeps_the_opaque_constructor_path() {
    assert_collection_requires_later(
        r#"globalThis.saved = () => later;
const items = { [Symbol.iterator]() { globalThis.saved(); return [][Symbol.iterator](); } };
const registry = new Map(items);
const later = 42;
export { items, registry, later };
"#,
        &[Member::new("items"), Member::new("registry")],
    );
}

#[test]
fn getter_entry_keeps_the_opaque_constructor_path() {
    assert_collection_requires_later(
        r#"globalThis.saved = () => later;
const registry = new Map([{ get 0() { globalThis.saved(); return "read"; }, 1: "value" }]);
const later = 42;
export { registry, later };
"#,
        &[Member::new("registry")],
    );
}

#[test]
fn shadowed_map_constructor_keeps_the_opaque_call_path() {
    assert_collection_requires_later(
        r#"globalThis.saved = () => later;
function Map() { globalThis.saved(); return new globalThis.Map(); }
const registry = new Map([["read", () => later]]);
const later = 42;
export { Map, registry, later };
"#,
        &[Member::new("Map"), Member::new("registry")],
    );
}

#[test]
fn reassigned_map_constructor_keeps_the_opaque_call_path() {
    assert_collection_requires_later(
        r#"globalThis.saved = () => later;
Map = function() { globalThis.saved(); return new globalThis.Map(); };
const registry = new Map([["read", () => later]]);
const later = 42;
export { registry, later };
"#,
        &[Member::new("registry")],
    );
}

#[test]
fn lazy_eval_disables_map_entry_proof_across_owners() {
    assert_collection_requires_later(
        r#"function makeRegistry() {
  eval("1");
  return new Map([["read", () => later]]);
}
const registry = makeRegistry();
const later = 42;
export { makeRegistry, registry, later };
"#,
        &[Member::new("makeRegistry"), Member::new("registry")],
    );
}

#[test]
fn failed_map_entry_evaluation_keeps_the_tdz_order() {
    let source = r#"function opaqueConsumer(value) { globalThis.saved = value; }
const result = "ok";
try { opaqueConsumer(new Map([[missing, () => later]])); }
catch {}
const missing = "key";
const later = 42;
console.log(result, globalThis.saved === undefined);
export { result, missing, later };
"#;
    let colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "runtime/all",
            &[
                Member::new("opaqueConsumer"),
                Member::new("result"),
                Member::new("missing"),
                Member::new("later"),
            ],
            &[
                "try { opaqueConsumer(new Map([[missing, () => later]])); } catch {}",
                "console.log(result, globalThis.saved === undefined);",
            ],
        )],
    )
    .with_unassigned_mode(unassigned_mode_inline());
    assert_preserved_entry_output(&run_fixture(colocated), "ok true\n");
    expect_atomic_conflict_rejection(
        split_collection_later(source, &[Member::new("result"), Member::new("missing")]),
        &["anon_residual_sentinel", "values/collection"],
        &[],
    );
}
