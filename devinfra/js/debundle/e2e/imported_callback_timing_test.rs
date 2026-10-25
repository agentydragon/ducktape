//! Imported wrappers keep callback-body reads deferred until invocation.

use analysis::DepKind;
use debundle_e2e_support::*;

const SOURCE: &str = r#"import { factory } from './peer.js';
const wrapped = factory(() => later);
const later = 42;
console.log(wrapped());
export { wrapped, later };
"#;

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

#[test]
fn returned_wrapper_does_not_invoke_its_callback_at_creation() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn immediate_callback_invocation_preserves_the_tdz() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn successive_factories_do_not_publish_callbacks_to_the_next_factory() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const first = factory(() => later);\n\
         const second = factory(() => later);\n\
         const later = 42;\nconsole.log(first(), second());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("first"), Member::new("second")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn invoking_returned_wrapper_before_initialization_is_rejected() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             const outcome = wrapped();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn escaped_wrapper_can_be_invoked_by_a_later_opaque_call() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             globalThis.saved = wrapped;\nconst outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn reassigned_provider_does_not_inherit_the_original_factory_summary() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\nfactory = callback => callback();\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_shadowing_parameter_does_not_inherit_the_import_contract() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             function invoke(factory) { return factory(() => later); }\n\
             const outcome = invoke(callback => callback());\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("outcome")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

const OBJECT_SOURCE: &str = "import { factory } from './peer.js';\n\
    const wrapped = factory(() => later);\n\
    const later = 42;\nconsole.log(wrapped.invoke());\n";

const OBJECT_FACTORY: &str = "let creations = 0;\n\
    export function factory(callback) { creations++; return { invoke: () => callback() }; }\n";

#[test]
fn audited_factory_keeps_callbacks_in_its_returned_object() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn invoked_factory_result_does_not_run_its_callback_after_await_synchronously() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             import { consume } from './host.js';\n\
             const wrapped = factory(async () => { await 0; return later; });\n\
             const consumed = consume(wrapped);\n\
             const later = 42;\nconsole.log(later);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("consumed")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[
            ("static/peer", OBJECT_FACTORY),
            (
                "static/host",
                "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
            ),
        ])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn invoked_factory_result_keeps_synchronous_callback_and_returned_callback_reads() {
    for (callback, consumer) in [
        (
            "async () => { globalThis.seen = later; await 0; }",
            "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
        ),
        (
            "() => () => { globalThis.seen = later; }",
            "export function consume(value) { return Reflect.apply(Reflect.apply(value.invoke, value, []), null, []); }\n",
        ),
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    "import {{ factory }} from './peer.js';\n\
                     import {{ consume }} from './host.js';\n\
                     let later = 0;\nconst wrapped = factory({callback});\n\
                     const consumed = consume(wrapped);\nlater = 42;\n\
                     console.log(globalThis.seen, later);\n"
                ),
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[Member::new("wrapped"), Member::new("consumed")],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", OBJECT_FACTORY), ("static/host", consumer)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factories(&["factory"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn unknown_complex_factory_remains_conservative() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(OBJECT_SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callback_factory_contract_does_not_apply_to_an_existing_callback() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const callback = () => later;\nconst wrapped = factory(callback);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("callback")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn purity_alone_does_not_defer_callbacks() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules())
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callbacks_written_to_the_returned_value_remain_observable() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => 0);\nwrapped.read = () => later;\n\
             const outcome = wrapped.read();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn import_and_local_aliases_preserve_factory_identity() {
    let peers = [(
        "static/peer",
        "function build(callback) { return () => callback(); }\nexport { build as factory };\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory as create } from './peer.js';\nconst alias = create;\n\
         const wrapped = alias(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn result_mutation_cannot_hide_an_early_getter_read() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    for (property, read) in [
        ("__proto__", "wrapped.value"),
        ("options", "wrapped.options.value"),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             const wrapped = factory(() => 0);\n\
             const configured = (wrapped.{property} = {{ get value() {{ return later; }} }});\n\
             const outcome = {read};\nconst later = 42;\nconsole.log(outcome);\n",
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[
                            Member::new("wrapped"),
                            Member::new("configured"),
                            Member::new("outcome"),
                        ],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn primitive_metadata_writes_keep_the_callback_deferred() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const named = (wrapped.label = 'factory');\n\
         const later = 42;\nconsole.log(wrapped(), wrapped.label);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("named")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 factory\n");
}

const NAMESPACE_FACTORY: &str = "export const api = {\n\
    make(callback) { return { invoke: () => callback() }; },\n\
    wrap(inner) { return { invoke: () => inner.invoke() }; },\n\
};\n";

#[test]
fn namespace_factories_compose_without_eager_callback_reads() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_defers_member_reads_inside_callback_result_literals() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ field: api.other, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 other: 'member',\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_member_callees_in_callback_result_literals() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ onEvent: api.register(() => later) }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 register(callback) { return callback; },\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_keeps_computed_member_callees_conservative_in_literals() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ onEvent: api[method](() => later) }));\n\
             const method = 'register';\n\
             const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 register(callback) { return callback; },\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn unrelated_primitive_operator_results_do_not_disable_namespace_factories() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(name, value) {\n\
                 Symbol.for('prefix' + name);\n\
                 return 1 / 0 + (value + 1) + (value > 0) + typeof value + void value + !value + value++;\n\
             }\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn primitive_operator_operands_keep_callback_reentry_visible() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(read) { return Symbol.for('prefix' + read()); }\n\
             const key = makeKey(() => later);\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(key.description, wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("key"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn primitive_operator_coercion_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({ [Symbol.toPrimitive]() { return later; } });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn ordinary_to_primitive_fallback_keeps_key_with_later_value() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({\n\
                 valueOf() { return {}; },\n\
                 toString() { return later; },\n\
             });\n\
             later = 42;\n\
             console.log(key, later);\n",
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn compound_assignment_member_target_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const box = { value: { [Symbol.toPrimitive]() { return later; } } };\n\
                 const key = box.value += 'x';\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("box"), Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("box"), Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0x 42\n");
}

#[test]
fn symbol_to_primitive_getter_return_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({\n\
                     get [Symbol.toPrimitive]() { return () => later; },\n\
                 });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn regex_literal_with_custom_to_primitive_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const object = /x/;\n\
                 object[Symbol.toPrimitive] = () => later;\n\
                 const key = 'prefix' + object;\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/key",
                    &[Member::new("object"), Member::new("key")],
                    &["object[Symbol.toPrimitive] = () => later;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[
                Member::new("object"),
                Member::new("key"),
                Member::new("later"),
            ],
            &["object[Symbol.toPrimitive] = () => later;", "later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
    let graph = fixture.owner_graph();
    let key = owner_for_binding(&graph, "key");
    let later = owner_for_binding(&graph, "later");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == key
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn strict_equality_and_known_primitive_operands_skip_coercion_callbacks() {
    let fixture = run_fixture(FixtureOpts::new(
        "const object = { [Symbol.toPrimitive]() { return later; } };\n\
         const same = object === null;\n\
         const key = 'prefix' + (1 + 2);\n\
         const later = 42;\n\
         console.log(same, key, later);\n",
        vec![
            logical_module("values/key", &[Member::new("same"), Member::new("key")]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "false prefix3 42\n");
    let graph = fixture.owner_graph();
    let comparison = owner_for_binding(&graph, "same");
    let later = owner_for_binding(&graph, "later");
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == comparison
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn logical_operator_results_still_preserve_namespace_alternatives() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => api || later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_allows_audited_method_results_and_unrelated_callback_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(ref => {\n\
                 ref.current[dynamic] = api.createRef();\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'slot';\n\
             const later = 42;\n\
             console.log(wrapped.invoke({ current: {} }).value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: value => callback(value) }; },\n\
                 createRef() { return {}; },\n\
                 other: 'member',\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_contract_stays_conservative_when_callback_returns_namespace() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ field: api, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_stays_conservative_for_namespace_aliases() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const namespace = api;\n\
                 return { field: namespace, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_computed_namespace_mutation() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 api[dynamic] = callback => callback();\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_dynamic_write_through_member_alias() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const target = api.other;\n\
                 target[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_unknown_call_results_as_write_receivers() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const receiver = lookup();\n\
                 receiver[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_stays_conservative_for_other_member_shapes() {
    for member_read in ["api['other']", "api.other.value"] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             const wrapped = api.make(() => ({{ field: {member_read}, value: later }}));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[(
                "static/peer",
                "export const api = { make(callback) { return { invoke: () => callback() }; }, other: { value: 'member' } };\n",
            )])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factory_members("api", &["make"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_contract_rejects_prototype_setter_member_values() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ __proto__: api.other, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: {} };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_composed_result_invoked_early_is_rejected() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             const outcome = wrapped.invoke();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_factory_contract_does_not_trust_a_replaced_member() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             api.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn pure_member_assertion_alone_does_not_defer_callback_reads() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_pure_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_result_published_before_initialization_is_rejected() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             globalThis.saved = wrapped;\n\
             const outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn unlisted_namespace_member_stays_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.other(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { other(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_alias_mutation_disables_the_factory_contract() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const alias = api;\n\
             alias.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("alias"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn altered_retained_object_cannot_hide_an_early_accessor_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const inner = api.make(() => 0);\n\
             const configured = Object.defineProperty(inner, 'invoke', { get() { return later; } });\n\
             const wrapped = api.wrap(inner);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("inner"),
                        Member::new("configured"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}
