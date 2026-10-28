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
            "const record = { get value() { return later; } };\n",
            "record",
            vec!["record"],
            vec![],
        ),
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
        (
            "let holder;\n",
            "(holder = { get value() { return later; } })",
            vec!["holder"],
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
