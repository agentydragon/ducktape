//! A census write invalidates only the intrinsic slots it can reach: a known
//! key on any receiver reaches only that key, and an exact constructor or
//! global-object receiver reaches only its own properties.

use debundle_e2e_graph_support::FixtureGraphExt;
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

fn assert_unresolved_native(
    mutations: &[&str],
    declaration: &str,
    member: &'static str,
    output: &str,
    stdout: &str,
) {
    let source = source(mutations, declaration, output);
    let fixture = run_fixture(opts(&source, member, &anonymous(mutations)));
    assert_eq!(
        fixture.candidate_relocation_assessment()["decision"]["scope"],
        "no_reordering"
    );
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == member)
        })
        .expect("native source unit");
    assert!(
        !node.synchronous_execution.as_ref().unwrap().complete,
        "{node:#?}"
    );
    assert_preserved_entry_output(&fixture, stdout);
}

const MAP: &str = "const collection = new Map();";
const SAME: &str = "const same = Object.is(1, 1);";
const SET: &str = "const collection = new Set();";
const SYMBOL: &str = "const marker = Symbol(\"m\");";
const GLOBAL_MAP_REPLACEMENT: &[&str] = &[
    "globalThis.box = globalThis;",
    "globalThis.box.Map = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };",
];

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
    assert_unresolved_native(
        GLOBAL_MAP_REPLACEMENT,
        MAP,
        "collection",
        "collection.size",
        "ReferenceError 42\n",
    );
}

#[test]
fn unresolved_native_module_cannot_interleave_around_initialization() {
    let source = source(GLOBAL_MAP_REPLACEMENT, MAP, "collection.size");
    let mut statements = anonymous(GLOBAL_MAP_REPLACEMENT);
    statements.push("console.log(collection.size, later);");
    let rejected = run_rejection_fixture(opts(&source, "collection", &statements));
    let graph = rejected.dependency_graph();
    let assessment = rejected.candidate_relocation_assessment();
    let order = assessment["emitted_order"].as_array().unwrap();
    let binding_ordinal = |binding: &str| {
        graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .unwrap()
            .statement_ordinal
            .0
    };
    let position = |ordinal: usize| {
        order
            .iter()
            .position(|unit| unit.as_u64() == Some(ordinal as u64))
            .unwrap()
    };
    let output = graph
        .nodes
        .iter()
        .map(|node| node.statement_ordinal.0)
        .max()
        .unwrap();
    // The call and output share a module, so one must cross initialization.
    assert!(
        !(position(binding_ordinal("collection")) < position(binding_ordinal("later"))
            && position(binding_ordinal("later")) < position(output)),
        "{assessment:#}"
    );
    assert!(matches!(
        assessment["decision"]["status"].as_str(),
        Some("conflict" | "incomplete")
    ));
}

#[test]
fn unknown_key_through_global_slot_value_reaches_every_slot() {
    assert_unresolved_native(
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
    assert_unresolved_native(
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
    assert_unresolved_native(
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
    assert_unresolved_native(
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
    assert_unresolved_native(
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
        assert_unresolved_native(
            mutations,
            MAP,
            "collection",
            "collection.size",
            "ReferenceError 42\n",
        );
    }
}
