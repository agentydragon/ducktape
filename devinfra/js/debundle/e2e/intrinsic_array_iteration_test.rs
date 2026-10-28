//! Builtin iteration preserves real callback effects and retained array values.
use debundle_e2e_support::*;

fn assert_early_iteration_rejected(
    declarations: &str,
    setup: &[&str],
    collection: &str,
    trailing: &str,
    names: &[&'static str],
) {
    assert_early_iteration_rejected_with_executor(
        declarations,
        setup,
        collection,
        trailing,
        names,
        "0",
    );
}

fn assert_early_iteration_rejected_with_executor(
    declarations: &str,
    setup: &[&str],
    collection: &str,
    trailing: &str,
    names: &[&'static str],
    executor: &str,
) {
    let source = format!(
        "import {{ consumeCallbacks }} from './peer.js';\nlet later = 0;\nconst entries = [{{ id: 'a', executor: () => {executor} }}, {{ id: 'b', executor: () => {executor} }}];\nlet observed = -1;\n{declarations}\n{}\nconst result = {collection};\n{trailing}\nlater = 42;\nconsole.log(observed);\n",
        setup.join("\n")
    );
    let mut registry = vec![
        Member::new("entries"),
        Member::new("observed"),
        Member::new("result"),
    ];
    registry.extend(names.iter().map(|name| Member::new(name)));
    let mut steps = setup.to_vec();
    if !trailing.is_empty() {
        steps.push(trailing);
    }
    let split = vec![
        logical_module_with_anon("registry", &registry, &steps),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let peers = [(
        "static/peer",
        "export function consumeCallbacks(array) { return array[0](); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split).with_extra_chunks(&peers),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn actual_iteration_callback_invocation_keeps_early_reads() {
    for collection in [
        "entries.forEach(entry => { observed = entry.executor(); })",
        "entries.filter(entry => { observed = entry.executor(); return true; })",
        "entries.map(entry => { observed = entry.executor(); return entry.id; })",
    ] {
        assert_early_iteration_rejected_with_executor("", &[], collection, "", &[], "later");
    }
}

#[test]
fn iteration_source_and_entry_mutation_remain_conservative() {
    assert_early_iteration_rejected(
        "function replace({items}) { items[1] = { executor: () => later }; }",
        &["replace({items: entries});"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["replace"],
    );
    assert_early_iteration_rejected(
        "const holder = { items: entries, get replace() { this.items[1] = { executor: () => later }; return 0; } };",
        &["holder.replace;"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["holder"],
    );
    assert_early_iteration_rejected(
        "const holder = { items: entries, replace() { this.items[1] = { executor: () => later }; } };",
        &["holder.replace();"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["holder"],
    );
    assert_early_iteration_rejected(
        "function replace() { arguments[0][1] = { executor: () => later }; }",
        &["replace(entries);"],
        "entries.forEach(entry => { observed = entry.executor(); })",
        "",
        &["replace"],
    );
    for collection in [
        "entries.forEach((entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); })",
        "entries.forEach((entry, index) => { if (index === 0) entries[1].executor = () => later; else observed = entry.executor(); })",
        "entries.forEach(function(entry, index) { if (index === 0) arguments[2][1] = { executor: () => later }; else observed = entry.executor(); })",
    ] {
        assert_early_iteration_rejected("", &[], collection, "", &[]);
    }
    assert_early_iteration_rejected(
        "function mutate() { arguments[0][1] = { executor: () => later }; }",
        &[],
        "entries.forEach((entry, index, array) => { if (index === 0) mutate(array); else observed = entry.executor(); })",
        "",
        &["mutate"],
    );
}

#[test]
fn replaced_methods_and_accessor_entries_keep_real_effects() {
    assert_early_iteration_rejected(
        "",
        &["entries.forEach = callback => { observed = later; };"],
        "entries.forEach(() => {})",
        "",
        &[],
    );
    assert_early_iteration_rejected(
        "",
        &["Object.defineProperty(entries[0], 'id', { get() { observed = later; return 'a'; } });"],
        "entries.forEach(entry => { entry.id; })",
        "",
        &[],
    );
    assert_early_iteration_rejected(
        "",
        &["entries[Symbol.iterator] = function*() { observed = later; yield entries[0]; };"],
        "[...entries]",
        "",
        &[],
    );
}

#[test]
fn borrowed_public_method_can_publish_a_native_receiver() {
    assert_early_iteration_rejected(
        "class Publisher { install() { globalThis.slot = this; } } const publisher = new Publisher();",
        &[
            "publisher.install.call(Array.prototype);",
            "globalThis.slot.forEach = function(callback) { observed = later; };",
        ],
        "entries.forEach(() => {})",
        "",
        &["Publisher", "publisher"],
    );
}

#[test]
fn fresh_global_rhs_does_not_prove_what_a_slot_read_returns() {
    assert_early_iteration_rejected(
        "",
        &[
            "Object.defineProperty(globalThis, 'slot', { configurable: true, get() { return Array.prototype; }, set(value) {} });",
            "globalThis.slot = {};",
            "globalThis.slot.forEach = function(callback) { observed = later; };",
        ],
        "entries.forEach(() => {})",
        "",
        &[],
    );
}

#[test]
fn array_species_mutation_through_source_and_result_aliases_stays_conservative() {
    assert_early_iteration_rejected(
        "",
        &[
            "entries.constructor = { [Symbol.species]: class { constructor() { observed = later; } } };",
        ],
        "entries.map(entry => entry.id)",
        "",
        &[],
    );
    assert_early_iteration_rejected(
        "const C = entries.constructor;",
        &[
            "Object.defineProperty(C, Symbol.species, { get() { observed = later; return Array; } });",
        ],
        "entries.map(entry => entry)",
        "",
        &["C"],
    );
    assert_early_iteration_rejected(
        "const filtered = entries.filter(entry => entry.id); const C = filtered.constructor;",
        &[
            "Object.defineProperty(C, Symbol.species, { get() { observed = later; return Array; } });",
        ],
        "filtered.map(entry => entry)",
        "",
        &["filtered", "C"],
    );
}

#[test]
fn mapped_results_retain_callbacks_for_invocation_and_publication() {
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => ({ get value() { return later; } }))",
        "observed = result[0].value;",
        &[],
    );
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => () => later)",
        "observed = result[0]();",
        &[],
    );
    assert_early_iteration_rejected(
        "function consume(array) { observed = array[0](); }",
        &[],
        "entries.map(() => () => later)",
        "consume(result);",
        &["consume"],
    );
    assert_early_iteration_rejected(
        "",
        &[],
        "entries.map(() => () => later)",
        "observed = consumeCallbacks(result);",
        &[],
    );
}

#[test]
fn empty_builtin_iteration_never_invokes_its_callback() {
    let source = "let later = 0;\nconst entries = [];\nconst result = entries.map(() => later);\nlater = 42;\nconsole.log(result.length);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("registry", &[Member::new("entries"), Member::new("result")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
}

#[test]
fn object_spread_getters_run_during_map_even_without_a_result_consumer() {
    for declarations in [
        "const payload = { get value() { observed = later; return 'ready'; } }; const entries = [{ payload }];",
        "const payload = { get value() { observed = later; return 'ready'; } }; const entries = [{ payload: { value: 'safe' } }, { payload }];",
    ] {
        let source = format!(
            "let later = 0;\nlet observed = -1;\n{declarations}\nfunction copy(value) {{ return {{ ...value }}; }}\nconst result = entries.map(entry => copy(entry.payload));\nlater = 42;\nconsole.log(observed);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "0\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "registry",
                        &[
                            Member::new("observed"),
                            Member::new("payload"),
                            Member::new("entries"),
                            Member::new("copy"),
                            Member::new("result"),
                        ],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            ),
            &["registry", "values/later"],
            &[],
        );
    }
}

#[test]
fn ordinary_spread_copy_fields_retain_referenced_callback_identity() {
    let source = "let later = 0;\nconst entries = [{ run: () => later }];\nconst result = entries.map(entry => ({ ...entry }));\nconst observed = result[0].run();\nlater = 42;\nconsole.log(observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "registry",
                    &[
                        Member::new("entries"),
                        Member::new("result"),
                        Member::new("observed"),
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn iterating_function_values_stores_them_without_invoking_them() {
    let source = "let later = 0;\nconst entries = [() => later];\nconst result = entries.map(callback => callback);\nlater = 42;\nconsole.log(result[0]());\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("registry", &[Member::new("entries"), Member::new("result")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn prior_unknown_call_does_not_make_fresh_primitive_cache_writes_run_unrelated_getters() {
    let source = "import { prepare } from './peer.js';\nlet later = 0;\nconst ids = { tool: 'tool-id' };\nconst entries = [{ id: ids.tool, name: 'tool', run: () => later }];\nconst lookup = {};\nconst latent = { get value() { return later; } };\nprepare();\nentries.forEach(entry => { lookup[entry.id] = entry.name; });\nlater = 42;\nconsole.log(lookup['tool-id'], entries[0].run());\n";
    let peers = [("static/peer", "export function prepare() { return 0; }\n")];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "tool 42\n");
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[
                        Member::new("ids"),
                        Member::new("entries"),
                        Member::new("lookup"),
                        Member::new("latent"),
                    ],
                    &[
                        "prepare();",
                        "entries.forEach(entry => { lookup[entry.id] = entry.name; });",
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "tool 42\n");
}

#[test]
fn cache_write_setters_and_key_coercions_preserve_real_effects() {
    assert_early_iteration_rejected(
        "const lookup = { set a(value) { observed = later; } };",
        &[],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
    assert_early_iteration_rejected(
        "const lookup = {};",
        &[
            "Object.defineProperty(Object.prototype, 'a', { set(value) { observed = later; }, configurable: true });",
        ],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
    assert_early_iteration_rejected(
        "const lookup = {};",
        &["entries[0].id = { toString() { observed = later; return 'a'; } };"],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup"],
    );
}

#[test]
fn implicit_protocols_cannot_install_unseen_cache_setters() {
    let definitions = "const lookup = {}; function install() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); }";
    for (extra, setup, names) in [
        (
            "const key = { toString() { install(); return 'a'; } }; const field = `${key}`;",
            "",
            vec!["key", "field"],
        ),
        (
            "class Matcher { static [Symbol.hasInstance]() { install(); return true; } }",
            "({}) instanceof Matcher;",
            vec!["Matcher"],
        ),
        (
            "const holder = new Proxy({}, { has() { install(); return true; } });",
            "'field' in holder;",
            vec!["holder"],
        ),
        (
            "const holder = { get field() { install(); return 0; } }; const { field } = holder;",
            "",
            vec!["holder", "field"],
        ),
        (
            "const holder = { *[Symbol.iterator]() { install(); yield 0; } }; const [field] = holder;",
            "",
            vec!["holder", "field"],
        ),
    ] {
        let declarations = format!("{definitions} {extra}");
        let mut names = names;
        names.extend(["lookup", "install"]);
        let setup = if setup.is_empty() {
            vec![]
        } else {
            vec![setup]
        };
        assert_early_iteration_rejected(
            &declarations,
            &setup,
            "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
            "",
            &names,
        );
    }
    assert_early_iteration_rejected(
        "const lookup = {}; const key = { toString() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); return 'a'; } };",
        &[],
        "entries.forEach(entry => { lookup[`${key}`] = entry.id; })",
        "",
        &["lookup", "key"],
    );
}

#[test]
fn nested_destructuring_getters_cannot_install_unseen_cache_setters() {
    assert_early_iteration_rejected(
        "const lookup = {}; const holder = { nested: { get value() { Object.defineProperty(lookup, 'a', { set(value) { observed = later; }, configurable: true }); return 0; } } }; const { nested: { value } } = holder;",
        &[],
        "entries.forEach(entry => { lookup[entry.id] = entry.id; })",
        "",
        &["lookup", "holder", "value"],
    );
}

#[test]
fn mapped_fallthrough_values_preserve_undefined_default_consumers() {
    for mapper in [
        "() => {}",
        "() => { return; }",
        "() => { if (false) return 1; }",
        "() => empty()",
    ] {
        assert_early_iteration_rejected(
            "function empty() {} function consume(value = () => later) { return value(); }",
            &[],
            &format!("entries.map({mapper})"),
            "observed = consume(result[0]);",
            &["empty", "consume"],
        );
    }
}

#[test]
fn default_instanceof_does_not_execute_unused_constructor_methods() {
    for value in ["({})", "getValue()"] {
        let source = format!(
            "import {{ getValue }} from './peer.js';\nlet later = 0;\nclass Matcher {{ unused() {{ return later; }} }}\nconst matches = {value} instanceof Matcher;\nlater = 42;\nconsole.log(matches);\n"
        );
        let peers = [("static/peer", "export function getValue() { return {}; }\n")];
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "registry",
                        &[Member::new("Matcher"), Member::new("matches")],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peers),
        );
        assert_preserved_entry_output(&fixture, "false\n");
    }
}

#[test]
fn instanceof_proxy_and_custom_has_instance_effects_remain_visible() {
    assert_early_iteration_rejected(
        "class Matcher {} const value = new Proxy({}, { getPrototypeOf() { observed = later; return null; } });",
        &[],
        "value instanceof Matcher",
        "",
        &["Matcher", "value"],
    );
    assert_early_iteration_rejected(
        "class Matcher { static [Symbol.hasInstance]() { observed = later; return false; } }",
        &[],
        "({}) instanceof Matcher",
        "",
        &["Matcher"],
    );
    assert_early_iteration_rejected(
        "class Matcher {}",
        &[
            "Object.defineProperty(Matcher, Symbol.hasInstance, { value() { observed = later; return false; } });",
        ],
        "({}) instanceof Matcher",
        "",
        &["Matcher"],
    );
}

#[test]
fn copy_contexts_retain_missing_fields_for_default_consumers() {
    for (entries, index) in [("[{ run: () => 0 }, {}]", 1), ("[{}, { run: () => 0 }]", 0)] {
        let source = format!(
            "let later = 0;\nconst entries = {entries};\nconst copies = entries.map(entry => ({{ ...entry }}));\nfunction consume(value = () => later) {{ return value(); }}\nconst observed = consume(copies[{index}].run);\nlater = 42;\nconsole.log(observed);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "0\n");
        let split = vec![
            logical_module(
                "registry",
                &[
                    Member::new("entries"),
                    Member::new("copies"),
                    Member::new("consume"),
                    Member::new("observed"),
                ],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, split),
            &["registry", "values/later"],
            &[],
        );
    }
}

#[test]
fn getter_triggered_async_work_keeps_future_reads_after_await() {
    let source = "let later = 0;\nclass Reader { static async read() { await 0; console.log(later); } }\nconst holder = { get value() { Reader.read(); return 0; } };\nconst observed = holder.value;\nlater = 42;\nconsole.log(observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "0\n42\n");
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "registry",
                &[
                    Member::new("Reader"),
                    Member::new("holder"),
                    Member::new("observed"),
                ],
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n42\n");
}

#[test]
fn installed_getters_can_invoke_returned_escaped_callbacks() {
    let source = "import { retain } from './peer.js';\nlet later = 0;\nfunction createReader() { return () => later; }\nconst holder = { value: 1 };\nretain(createReader);\nretain(holder);\nconst observed = holder.missing;\nlater = 42;\nconsole.log(observed);\n";
    let peers = [(
        "static/peer",
        "let factory; export function retain(value) { if (typeof value === 'function') factory = value; else Object.defineProperty(value, 'missing', { get() { return factory()(); } }); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[
                        Member::new("createReader"),
                        Member::new("holder"),
                        Member::new("observed"),
                    ],
                    &["retain(createReader);", "retain(holder);"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers),
        &["registry", "values/later"],
        &[],
    );
}
