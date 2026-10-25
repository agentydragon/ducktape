//! Imported wrappers keep callback-body reads deferred until invocation.

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
