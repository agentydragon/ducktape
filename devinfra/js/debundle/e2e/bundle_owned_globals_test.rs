//! A global property that analyzed code only ever fills with fresh objects
//! holds no intrinsic under `predefined_globals: standard`, so unknown-key
//! writes through its value reach no native slot.

use debundle_e2e_support::*;

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

fn cases() -> [(&'static str, Vec<&'static str>, Vec<&'static str>); 4] {
    [
        (SENTRY, vec!["register"], vec![]),
        (OTEL, vec!["root", "key", "register", "unregister"], vec![]),
        (ANALYTICS, vec!["root", "getInstance"], vec![]),
        (
            EXPERIMENTS,
            vec!["root", "instances", "create"],
            vec!["root.experimentInstances = {};"],
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
fn non_fresh_predefined_or_unknown_key_stores_stay_unknown() {
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
