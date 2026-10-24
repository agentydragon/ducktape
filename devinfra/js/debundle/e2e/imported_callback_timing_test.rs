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
