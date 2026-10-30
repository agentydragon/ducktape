//! Native browser ESM cannot synchronously expose an evaluating namespace.

use debundle_e2e_support::*;

const PROVIDER: &str = r#"const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;

fn provider_opts<'a>(source: &'a str) -> FixtureOpts<'a> {
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module("startup", &[Member::new("prefix")]),
            logical_module("values", &[Member::new("value"), Member::new("read")]),
        ],
    );
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    opts
}

#[test]
fn unrelated_commonjs_cannot_expose_native_browser_exports() {
    let chunks = [("static/other", "export const load = typeof require;\n")];
    let fixture = run_fixture(provider_opts(PROVIDER).with_extra_chunks(&chunks));
    assert_generated_module_after_entry_script(
        &fixture,
        "const app = await import('./static/app/entry.js'); console.log(app.read());\n",
        "ready\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn generic_runtime_keeps_loader_fallback() {
    let chunks = [("static/other", "export const load = typeof require;\n")];
    let mut opts = provider_opts(PROVIDER).with_extra_chunks(&chunks);
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::Present;
    expect_atomic_conflict_rejection(opts, &["startup", "values"], &[]);
}

#[test]
fn published_callback_still_constrains_browser_startup() {
    let source = r#"export function read() { return value; }
globalThis.callback = read;
const prefix = new globalThis.Error();
export const value = "ready";
"#;
    expect_atomic_conflict_rejection(provider_opts(source), &["startup", "values"], &[]);
}

#[test]
fn inert_global_slot_publication_is_currently_treated_as_reentry() {
    let source = r#"globalThis.inspectState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "startup/all",
            &[Member::new("startup"), Member::new("later")],
            &["globalThis.inspectState = () => later;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "42\n");
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn no_global_slot_publication_allows_same_startup_split() {
    let source = r#"const unusedReader = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module("debug/exposure", &[Member::new("unusedReader")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn typeof_window_guarded_publication_uses_same_certificate() {
    let source = r#"import './prelude.js';
typeof window < 'u' && (window.inspectState = () => later);
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let files = [
        (
            "static/app/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
        (
            "static/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
    ];
    let modules = || {
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["typeof window < 'u' && (window.inspectState = () => later);"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ]
    };
    let mut unasserted = FixtureOpts::new(source, modules()).with_extra_files(&files);
    unasserted.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(unasserted, &["startup", "state/later"], &[]);

    let mut asserted = FixtureOpts::new(source, modules()).with_extra_files(&files);
    asserted.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    asserted.deferred_global_publications = &[("window", "inspectState")];
    let fixture = run_fixture(asserted);
    assert_preserved_entry_output(&fixture, "42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "console.log(globalThis.inspectState());\n",
        "42\n",
    );
}

#[test]
fn static_global_property_probe_preserves_deferred_publication() {
    let probe = "'cache' in globalThis;";
    let write = "globalThis.inspectState = () => later;";
    let source = r#"'cache' in globalThis;
globalThis.inspectState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "startup/all",
            &[Member::new("startup"), Member::new("later")],
            &[probe, write],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "42\n");

    let source_without_publication = r#"'cache' in globalThis;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut no_publication = FixtureOpts::new(
        source_without_publication,
        vec![
            logical_module_with_anon("debug/exposure", &[], &[probe]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    no_publication.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(no_publication), "42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("debug/exposure", &[], &[probe, write]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("globalThis", "inspectState")];
    assert_preserved_entry_output(&run_fixture(split), "42\n");
}

#[test]
fn primitive_result_from_global_receiver_is_not_an_alias() {
    let source = r#"const available = !!globalThis;
globalThis.inspectState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "startup/all",
            &[
                Member::new("available"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &["globalThis.inspectState = () => later;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[Member::new("available")],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("globalThis", "inspectState")];
    assert_preserved_entry_output(&run_fixture(split), "42\n");
}

#[test]
fn confined_local_global_alias_is_not_a_publication() {
    for setup in [
        "(function() { const host = globalThis; const stack = new host.Error().stack; void stack; })();",
        "!function() { const host = globalThis; const stack = new host.Error().stack; host._debug = host._debug || {}; host._debug[stack] = 'id'; }();",
    ] {
        let write = "globalThis.inspectState = () => later;";
        let source = format!(
            "{setup}\n{write}\nconst startup = new globalThis.Error();\nconst later = 42;\nconsole.log(later);\n"
        );
        let mut colocated = FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "state/all",
                &[Member::new("startup"), Member::new("later")],
                &[setup, write],
            )],
        );
        colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        assert_preserved_entry_output(&run_fixture(colocated), "42\n");

        let mut split = FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon("debug/exposure", &[], &[setup, write]),
                logical_module("startup", &[Member::new("startup")]),
                logical_module("state/later", &[Member::new("later")]),
            ],
        );
        split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        split.deferred_global_publications = &[("globalThis", "inspectState")];
        assert_preserved_entry_output(&run_fixture(split), "42\n");
    }
}

#[test]
fn private_frozen_namespace_does_not_publish_its_callback() {
    let facade = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));";
    let source = format!(
        "{facade}\nconst startup = new globalThis.Error();\nconst later = 42;\nconsole.log(facade.callback(), later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module(
            "state/all",
            &[
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "42 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module("state/facade", &[Member::new("facade")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(split), "42 42\n");
}

#[test]
fn consuming_a_frozen_namespace_still_invokes_its_callback() {
    let facade = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));";
    let source = format!(
        "let later = 0;\n{facade}\nconst startup = facade.callback();\nlater = 42;\nconsole.log(startup, later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &["later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module("state/facade", &[Member::new("facade")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn opaque_consumer_of_frozen_namespace_keeps_stored_callback() {
    let consumer =
        "globalThis.consume = value => { globalThis.observed = value.callback(); return 1; };";
    let facade = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));";
    let source = format!(
        "let later = 0;\n{consumer}\n{facade}\nconst startup = globalThis.consume(facade);\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &[consumer, "later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon("state/facade", &[Member::new("facade")], &[consumer]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn unsupported_frozen_namespace_shapes_keep_callback_reentry() {
    for (setup, facade) in [
        (
            "const source = {get callback() { globalThis.observed = later; return () => later; }};",
            "const facade = Object.freeze(Object.defineProperty({__proto__: null, ...source}, Symbol.toStringTag, {value: 'Module'}));",
        ),
        (
            "const source = new Proxy({callback: () => later}, {defineProperty(target, key, descriptor) { globalThis.observed = later; return Reflect.defineProperty(target, key, descriptor); }});",
            "const facade = Object.freeze(Object.defineProperty(source, Symbol.toStringTag, {value: 'Module'}));",
        ),
    ] {
        let source = format!(
            "let later = 0;\n{setup}\n{facade}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
        );
        let mut colocated = FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "state/all",
                &[
                    Member::new("source"),
                    Member::new("facade"),
                    Member::new("startup"),
                    Member::new("later"),
                ],
                &["later = 42;"],
            )],
        );
        colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

        let mut split = FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "state/facade",
                    &[Member::new("source"), Member::new("facade")],
                ),
                logical_module("startup", &[Member::new("startup")]),
                logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        expect_atomic_conflict_rejection(split, &["state/facade", "state/later"], &[]);
    }
}

#[test]
fn changed_frozen_namespace_intrinsic_keeps_early_callback_effects() {
    let source = "let later = 0;
const Object = {defineProperty(value) { return value; }, freeze(value) { globalThis.observed = value.callback(); return value; }};
const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));
const startup = new globalThis.Error();
later = 42;
console.log(globalThis.observed, later);
";
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("Object"),
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &["later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "state/facade",
                &[Member::new("Object"), Member::new("facade")],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["state/facade", "state/later"], &[]);
}

#[test]
fn replaced_frozen_namespace_intrinsics_keep_early_reads() {
    let facade = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));";
    for (saved, mutate, restore) in [
        (
            "const saved = Object.freeze;",
            "Object.freeze = value => { globalThis.observed = value.callback(); return saved(value); };",
            "Object.freeze = saved;",
        ),
        (
            "const saved = Object.defineProperty;",
            "Object.defineProperty = (value, key, descriptor) => { globalThis.observed = value.callback(); return saved(value, key, descriptor); };",
            "Object.defineProperty = saved;",
        ),
        (
            "const saved = globalThis.Symbol;",
            "globalThis.Symbol = new Proxy(saved, {get(target, key) { if (key === 'toStringTag') globalThis.observed = later; return Reflect.get(target, key); }});",
            "globalThis.Symbol = saved;",
        ),
    ] {
        let source = format!(
            "let later = 0;\n{saved}\n{mutate}\n{facade}\n{restore}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
        );
        let mut colocated = FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "state/all",
                &[
                    Member::new("saved"),
                    Member::new("facade"),
                    Member::new("startup"),
                    Member::new("later"),
                ],
                &[mutate, restore, "later = 42;"],
            )],
        );
        colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

        let mut split = FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "state/facade",
                    &[Member::new("saved"), Member::new("facade")],
                    &[mutate, restore],
                ),
                logical_module("startup", &[Member::new("startup")]),
                logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        expect_atomic_conflict_rejection(split, &["state/facade", "state/later"], &[]);
    }
}

#[test]
fn aliased_symbol_replacement_keeps_early_read() {
    let mutate = "root.Symbol = new Proxy(saved, {get(target, key) { if (key === 'toStringTag') globalThis.observed = later; return Reflect.get(target, key); }});";
    let restore = "root.Symbol = saved;";
    let source = format!(
        "let later = 0;\nconst root = globalThis;\nconst saved = root.Symbol;\n{mutate}\nconst facade = Object.freeze(Object.defineProperty({{__proto__: null, callback: () => later}}, Symbol.toStringTag, {{value: 'Module'}}));\n{restore}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("root"),
                Member::new("saved"),
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &[mutate, restore, "later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon(
                "state/facade",
                &[
                    Member::new("root"),
                    Member::new("saved"),
                    Member::new("facade"),
                ],
                &[mutate, restore],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["state/facade", "state/later"], &[]);
}

#[test]
fn local_global_alias_must_not_escape_its_one_shot_iife() {
    for setup in [
        "(function repeat() { const host = globalThis; globalThis.saved = repeat; void host.Error; })();",
        "(function() { const host = globalThis; try { globalThis.saved = arguments.callee; } catch (error) {} void host.Error; })();",
        "(function() { const host = globalThis; globalThis.saved = () => host.inspectState(); })();",
        "(function() { const host = globalThis; host.Array.of(host); })();",
        "(function() { const host = globalThis; void host.inspectState; })();",
        "(function() { const host = globalThis; void host['inspect' + 'State']; })();",
        "(() => { const host = globalThis; void host.Error; })();",
    ] {
        let source = format!(
            "{setup}\nglobalThis.inspectState = () => later;\nconst startup = new globalThis.Error();\nconst later = 42;\nconsole.log(later);\n"
        );
        let mut opts = FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "debug/exposure",
                    &[],
                    &[setup, "globalThis.inspectState = () => later;"],
                ),
                logical_module("startup", &[Member::new("startup")]),
                logical_module("state/later", &[Member::new("later")]),
            ],
        );
        opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        opts.deferred_global_publications = &[("globalThis", "inspectState")];
        expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
    }
}

#[test]
fn deferred_global_certificate_does_not_cover_another_slot_or_cross_root_read() {
    let source = r#"globalThis.otherState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let modules = vec![
        logical_module_with_anon(
            "debug/exposure",
            &[],
            &["globalThis.otherState = () => later;"],
        ),
        logical_module("startup", &[Member::new("startup")]),
        logical_module("state/later", &[Member::new("later")]),
    ];
    let mut opts = FixtureOpts::new(source, modules);
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);

    let source = r#"globalThis.inspectState = () => later;
typeof window !== 'undefined' && window.inspectState;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
}

#[test]
fn deferred_global_certificate_rejects_visible_descriptor_or_alias() {
    for before in [
        "Object.defineProperty(globalThis, 'inspectState', {set(fn) { globalThis.seen = fn(); }});",
        "const alias = globalThis; alias.inspectState = () => later;",
        "const alias = ({root: globalThis}).root; Object.defineProperty(alias, 'inspectState', {set(fn) { globalThis.seen = fn(); }});",
        "const receive = value => Object.defineProperty(value, 'inspectState', {set(fn) { globalThis.seen = fn(); }}); receive(globalThis);",
    ] {
        let source = format!(
            "let later = 0;\n{before}\nglobalThis.inspectState = () => later;\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(later);\n"
        );
        let mut opts = FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "debug/exposure",
                    &[],
                    &["globalThis.inspectState = () => later;"],
                ),
                logical_module("startup", &[Member::new("startup")]),
                logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        opts.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
        opts.deferred_global_publications = &[("globalThis", "inspectState")];
        expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
    }
}

#[test]
fn dynamic_in_key_keeps_its_early_callback_read() {
    let source = r#"let later = 0;
const key = {toString() { globalThis.observed = later; return 'cache'; }};
key in globalThis;
globalThis.inspectState = () => later;
const startup = new globalThis.Error();
later = 42;
console.log(globalThis.observed, later);
"#;
    let setup = "key in globalThis;";
    let write = "globalThis.inspectState = () => later;";
    let assignment = "later = 42;";
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("key"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &[setup, write, assignment],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("debug/exposure", &[Member::new("key")], &[setup, write]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &[assignment]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn block_shadowed_window_in_protocol_cannot_hide_proxy_reentry() {
    let block = "{ const window = new Proxy({}, {has() { globalThis.observed = later; return false; }}); 'cache' in window; }";
    let write = "typeof window < 'u' && (window.inspectState = () => later);";
    let source = format!(
        "import './prelude.js';\nlet later = 0;\n{block}\n{write}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
    );
    let files = [
        (
            "static/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
        (
            "static/app/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
    ];
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[Member::new("startup"), Member::new("later")],
            &[block, write, "later = 42;"],
        )],
    )
    .with_extra_files(&files);
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon("debug/exposure", &[], &[block, write]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    )
    .with_extra_files(&files);
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("window", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn replaced_window_alias_cannot_certify_a_global_write() {
    let setup = "globalThis.window = new Proxy(globalThis, {set(target, key, value) { if (key === 'inspectState') globalThis.observed = value(); return Reflect.set(target, key, value); }});";
    let write = "typeof window < 'u' && (window.inspectState = () => later);";
    let source = format!(
        "let later = 0;\n{setup}\n{write}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[Member::new("startup"), Member::new("later")],
            &[setup, write, "later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon("debug/exposure", &[], &[setup, write]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("window", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn direct_read_and_call_of_published_slot_stays_ordered() {
    let source = r#"let later = 0;
globalThis.inspectState = () => later;
const startup = globalThis.inspectState();
later = 42;
console.log(startup, later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state/all",
            &[Member::new("startup"), Member::new("later")],
            &["globalThis.inspectState = () => later;", "later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    split.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn separately_exported_reader_keeps_generic_reentry() {
    let source = r#"import { tick } from './peer.js';
export function reader() { return later; }
globalThis.inspectState = reader;
const startup = tick();
const later = 42;
console.log(startup, later);
"#;
    let files = [("static/peer.js", "export function tick() { return 0; }\n")];
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[Member::new("reader")],
                &["globalThis.inspectState = reader;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    )
    .with_extra_files(&files);
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
}

#[test]
fn cycle_keeps_export_reentry_in_browser() {
    let source = r#"import './other.js';
const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;
    let chunks = [("static/other", "import './app.js'; export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(source).with_extra_chunks(&chunks),
        &["startup", "values"],
        &[],
    );
}

#[test]
fn missing_and_pruned_inputs_cannot_certify_browser_closure() {
    let source = r#"import './missing.js';
const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;
    let files = [("static/missing.js", "export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(source).with_extra_files(&files),
        &["startup", "values"],
        &[],
    );
    let chunks = [("static/other", "export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(PROVIDER)
            .with_extra_chunks(&chunks)
            .with_prune_other_chunks(),
        &["startup", "values"],
        &[],
    );
}
