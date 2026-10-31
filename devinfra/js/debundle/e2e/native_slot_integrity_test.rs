//! A census write invalidates only the intrinsic slots it can reach: a known
//! key on any receiver reaches only that key, and an exact constructor or
//! global-object receiver reaches only its own properties.

use debundle_e2e_support::*;

fn opts<'a>(source: &'a str, member: &'static str, anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/native", &[Member::new(member)], anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

/// `mutations` run first, then the native operation declaring `member`; the
/// published callback reads `later` before its initialization.
fn source(mutations: &[&str], declaration: &str, output: &str) -> String {
    format!(
        "globalThis.saved = () => later;\n{}\n{declaration}\nconst later = 42;\nconsole.log({output}, later);\n",
        mutations.join("\n")
    )
}

fn anonymous<'a>(mutations: &[&'a str]) -> Vec<&'a str> {
    [&["globalThis.saved = () => later;"], mutations].concat()
}

fn assert_split(
    mutations: &[&str],
    declaration: &str,
    member: &'static str,
    output: &str,
    stdout: &str,
) {
    let source = source(mutations, declaration, output);
    let fixture = run_fixture(opts(&source, member, &anonymous(mutations)));
    assert_preserved_entry_output(&fixture, stdout);
    assert_all_emitted_js_checks(&fixture);
}

/// Refused because the native operation itself may run user code: its
/// source unit's synchronous execution is incomplete.
fn assert_refused(
    mutations: &[&str],
    declaration: &str,
    member: &'static str,
    output: &str,
    stdout: &str,
) {
    let source = source(mutations, declaration, output);
    let rejected = run_rejection_fixture(opts(&source, member, &anonymous(mutations)));
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["values/native", "values/later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    let node = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["declared_bindings"][0]["binding"] == member)
        .expect("native source unit");
    assert_eq!(node["synchronous_execution"]["complete"], false, "{node}");
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_preserved_entry_output(&fixture, stdout);
}

const MAP: &str = "const collection = new Map();";
const SAME: &str = "const same = Object.is(1, 1);";
const SET: &str = "const collection = new Set();";
const SYMBOL: &str = "const marker = Symbol(\"m\");";

#[test]
fn known_key_through_global_slot_value_keeps_other_globals() {
    assert_split(
        &[
            "globalThis.config = { flag: false };",
            "globalThis.config.flag = true;",
        ],
        MAP,
        "collection",
        "collection.size",
        "0 42\n",
    );
}

#[test]
fn same_key_through_global_slot_value_replaces_the_global() {
    assert_refused(
        &[
            "globalThis.box = globalThis;",
            "globalThis.box.Map = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };",
        ],
        MAP,
        "collection",
        "collection.size",
        "ReferenceError 42\n",
    );
}

#[test]
fn unknown_key_through_global_slot_value_reaches_every_slot() {
    assert_refused(
        &[
            "globalThis.box = Object;",
            "globalThis.box[[\"i\", \"s\"].join(\"\")] = function is() { try { globalThis.saved(); return \"ready\"; } catch (error) { return error.name; } };",
        ],
        SAME,
        "same",
        "same",
        "ReferenceError 42\n",
    );
}

#[test]
fn constructor_static_write_keeps_other_statics() {
    assert_split(
        &["Object.entries = function entries() { return []; };"],
        SAME,
        "same",
        "same",
        "true 42\n",
    );
}

#[test]
fn constructor_static_write_replaces_that_static() {
    assert_refused(
        &[
            "Object.is = function is() { try { globalThis.saved(); return \"ready\"; } catch (error) { return error.name; } };",
        ],
        SAME,
        "same",
        "same",
        "ReferenceError 42\n",
    );
}

#[test]
fn accessor_install_on_the_static_runs_on_read() {
    assert_refused(
        &[
            "Object.defineProperty(Object, \"is\", { get() { try { globalThis.saved(); return () => \"ready\"; } catch (error) { return () => error.name; } } });",
        ],
        SAME,
        "same",
        "same",
        "ReferenceError 42\n",
    );
}

#[test]
fn rebound_global_object_alias_replaces_a_static() {
    assert_refused(
        &[
            "globalThis = Object;",
            "globalThis.is = function is() { try { return String(later); } catch (error) { return error.name; } };",
        ],
        SAME,
        "same",
        "same",
        "ReferenceError 42\n",
    );
}

#[test]
fn global_object_write_before_rebinding_its_alias_replaces_the_global() {
    assert_refused(
        &[
            "globalThis.Map = function Map() { try { return { size: String(later) }; } catch (error) { return { size: error.name }; } };",
            "globalThis = Object;",
        ],
        MAP,
        "collection",
        "collection.size",
        "ReferenceError 42\n",
    );
}

#[test]
fn prototype_write_keeps_constructor_statics() {
    assert_split(
        &["Object.prototype.extra = 1;"],
        SAME,
        "same",
        "same",
        "true 42\n",
    );
}

#[test]
fn constructor_static_polyfill_keeps_the_global_binding() {
    assert_split(
        &["Symbol.dispose ??= Symbol(\"Symbol.dispose\");"],
        SYMBOL,
        "marker",
        "marker.description",
        "m 42\n",
    );
}

#[test]
fn prototype_writes_keep_empty_collection_construction() {
    // `new Set()` never reads the adder it would call for an iterable.
    assert_split(
        &["Set.prototype.add = function add() { globalThis.saved(); return this; };"],
        SET,
        "collection",
        "collection.size",
        "0 42\n",
    );
    assert_split(
        &["Map.prototype.extra = 1;"],
        MAP,
        "collection",
        "collection.size",
        "0 42\n",
    );
}

#[test]
fn symbol_keyed_constructor_write_keeps_the_global_binding() {
    assert_split(
        &["Map[Symbol.for(\"registry\")] = 1;"],
        MAP,
        "collection",
        "collection.size",
        "0 42\n",
    );
}

#[test]
fn affixed_string_keys_keep_slots_they_cannot_name() {
    for mutations in [
        &[
            "globalThis.storage = {};",
            "globalThis.storage[`migration.${1 + 1}`] = 1;",
        ][..],
        &["globalThis[\"pLPCommand\" + (1 + 1)] = 1;"][..],
        &["globalThis[(1 + 1) + \"-callback\"] = 1;"][..],
        &["globalThis[Symbol.for(\"registry.\" + (1 + 1))] = 1;"][..],
    ] {
        assert_split(mutations, MAP, "collection", "collection.size", "0 42\n");
    }
}

#[test]
fn affixed_string_keys_reach_slots_they_may_name() {
    for mutations in [
        &[
            "globalThis[\"M\" + \"ap\"] = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };",
        ][..],
        &[
            "globalThis[`${[\"M\"][0]}ap`] = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };",
        ][..],
    ] {
        assert_refused(
            mutations,
            MAP,
            "collection",
            "collection.size",
            "ReferenceError 42\n",
        );
    }
}
