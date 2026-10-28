//! Own descriptor inspection stores getter/method capabilities without calling them.

use debundle_e2e_support::*;

fn descriptor_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("descriptors", &[Member::new("descriptors")]),
        logical_module("later", &[Member::new("later")]),
    ]
}

#[test]
fn ordinary_own_descriptor_inspection_defers_getter_and_method_bodies() {
    let source = "const descriptors = Object.getOwnPropertyDescriptors({ get value() { return later; }, method() { return later; } });\nconst later = 42;\nconsole.log(descriptors.value.get(), descriptors.method.value());\n";
    let fixture = run_fixture(FixtureOpts::new(source, descriptor_modules()));
    assert_preserved_entry_output(&fixture, "42 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn descriptor_flags_receivers_and_deferred_counters_are_preserved() {
    let source = "let getterCalls = 0;\nlet methodCalls = 0;\nconst descriptors = Object.getOwnPropertyDescriptors({ get value() { getterCalls++; return this.label + later; }, method() { methodCalls++; return this.label + later; } });\nconsole.log(getterCalls, methodCalls);\nconst later = 42;\nconsole.log(descriptors.value.get.call({ label: 'getter:' }), descriptors.method.value.call({ label: 'method:' }), getterCalls, methodCalls, descriptors.value.enumerable, descriptors.value.configurable, descriptors.method.writable);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "descriptors",
                &[
                    Member::new("getterCalls"),
                    Member::new("methodCalls"),
                    Member::new("descriptors"),
                ],
                &["console.log(getterCalls, methodCalls);"],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 0\ngetter:42 method:42 1 1 true true true\n");
}

fn reject_descriptor_use(
    prefix: &str,
    argument: &str,
    use_sites: &[&str],
    extra_members: &[&'static str],
    prefix_statements: &[&str],
) {
    let use_site = use_sites.join("\n");
    let source = format!(
        "{prefix}const descriptors = Object.getOwnPropertyDescriptors({argument});\n{use_site}\nconst later = 42;\n"
    );
    let mut members = vec![Member::new("descriptors")];
    members.extend(extra_members.iter().copied().map(Member::new));
    let mut statements = prefix_statements.to_vec();
    statements.extend_from_slice(use_sites);
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon("descriptors", &members, &statements),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["descriptors", "later"],
        &[],
    );
}

#[test]
fn descriptor_carrier_retains_later_calls_publication_and_writes() {
    for use_sites in [
        vec!["descriptors.value.get();"],
        vec!["descriptors.method.value();"],
        vec!["consume(descriptors);"],
        vec![
            "globalThis.saved = descriptors;",
            "consume(globalThis.saved);",
        ],
        vec!["descriptors.value.get = () => later;"],
        vec!["Object.defineProperty(descriptors, 'extra', { get value() { return later; } });"],
    ] {
        reject_descriptor_use(
            "function consume(value) { return value.value.get(); }\n",
            "{ get value() { return later; }, method() { return later; } }",
            &use_sites,
            &["consume"],
            &[],
        );
    }
}

#[test]
fn descriptor_inspection_rejects_unknown_or_unsupported_inputs() {
    for (prefix, argument, members, statements) in [
        (
            "globalThis.saved = () => later;\n",
            "globalThis.record",
            vec![],
            vec!["globalThis.saved = () => later;"],
        ),
        (
            "",
            "new Proxy({}, { ownKeys() { return later; } })",
            vec![],
            vec![],
        ),
        ("", "{ get ['value']() { return later; } }", vec![], vec![]),
        (
            "",
            "{ ...{}, get value() { return later; } }",
            vec![],
            vec![],
        ),
        (
            "",
            "{ __proto__: null, get value() { return later; } }",
            vec![],
            vec![],
        ),
    ] {
        reject_descriptor_use(prefix, argument, &[], &members, &statements);
    }
}

#[test]
fn descriptor_inspection_requires_native_operation_identity() {
    reject_descriptor_use(
        "const Object = { getOwnPropertyDescriptors(record) { return record.value; } };\n",
        "{ get value() { return later; } }",
        &[],
        &["Object"],
        &[],
    );
    reject_descriptor_use(
        "Object.getOwnPropertyDescriptors = record => record.value;\n",
        "{ get value() { return later; } }",
        &[],
        &[],
        &["Object.getOwnPropertyDescriptors = record => record.value;"],
    );
    let source = "import './replacement.js';\nconst descriptors = Object.getOwnPropertyDescriptors({ get value() { return later; } });\nconst later = 42;\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, descriptor_modules()).with_extra_chunks(&[("static/replacement", "export function replace() { Object.getOwnPropertyDescriptors = record => record.value; }\n")]),
        &["descriptors", "later"], &[],
    );
}

#[test]
fn descriptor_inspection_preserves_eager_argument_effects() {
    let source = "const effects = [];\nfunction makeValue() { effects.push('argument'); return 1; }\nconst descriptors = Object.getOwnPropertyDescriptors({ field: makeValue(), get value() { effects.push('getter'); return later; } });\nconst later = 42;\nconsole.log(effects.join(','), descriptors.field.value, descriptors.value.get(), effects.join(','));\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "descriptors",
                &[
                    Member::new("effects"),
                    Member::new("makeValue"),
                    Member::new("descriptors"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "argument 1 42 argument,getter\n");
}

#[test]
fn descriptor_inspection_preserves_argument_throws() {
    let source = "let observed;\ntry { Object.getOwnPropertyDescriptors({ field: (() => { throw new Error('argument'); })(), get value() { return later; } }); } catch (error) { observed = error.message; }\nconst later = 42;\nconsole.log(observed, later);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "argument 42\n");
}

#[test]
fn own_setter_descriptor_retains_its_deferred_body() {
    let source = "const descriptors = Object.getOwnPropertyDescriptors({ set value(next) { globalThis.observed = next + later; } });\nconst later = 42;\ndescriptors.value.set(8);\nconsole.log(globalThis.observed);\n";
    let fixture = run_fixture(FixtureOpts::new(source, descriptor_modules()));
    assert_preserved_entry_output(&fixture, "50\n");
    reject_descriptor_use(
        "",
        "{ set value(next) { globalThis.observed = next + later; } }",
        &["descriptors.value.set(8);"],
        &[],
        &[],
    );
}

#[test]
fn captured_descriptors_apply_to_a_fresh_target_without_running_accessors() {
    let source = r#"
let getterCalls = 0;
let methodCalls = 0;
function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  return Object.defineProperties(target, descriptors);
}
const record = {
  label: "copied",
  get value() { getterCalls++; return this.label + ":" + later; },
  make() { methodCalls++; return this.label + ":" + later; },
};
const result = wrap(record);
const beforeGetterCalls = getterCalls;
const beforeMethodCalls = methodCalls;
const later = "ready";
console.log(beforeGetterCalls, beforeMethodCalls, result.value, result.make(), getterCalls, methodCalls);
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("wrapper", &[Member::new("wrap")]),
            logical_module_with_anon(
                "result",
                &[
                    Member::new("getterCalls"),
                    Member::new("methodCalls"),
                    Member::new("result"),
                    Member::new("record"),
                    Member::new("beforeGetterCalls"),
                    Member::new("beforeMethodCalls"),
                ],
                &[
                    "console.log(beforeGetterCalls, beforeMethodCalls, result.value, result.make(), getterCalls, methodCalls);",
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 0 copied:ready copied:ready 1 1\n");
}

#[test]
fn direct_descriptor_application_returns_its_target_identity() {
    let source = "function wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); return Object.defineProperties({}, descriptors); }\nconst result = wrap({ get value() { return later; }, method() { return later; } });\nconst later = 42;\nconsole.log(result.value, result.method());\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("wrapper", &[Member::new("wrap")]),
            logical_module("result", &[Member::new("result")]),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn direct_descriptor_application_preserves_target_alias_identity() {
    let source = "const descriptors = Object.getOwnPropertyDescriptors({ get value() { return later; }, method() { return later; } });\nconst target = {};\nconst result = Object.defineProperties(target, descriptors);\nconst alias = result;\nconst later = 42;\nconsole.log(result === target, alias === target, alias.value, alias.method());\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("descriptors", &[Member::new("descriptors")]),
            logical_module(
                "target",
                &[
                    Member::new("target"),
                    Member::new("result"),
                    Member::new("alias"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "true true 42 42\n");
}

#[test]
fn direct_descriptor_application_retains_captured_accessor_for_stored_read() {
    let source = "function wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); return Object.defineProperties({}, descriptors); }\nconst result = wrap({ get value() { return Later; } });\nlet touched;\nlet observed;\ntry { touched = result.value; } catch (error) { observed = error.name; }\nclass Later {}\nconsole.log(observed);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[
                        Member::new("result"),
                        Member::new("touched"),
                        Member::new("observed"),
                    ],
                    &["try { touched = result.value; } catch (error) { observed = error.name; }"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["later", "result"],
        &["eager_use"],
    );
}

#[test]
fn copied_setter_assignment_retains_its_later_dependency() {
    let source = "function wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); const target = {}; Object.defineProperties(target, descriptors); return target; }\nconst result = wrap({ set value(next) { globalThis.observed = Later; } });\nconst alias = result;\nlet observed;\ntry { alias.value = 1; } catch (error) { observed = error.name; }\nclass Later {}\nconsole.log(observed);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[
                        Member::new("result"),
                        Member::new("alias"),
                        Member::new("observed"),
                    ],
                    &["try { alias.value = 1; } catch (error) { observed = error.name; }"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["later", "result"],
        &[],
    );
}

#[test]
fn direct_descriptor_application_retains_target_for_early_accessor_reads() {
    let source = "function wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); return Object.defineProperties({}, descriptors); }\nconst result = wrap({ get value() { return Later; } });\nlet observed;\ntry { result.value; } catch (error) { observed = error.name; }\nclass Later {}\nconsole.log(observed);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[Member::new("result"), Member::new("observed")],
                    &["try { result.value; } catch (error) { observed = error.name; }"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["later", "result"],
        &["eager_use"],
    );
}

#[test]
fn copied_accessor_reads_and_opaque_target_consumption_retain_dependencies() {
    let wrapper = "function wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); const target = {}; Object.defineProperties(target, descriptors); return target; }\n";
    let reads_copied_accessor = format!(
        "{wrapper}const result = wrap({{ get value() {{ return later; }}, method() {{ return later; }} }});\nconst observed = result.value;\nconst later = 42;\n"
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &reads_copied_accessor,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module("result", &[Member::new("result"), Member::new("observed")]),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["result", "later"],
        &[],
    );

    let dynamic_read = format!(
        "{wrapper}const result = wrap({{ get value() {{ return later; }} }});\nconst key = globalThis.key;\nconst observed = result[key];\nconst later = 42;\n"
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &dynamic_read,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module(
                    "result",
                    &[
                        Member::new("result"),
                        Member::new("key"),
                        Member::new("observed"),
                    ],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["result", "later"],
        &[],
    );

    let getter_as_callee = format!(
        "{wrapper}const result = wrap({{ get value() {{ return later; }} }});\nresult.value();\nconst later = 42;\n"
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &getter_as_callee,
            vec![
                logical_module_with_anon(
                    "result",
                    &[Member::new("wrap"), Member::new("result")],
                    &["result.value();"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["result", "later"],
        &[],
    );

    let opaque_consumption = format!(
        "{wrapper}const result = wrap({{ get value() {{ return later; }}, method() {{ return later; }} }});\nglobalThis.consume(result);\nconst later = 42;\n"
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &opaque_consumption,
            vec![
                logical_module_with_anon(
                    "result",
                    &[Member::new("wrap"), Member::new("result")],
                    &["globalThis.consume(result);"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["result", "later"],
        &[],
    );
}

#[test]
fn conflicting_descriptor_carrier_self_reference_terminates() {
    let source = "function capture(record) { return Object.getOwnPropertyDescriptors(record); }\nconst first = capture({});\nconst second = capture({ first });\nObject.freeze(second);\nconsole.log('done');\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "done\n");
}

#[test]
fn descriptor_application_preserves_the_source_literal_capture_context() {
    let source = "function makeRecord(read) { return { get value() { return read(); } }; }\nfunction wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); const target = {}; Object.defineProperties(target, descriptors); return target; }\nconst record = makeRecord(() => later);\nconst result = wrap(record);\nconst observed = result.value;\nconst later = 42;\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("maker", &[Member::new("makeRecord")]),
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[
                        Member::new("record"),
                        Member::new("result"),
                        Member::new("observed"),
                    ],
                    &["const observed = result.value;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["result", "later"],
        &[],
    );
}

#[test]
fn descriptor_capture_falls_back_after_a_known_own_key_is_added() {
    let source = "const record = { method() { return later; } };\n[0].forEach(() => { record.extra = 1; });\nconst descriptors = Object.getOwnPropertyDescriptors(record);\nconst target = {};\nObject.defineProperties(target, descriptors);\nconst later = 42;\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "descriptor",
                    &[
                        Member::new("record"),
                        Member::new("descriptors"),
                        Member::new("target"),
                    ],
                    &["[0].forEach(() => { record.extra = 1; });"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["later"],
        &["eager_use"],
    );
}

#[test]
fn descriptor_map_mutation_invalidates_application_but_keeps_callbacks() {
    let source = "const descriptors = Object.getOwnPropertyDescriptors({ get value() { return later; }, method() { return later; } });\ndescriptors.extra = () => later;\nconst target = {};\nObject.defineProperties(target, descriptors);\nconst later = 42;\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "descriptors",
                    &[Member::new("descriptors"), Member::new("target")],
                    &["descriptors.extra = () => later;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["later"],
        &["eager_use"],
    );
}

#[test]
fn descriptor_capture_accepts_an_assignment_of_a_fresh_ordinary_source() {
    let source = "let record;\nconst descriptors = Object.getOwnPropertyDescriptors(record = { get value() { return later; } });\nconst later = 42;\nconsole.log(descriptors.value.get());\n";
    let fixture = run_fixture(FixtureOpts::new(source, descriptor_modules()));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn copied_getter_read_invalidates_nested_dense_array_state() {
    let source = "const items = [];\nfunction wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); const target = {}; Object.defineProperties(target, descriptors); return target; }\nconst result = wrap({ items, get value() { this.items.push(1); return 0; } });\nconst touched = result.value;\nlet observed;\ntry { items.forEach(() => Later); } catch (error) { observed = error.name; }\nclass Later {}\nconsole.log(touched, observed);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[
                        Member::new("items"),
                        Member::new("result"),
                        Member::new("touched"),
                        Member::new("observed"),
                    ],
                    &[
                        "try { items.forEach(() => Later); } catch (error) { observed = error.name; }",
                    ],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["later", "result"],
        &[],
    );
}

#[test]
fn copied_getter_callee_runs_before_argument_effects() {
    let source = "const items = [];\nfunction wrap(record) { const descriptors = Object.getOwnPropertyDescriptors(record); const target = {}; Object.defineProperties(target, descriptors); return target; }\nconst result = wrap({ items, get run() { this.items.push(1); return () => 0; } });\nlet observed;\ntry { result.run(items.forEach(() => Later)); } catch (error) { observed = error.name; }\nclass Later {}\nconsole.log(observed);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapper", &[Member::new("wrap")]),
                logical_module_with_anon(
                    "result",
                    &[
                        Member::new("items"),
                        Member::new("result"),
                        Member::new("observed"),
                    ],
                    &[
                        "try { result.run(items.forEach(() => Later)); } catch (error) { observed = error.name; }",
                    ],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["later", "result"],
        &[],
    );
}

#[test]
fn opaque_descriptor_carrier_does_not_certify_a_nested_factory_input() {
    let source = "import { factory } from './peer.js';\nconst descriptors = Object.getOwnPropertyDescriptors({ nested: { get value() { return later; } } });\nconst wrapped = factory(descriptors);\nconst later = 42;\n";
    let chunks = [(
        "static/peer",
        "export function factory(record) { const eager = record.nested.value.value; return () => eager; }\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "descriptors",
                    &[Member::new("descriptors"), Member::new("wrapped")],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
        &["descriptors", "later"],
        &[],
    );
}

#[test]
fn descriptor_carrier_retains_getter_and_method_parameter_captures() {
    let make = "function inspect(callback) { return Object.getOwnPropertyDescriptors({ get value() { return callback(); }, method() { return callback(); } }); }\n";
    let source = format!(
        "{make}const descriptors = inspect(() => later);\nconst later = 42;\nconsole.log(descriptors.value.get(), descriptors.method.value());\n"
    );
    let modules = || {
        vec![
            logical_module(
                "descriptors",
                &[Member::new("inspect"), Member::new("descriptors")],
            ),
            logical_module("later", &[Member::new("later")]),
        ]
    };
    let fixture = run_fixture(FixtureOpts::new(&source, modules()));
    assert_preserved_entry_output(&fixture, "42 42\n");
    for read in ["descriptors.value.get()", "descriptors.method.value()"] {
        let source = format!(
            "{make}const descriptors = inspect(() => later);\nconst before = {read};\nconst later = 42;\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "descriptors",
                        &[
                            Member::new("inspect"),
                            Member::new("descriptors"),
                            Member::new("before"),
                        ],
                    ),
                    logical_module("later", &[Member::new("later")]),
                ],
            ),
            &["descriptors", "later"],
            &[],
        );
    }
}

#[test]
fn descriptor_data_values_retain_later_nested_object_mutations() {
    let caught = "const holder = {};\nfunction consume(record) { return record.holder.value.read(); }\nconst descriptors = Object.getOwnPropertyDescriptors({ holder });\nholder.read = () => later;\nlet observed;\ntry { consume(descriptors); } catch (error) { observed = error.name; }\nconst later = 42;\nconsole.log(observed, later);\n";
    let fixture = run_fixture(FixtureOpts::new(caught, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 42\n");
    let source = "const holder = {};\nfunction consume(record) { return record.holder.value.read(); }\nconst descriptors = Object.getOwnPropertyDescriptors({ holder });\nholder.read = () => later;\nconst before = consume(descriptors);\nconst later = 42;\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "descriptors",
                    &[
                        Member::new("holder"),
                        Member::new("consume"),
                        Member::new("descriptors"),
                        Member::new("before"),
                    ],
                    &["holder.read = () => later;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["descriptors", "later"],
        &[],
    );
}
