//! A global property that analyzed code only ever fills with fresh objects
//! holds no intrinsic under `predefined_globals: standard`, so unknown-key
//! writes through its value reach no native slot.

use debundle_e2e_support::*;
use sha2::{Digest, Sha256};

/// `mutations` are anonymous statements and `members` the declarations of
/// the bundle code; `new Map()` follows and a published callback reads
/// `later`.
fn opts<'a>(
    source: &'a str,
    members: &[&'static str],
    anonymous: &[&str],
    standard: bool,
) -> FixtureOpts<'a> {
    let mut native: Vec<Member> = members.iter().map(|member| Member::new(member)).collect();
    native.push(Member::new("collection"));
    let mut anonymous = anonymous.to_vec();
    anonymous.push("globalThis.saved = () => later;");
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/native", &native, &anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    );
    if standard {
        opts.host_behavior.predefined_globals = PredefinedGlobals::Standard;
    }
    opts
}

fn source(bundle: &str) -> String {
    format!(
        "globalThis.saved = () => later;\n{bundle}\nconst collection = new Map();\nconst later = 42;\nconsole.log(collection.size, later);\n"
    )
}

const SENTRY: &str = "function register(n, value) { var e = typeof window != \"undefined\" ? window : typeof global != \"undefined\" ? global : typeof globalThis != \"undefined\" ? globalThis : typeof self != \"undefined\" ? self : {}; e._sentryDebugIds = e._sentryDebugIds || {}; e._sentryDebugIds[n] = value; }";
const OTEL: &str = "const root = typeof globalThis == \"object\" ? globalThis : typeof self == \"object\" ? self : {};\nconst key = Symbol.for(\"registry.api.1\");\nfunction register(type, value) { var registry = (root[key] = root[key] ?? { version: \"1\" }); registry[type] = value; return true; }\nfunction unregister(type) { var registry = root[key]; registry && delete registry[type]; }";
const ANALYTICS: &str = "const root = typeof globalThis < \"u\" ? globalThis : self;\nfunction getInstance(name) { root.analyticsConnectorInstances || (root.analyticsConnectorInstances = {}); root.analyticsConnectorInstances[name] || (root.analyticsConnectorInstances[name] = { name }); return root.analyticsConnectorInstances[name]; }";
const EXPERIMENTS: &str = "const root = typeof globalThis < \"u\" ? globalThis : self;\nroot.experimentInstances = {};\nvar instances = root.experimentInstances;\nfunction create(name) { instances[name] || (instances[name] = { name }); return instances[name]; }";

const COLLECTION_REGISTRY: &str = "const root = typeof globalThis < \"u\" ? globalThis : {};\nconst contextKey = Symbol.for(\"context-registry\");\nfunction contexts() { root[contextKey] ??= new Map(); return root[contextKey]; }\nfunction set(name, value) { root[contextKey][name] = value; }";

fn cases() -> [(&'static str, Vec<&'static str>, Vec<&'static str>); 5] {
    [
        (SENTRY, vec!["register"], vec![]),
        (OTEL, vec!["root", "key", "register", "unregister"], vec![]),
        (ANALYTICS, vec!["root", "getInstance"], vec![]),
        (
            EXPERIMENTS,
            vec!["root", "instances", "create"],
            vec!["root.experimentInstances = {};"],
        ),
        (
            COLLECTION_REGISTRY,
            vec!["root", "contextKey", "contexts", "set"],
            vec![],
        ),
    ]
}

#[test]
fn bundle_owned_globals_keep_native_slots() {
    for (bundle, members, anonymous) in cases() {
        let source = source(bundle);
        let fixture = run_fixture(opts(&source, &members, &anonymous, true));
        assert_preserved_entry_output(&fixture, "0 42\n");
        assert_all_emitted_js_checks(&fixture);
    }
}

#[test]
fn without_the_host_claim_bundle_globals_stay_unknown() {
    for (bundle, members, anonymous) in cases() {
        let source = source(bundle);
        expect_atomic_conflict_rejection(
            opts(&source, &members, &anonymous, false),
            &["values/native", "values/later"],
            &[],
        );
    }
}

#[test]
fn non_fresh_predefined_unknown_key_or_accessor_stores_stay_unknown() {
    for (bundle, members) in [
        // The slot can hold the global object, whose `Map` the write replaces.
        (
            "const root = globalThis;\nfunction install(name, value) { root.registry = root.registry || root; root.registry[name] = value; }",
            vec!["root", "install"],
        ),
        // `name` is a standard Window property the host defines.
        (
            "const root = globalThis;\nfunction install(name, value) { root.name = root.name || {}; root.name[name] = value; }",
            vec!["root", "install"],
        ),
        // An unknown-key global write may store anything into the slot.
        (
            "const root = globalThis;\nfunction install(name, value) { root.registry = root.registry || {}; root.registry[name] = value; }\nfunction set(key, value) { root[key] = value; }",
            vec!["root", "install", "set"],
        ),
        // `method` may be `__defineGetter__`, installing a getter for the slot.
        (ACCESSOR_CALL, vec!["root", "install", "call", "other"]),
    ] {
        let source = source(bundle);
        expect_atomic_conflict_rejection(
            opts(&source, &members, &[], true),
            &["values/native", "values/later"],
            &[],
        );
    }
}

#[test]
fn a_slot_holding_the_global_object_can_replace_map() {
    let bundle = "globalThis.registry = globalThis;\nglobalThis.registry[[\"Ma\", \"p\"].join(\"\")] = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };";
    let anonymous = [
        "globalThis.registry = globalThis;",
        "globalThis.registry[[\"Ma\", \"p\"].join(\"\")] = function Map() { try { globalThis.saved(); return { size: \"ready\" }; } catch (error) { return { size: error.name }; } };",
    ];
    let source = source(bundle);
    expect_atomic_conflict_rejection(
        opts(&source, &[], &anonymous, true),
        &["values/native", "values/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 42\n");
}

#[test]
fn a_store_in_another_chunk_keeps_the_slot_unknown() {
    let bundle = "import './poison.js';\nconst root = globalThis;\nfunction install(name, value) { root.registry = root.registry || {}; root.registry[name] = value; }";
    let source = source(bundle);
    let extra = [(
        "static/poison",
        "export function poison(value) { globalThis.registry = value; }\n",
    )];
    expect_atomic_conflict_rejection(
        opts(&source, &["root", "install"], &[], true).with_extra_chunks(&extra),
        &["values/native", "values/later"],
        &[],
    );
    let clean = [(
        "static/poison",
        "export function poison(value) { globalThis.registry = { value }; }\n",
    )];
    let fixture =
        run_fixture(opts(&source, &["root", "install"], &[], true).with_extra_chunks(&clean));
    assert_preserved_entry_output(&fixture, "0 42\n");
}

const ACCESSOR_CALL: &str = "const root = globalThis;\nfunction install(name, value) { root.registry = root.registry || {}; root.registry[name] = value; }\nfunction call(method, key, value) { root[method](key, value); }\nfunction other(target, key) { target[key](); }";

fn no_accessor_install(
    source: &str,
    declaration: &str,
    binding: &str,
) -> NoAccessorInstallCallableContract {
    NoAccessorInstallCallableContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {declaration:?}\nbindings: [{binding}]\n"
        ))
        .expect("source selector"),
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Every caller passes a primitive value.".into()),
    }
}

fn accessor_opts<'a>(
    source: &'a str,
    premise: NoAccessorInstallCallableContract,
) -> FixtureOpts<'a> {
    opts(source, &["root", "install", "call", "other"], &[], true)
        .with_no_accessor_install_callables("static/app", vec![premise])
}

#[test]
fn a_premise_on_the_calling_function_keeps_the_slot() {
    const DIRECT: &str = "function call(method, key, value) { root[method](key, value); }";
    // A nested closure is part of the selected function.
    const NESTED: &str =
        "function call(method, key, value) { const run = () => root[method](key, value); run(); }";
    for declaration in [DIRECT, NESTED] {
        let source = source(&ACCESSOR_CALL.replace(DIRECT, declaration));
        let fixture = run_fixture(accessor_opts(
            &source,
            no_accessor_install(&source, declaration, "call"),
        ));
        assert_preserved_entry_output(&fixture, "0 42\n");
        assert_all_emitted_js_checks(&fixture);
        // `new Map()` consumes the census the premise kept intact.
        let graph = fixture.dependency_graph();
        let node_id = source_unit_for_binding(&graph, "collection");
        let node = graph.nodes.iter().find(|node| node.id == node_id).unwrap();
        let report = serde_json::to_value(node).unwrap();
        assert_eq!(
            report["synchronous_execution"]["native_integrity_uses"][0]["no_accessor_install_refs"],
            serde_json::json!([{ "defining_chunk": "static/app", "input_index": 0 }]),
            "{report}"
        );
    }
}

#[test]
fn a_premise_on_another_function_keeps_the_slot_unknown() {
    let source = source(ACCESSOR_CALL);
    expect_atomic_conflict_rejection(
        accessor_opts(
            &source,
            no_accessor_install(
                &source,
                "function other(target, key) { target[key](); }",
                "other",
            ),
        ),
        &["values/native", "values/later"],
        &[],
    );
}

#[test]
fn a_premise_needs_one_function_with_a_computed_call() {
    let source = source(ACCESSOR_CALL);
    expect_rejection_containing_all(
        accessor_opts(
            &source,
            no_accessor_install(
                &source,
                "function install(name, value) { root.registry = root.registry || {}; root.registry[name] = value; }",
                "install",
            ),
        ),
        &[
            "no_accessor_install_callables[0]",
            "no computed-key method call",
        ],
    );
    let pair = source.replace(
        "function other(target, key) { target[key](); }",
        "const other = [(target, key) => target[key](), () => 0];",
    );
    expect_rejection_containing_all(
        accessor_opts(
            &pair,
            no_accessor_install(
                &pair,
                "const other = [(target, key) => target[key](), () => 0];",
                "other",
            ),
        ),
        &[
            "no_accessor_install_callables[0]",
            "exactly one outermost function",
        ],
    );
    let mut stale = no_accessor_install(
        &source,
        "function call(method, key, value) { root[method](key, value); }",
        "call",
    );
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        accessor_opts(&source, stale),
        &["no_accessor_install_callables[0]", "stale"],
    );
}
