//! Builtin iteration preserves real callback effects and retained array values.
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use serde_json::Value;

fn binding_execution(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    let unit = source_unit_for_binding(&graph, binding);
    let node = graph
        .nodes
        .iter()
        .find(|node| node.id == unit)
        .expect("binding source unit");
    serde_json::to_value(
        node.synchronous_execution
            .as_ref()
            .expect("synchronous execution report"),
    )
    .expect("serialize execution report")
}

fn assert_native_map_receipt(fixture: &Fixture, binding: &str) {
    let execution = binding_execution(fixture, binding);
    assert_eq!(execution["complete"], true, "{execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(authority["authority"], "operation_receipt", "{execution:#}");
    let calls = authority["receipt"]["calls"]
        .as_array()
        .expect("operation receipt calls");
    let map = calls
        .iter()
        .find(|call| {
            call["target"]["kind"] == "native"
                && call["target"]["intrinsic"] == "array"
                && call["target"]["property"] == "prototype.map"
        })
        .unwrap_or_else(|| panic!("missing selected Array.prototype.map edge: {execution:#}"));
    assert_eq!(map["caller"]["kind"], "root_initializer", "{map:#}");
}

fn assert_no_native_iteration_receipt(fixture: &Fixture, property: &str) {
    let expected_property = format!("prototype.{property}");
    let graph = fixture.dependency_graph();
    let reports: Vec<_> = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .collect();
    assert!(
        !reports.is_empty(),
        "fixture must publish execution reports"
    );
    for execution in reports {
        let execution = serde_json::to_value(execution).expect("serialize execution report");
        let calls = execution
            .pointer("/binding_effect_authority/receipt/calls")
            .and_then(Value::as_array);
        assert!(
            calls.is_none_or(|calls| !calls.iter().any(|call| {
                call["target"]["kind"] == "native"
                    && call["target"]["intrinsic"] == "array"
                    && call["target"]["property"].as_str() == Some(expected_property.as_str())
            })),
            "iteration without a completed selected transfer must not publish a native {property} receipt: {execution:#}"
        );
    }
}

fn assert_unsupported_iteration_keeps_source_output(
    collection: &str,
    expected: &str,
    property: &str,
) {
    let source = format!(
        "let later = 0;\nconst entries = [{{ id: 'a', size: 1, executor: () => later }}, {{ id: 'b', size: 2, executor: () => later }}];\nlet observed = -1;\nconst result = {collection};\nlater = 42;\nconsole.log(JSON.stringify(result), observed, entries[0].executor());\n"
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_preserved_entry_output(&fixture, expected);
    assert_no_native_iteration_receipt(&fixture, property);

    let split = vec![
        logical_module_with_anon(
            "registry",
            &[
                Member::new("entries"),
                Member::new("observed"),
                Member::new("result"),
            ],
            &[],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split),
        &["registry", "values/later"],
        &[],
    );
}

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

/// The split is accepted and the emitted entry prints what the source does:
/// the callback ran on every element without invoking their stored
/// `executor` callbacks, which read `later`.
fn assert_iteration_split_accepted(collection: &str, trailing: &str, expected: &str) {
    assert_iteration_split_accepted_after("", collection, trailing, expected);
}

fn assert_iteration_split_accepted_after(
    setup: &str,
    collection: &str,
    trailing: &str,
    expected: &str,
) {
    let source = format!(
        "let later = 0;\n{setup}\nconst entries = [{{ id: 'a', size: 1, executor: () => later }}, {{ id: 'b', size: 2, executor: () => later }}];\nconst result = {collection};\n{trailing}\nlater = 42;\nconsole.log(JSON.stringify(result), entries[0].executor());\n"
    );
    let mut steps = Vec::new();
    if !setup.is_empty() {
        steps.push(setup);
    }
    if !trailing.is_empty() {
        steps.push(trailing);
    }
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon(
                "registry",
                &[Member::new("entries"), Member::new("result")],
                &steps,
            ),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, expected);
}

#[test]
fn selected_native_map_has_an_operation_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [1, 2];\nconst mapped = entries.map(value => value + 1);\nconsole.log(mapped.join(','));\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "2,3\n");
    assert_native_map_receipt(&fixture, "mapped");
}

#[test]
fn native_map_keeps_its_receiver_when_an_argument_rebinds_the_source_cell() {
    let fixture = run_fixture(FixtureOpts::new(
        "const original = [1, 2];\nconst replacement = [10, 20];\nlet receiver = original;\nconst mapped = receiver.map((receiver = replacement, value => value + 1));\nconsole.log(mapped[0], receiver[0]);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "2 10\n");
    assert_native_map_receipt(&fixture, "mapped");
}

#[test]
fn a_custom_prototype_method_selected_before_arguments_is_not_reinterpreted_after_restore() {
    let fixture = run_fixture(FixtureOpts::new(
        "const nativeMap = Array.prototype.map;\nconst entries = [1, 2];\nArray.prototype.map = function () { return ['custom']; };\nconst mapped = entries.map((Array.prototype.map = nativeMap, value => value + 1));\nconsole.log(mapped[0]);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "custom\n");
    let execution = binding_execution(&fixture, "mapped");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "a custom method selected before the argument must not gain native receipt after the argument restores the prototype: {execution:#}"
    );
}

#[test]
fn flattened_effect_only_iteration_keeps_custom_method_fallback_and_refuses_receipt() {
    let source = "let later = 0;\nconst nativeMap = Array.prototype.map;\nconst entries = [1, 2];\nlet observed = -1;\nArray.prototype.map = function () { return ['custom']; };\nif (true) entries.map((Array.prototype.map = nativeMap, value => { observed = later; return value + 1; }));\nlater = 42;\nconsole.log(observed);\n";
    let steps = [
        "Array.prototype.map = function () { return ['custom']; };",
        "if (true) entries.map((Array.prototype.map = nativeMap, value => { observed = later; return value + 1; }));",
    ];
    let split = vec![
        logical_module_with_anon(
            "registry",
            &[
                Member::new("nativeMap"),
                Member::new("entries"),
                Member::new("observed"),
            ],
            &steps,
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "-1\n");
    assert_no_native_iteration_receipt(&fixture, "map");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, split),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn throw_only_map_callback_does_not_certify_a_native_map_result() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [1];\nconst mapped = entries.map(() => { throw new Error('callback failure'); });\nconsole.log(mapped[0]);\n",
        vec![],
    ));
    assert_preserved_entry_error(&fixture, "Error");
    assert_no_native_iteration_receipt(&fixture, "map");
}

#[test]
fn mixed_map_callback_completion_does_not_certify_a_native_map_result() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [1, 2];\nconst mapped = entries.map(value => { if (value === 2) throw new Error('callback failure'); return value; });\nconsole.log(mapped[0]);\n",
        vec![],
    ));
    assert_preserved_entry_error(&fixture, "Error");
    assert_no_native_iteration_receipt(&fixture, "map");
}

#[test]
fn repeated_local_map_site_uses_each_activation_receiver() {
    let fixture = run_fixture(FixtureOpts::new(
        "function mapValues(values) { return values.map(value => value * 2); }\nconst first = mapValues([3]);\nconst second = mapValues([7]);\nconsole.log(first[0], second[0]);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "6 14\n");
}

#[test]
fn iteration_rows_run_callbacks_without_invoking_stored_element_callbacks() {
    for (collection, expected) in [
        ("entries.forEach(entry => entry.size)", "undefined 42\n"),
        ("entries.map(entry => entry.size)", "[1,2] 42\n"),
    ] {
        assert_iteration_split_accepted(collection, "", expected);
    }
}

#[test]
fn unsupported_iteration_result_shapes_keep_conservative_callback_effects() {
    for (collection, expected, property) in [
        (
            "entries.some(entry => { observed = later; return entry.id === 'b'; })",
            "true 0 42\n",
            "some",
        ),
        (
            "entries.every(entry => { observed = later; return entry.size > 0; })",
            "true 0 42\n",
            "every",
        ),
        (
            "entries.findIndex(entry => { observed = later; return entry.id === 'b'; })",
            "1 0 42\n",
            "findIndex",
        ),
        (
            "entries.find(entry => { observed = later; return entry.id === 'b'; })",
            "{\"id\":\"b\",\"size\":2} 0 42\n",
            "find",
        ),
        (
            "entries.reduce((total, entry) => { observed = later; return total + entry.size; }, 0)",
            "3 0 42\n",
            "reduce",
        ),
        (
            "entries.reduce((first, entry) => { observed = later; return first; })",
            "{\"id\":\"a\",\"size\":1} 0 42\n",
            "reduce",
        ),
        (
            "entries.flatMap(entry => { observed = later; return [entry.id, entry.size]; })",
            "[\"a\",1,\"b\",2] 0 42\n",
            "flatMap",
        ),
        (
            "entries.flatMap(entry => { observed = later; return entry.id; })",
            "[\"a\",\"b\"] 0 42\n",
            "flatMap",
        ),
    ] {
        assert_unsupported_iteration_keeps_source_output(collection, expected, property);
    }
}

#[test]
fn filter_excluding_the_first_element_does_not_certify_positional_slots() {
    let source = "let later = 0;\nconst entries = [1, 2];\nlet observed = -1;\nconst filtered = entries.filter(value => { observed = later; return value === 2; });\nlater = 42;\nconsole.log(filtered[0], observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "2 0\n");
    assert_no_native_iteration_receipt(&fixture, "filter");
    let split = vec![
        logical_module_with_anon(
            "registry",
            &[
                Member::new("entries"),
                Member::new("observed"),
                Member::new("filtered"),
            ],
            &[],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, split),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn short_circuiting_some_does_not_certify_an_unreachable_throwing_suffix() {
    let fixture = run_fixture(FixtureOpts::new(
        "const entries = [1, 2];\nconst result = entries.some(value => value === 1 || (() => { throw new Error('unreachable'); })());\nconsole.log(result);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "true\n");
    assert_no_native_iteration_receipt(&fixture, "some");
}

/// Rows require only the Array prototype properties they read: writes to the
/// global object, to `Array` statics, or to `Object.prototype` under other keys
/// leave them intact.
#[test]
fn unrelated_intrinsic_writes_leave_iteration_rows_intact() {
    for setup in [
        "globalThis.appState = { ready: true };",
        "Array.from = Array.from;",
        "Array.prototype.unrelated = function () { return later; };",
        "Object.defineProperty(Object.prototype, 'unrelated', { value: 1, configurable: true });",
    ] {
        assert_iteration_split_accepted_after(
            setup,
            "entries.map(entry => entry.size)",
            "",
            "[1,2] 42\n",
        );
    }
}

#[test]
fn writes_reaching_a_row_slot_keep_real_effects() {
    for (declarations, setup, collection, names) in [
        (
            "",
            "Array.prototype.some = function () { observed = later; return false; };",
            "entries.some(() => false)",
            &[][..],
        ),
        (
            "",
            "Object.defineProperty(Array.prototype, 'every', { value() { observed = later; return true; }, configurable: true });",
            "entries.every(() => true)",
            &[],
        ),
        (
            "const key = ['fi', 'nd'].join('');",
            "Array.prototype[key] = function () { observed = later; };",
            "entries.find(() => false)",
            &["key"],
        ),
        (
            "",
            "Object.defineProperty(Array, Symbol.species, { get() { observed = later; return Array; }, configurable: true });",
            "entries.map(entry => entry.id)",
            &[],
        ),
        (
            "",
            "Array.prototype.constructor = { get [Symbol.species]() { observed = later; return Array; } };",
            "entries.filter(() => true)",
            &[],
        ),
    ] {
        assert_early_iteration_rejected(declarations, &[setup], collection, "", names);
    }
}

#[test]
fn iteration_results_keep_retained_element_capabilities() {
    for (collection, trailing) in [
        (
            "entries.find(entry => entry.id === 'a')",
            "observed = result.executor();",
        ),
        (
            "entries.reduce((last, entry) => entry.executor, null)",
            "observed = result();",
        ),
        (
            "entries.reduce((last, entry) => entry)",
            "observed = result.executor();",
        ),
        (
            "entries.flatMap(entry => [entry.executor])",
            "observed = result[0]();",
        ),
        (
            "entries.flatMap(entry => entry.executor)",
            "observed = result[1]();",
        ),
    ] {
        assert_early_iteration_rejected_with_executor("", &[], collection, trailing, &[], "later");
    }
}

#[test]
fn callback_writes_stay_ordered_before_later_readers() {
    let source = "let later = 0;\nlet seen = 0;\nconst entries = [{ id: 'a' }, { id: 'b' }];\nentries.some(entry => { seen += 1; return entry.id === 'b'; });\nconst copy = seen;\nlater = 42;\nconsole.log(copy, seen, later);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "registry",
                &[Member::new("seen"), Member::new("entries")],
                &["entries.some(entry => { seen += 1; return entry.id === 'b'; });"],
            ),
            logical_module("readers", &[Member::new("copy")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2 2 42\n");
}

#[test]
fn actual_iteration_callback_invocation_keeps_early_reads() {
    for collection in [
        "entries.forEach(entry => { observed = entry.executor(); })",
        "entries.filter(entry => { observed = entry.executor(); return true; })",
        "entries.map(entry => { observed = entry.executor(); return entry.id; })",
        "entries.some(entry => { observed = entry.executor(); return false; })",
        "entries.every(entry => { observed = entry.executor(); return true; })",
        "entries.find(entry => { observed = entry.executor(); return false; })",
        "entries.findIndex(entry => { observed = entry.executor(); return false; })",
        "entries.flatMap(entry => { observed = entry.executor(); return []; })",
        "entries.reduce((total, entry) => { observed = entry.executor(); return total; }, 0)",
        "entries.reduce((first, entry) => { observed = entry.executor(); return first; })",
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
        "entries.some((entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); return false; })",
        "entries.reduce((total, entry, index, array) => { if (index === 0) array[1] = { executor: () => later }; else observed = entry.executor(); return total; }, 0)",
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
    for (setup, collection) in [
        (
            "entries.forEach = callback => { observed = later; };",
            "entries.forEach(() => {})",
        ),
        (
            "entries.some = callback => { observed = later; };",
            "entries.some(() => false)",
        ),
        (
            "entries.reduce = function () { observed = later; };",
            "entries.reduce((total) => total, 0)",
        ),
    ] {
        assert_early_iteration_rejected("", &[setup], collection, "", &[]);
    }
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
