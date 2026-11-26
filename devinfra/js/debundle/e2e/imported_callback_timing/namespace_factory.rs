//! Namespace callback factories compose without eager callback reads and keep tracking namespace carriers.

use super::{NAMESPACE_FACTORY, modules, peer_api_callback_factories};
use debundle_e2e_support::*;

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
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make", "wrap"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_local_subclass_prototype_argument() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class LocalBase {}\n\
             class LocalChild extends LocalBase {}\n\
             function observe(value) { void value; }\n\
             observe(LocalChild.prototype);\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "LocalBase", "LocalChild", "observe"]),
                    &["observe(LocalChild.prototype);"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

const INHERITED_NAMESPACE_ALIAS: &str = r#"import { api } from './peer.js';
class LocalBase { static shared = api; }
class LocalChild extends LocalBase {}
let later = 0;
LocalChild.shared.make = callback => callback();
const wrapped = api.make(() => later);
later = 42;
console.log(wrapped);
"#;

#[test]
fn namespace_factory_retains_inherited_static_namespace_alias() {
    let fixture = run_fixture(
        FixtureOpts::new(
            INHERITED_NAMESPACE_ALIAS,
            vec![logical_module_with_anon(
                "app",
                &binding_members(["LocalBase", "LocalChild", "wrapped", "later"]),
                &[
                    "LocalChild.shared.make = callback => callback();",
                    "later = 42;",
                ],
            )],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "0\n");
}

#[test]
fn namespace_factory_rejects_split_inherited_static_namespace_alias() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            INHERITED_NAMESPACE_ALIAS,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["LocalBase", "LocalChild", "wrapped"]),
                    &["LocalChild.shared.make = callback => callback();"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_ignores_self_initialized_unrelated_object() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             var state = state || { value: 0 };\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke(), state.value);\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["state", "wrapped"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42 0\n");
}

#[test]
fn namespace_factory_allows_adjacent_fresh_own_field_writes() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { api } from './peer.js';
class Provider {
    limit = 1 / 0;
    candidates = [].values;
    async prepare() {}
}
const provider = new Provider();
provider.limit = 1 / 0;
provider.candidates = () => [];
const wrapped = api.make(() => later);
const later = 42;
console.log(wrapped.invoke(), provider.limit, provider.candidates().length);
"#,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["Provider", "provider", "wrapped"]),
                    &["provider.limit = 1 / 0;", "provider.candidates = () => [];"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42 Infinity 0\n");
}

#[test]
fn namespace_factory_tracks_namespace_seed_in_self_initializer() {
    assert_namespace_factory_publication_rejected(
        "var state = state || api;\n\
         globalThis.exposed = state;\n",
    );
}

#[test]
fn namespace_factory_keeps_unproven_instance_writes_conservative() {
    for prefix in [
        "class Provider { value = 0; constructor() {} }\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Base { value = 0; }\nclass Provider extends Base {}\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ninspect(provider);\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ndelete provider.value;\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.other = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.value = computeValue();\n",
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    "import {{ api }} from './peer.js';\n\
                     function inspect(value) {{ return value; }}\n\
                     function computeValue() {{ return 0; }}\n{prefix}\
                     const wrapped = api.make(() => later);\n\
                     const later = 42;\nconsole.log(wrapped.invoke());\n"
                ),
                modules(),
            )
            .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
            .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
            .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
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
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

const REGISTER_CALLBACK_PEER: &str = r#"export const api = {
make(callback) { return { invoke: () => callback() }; },
register(callback) { return callback; },
};
"#;

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
        .with_import_order(&["callbacks/wrapped", "residual"])
        .with_extra_chunks(&[("static/peer", REGISTER_CALLBACK_PEER)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn audited_export_factory_result_allows_an_adjacent_metadata_write() {
    for contract in [
        ChunkExportPurityBuilder::new("static/peer").with_callback_factories(&["outer"]),
        ChunkExportPurityBuilder::new("static/peer").with_inline_callback_factories(&["outer"]),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { api, outer } from './peer.js';\n\
             const result = outer(() => 0);\n\
             result.label = 'factory';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(result.label, wrapped.invoke());\n",
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["result", "wrapped"]),
                        &["result.label = 'factory';"],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[(
                "static/peer",
                "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export function outer(callback) {\n\
                 function wrapped() { return callback(); }\n\
                 wrapped.kind = 'factory';\nreturn wrapped;\n}\n",
            )])
            .with_chunk_export_purity(&[contract
                .with_callback_factory_members("api", &["make"])
                .build()]),
        );
        assert_preserved_entry_output(&fixture, "factory 42\n");
    }
}

#[test]
fn audited_factory_result_in_another_namespace_allows_an_adjacent_metadata_write() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api, other } from './peer.js';\n\
             const result = other.make(() => 0);\n\
             result.label = 'factory';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(result.label, wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["result", "wrapped"]),
                    &["result.label = 'factory';"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export const other = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"])
            .with_callback_factory_members("other", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "factory 42\n");
}

#[test]
fn an_opaque_mutation_between_factory_result_and_metadata_write_stays_conservative() {
    const SOURCE: &str = "import { api, outer } from './peer.js';\n\
        import { install_label_setter } from './host.js';\n\
        let later = 0;\n\
        const result = outer(() => later);\n\
        install_label_setter(result);\n\
        result.label = 'factory';\n\
        const wrapped = api.make(() => later);\n\
        later = 42;\n\
        console.log(result.label, globalThis.seen, wrapped.invoke(), later);\n";
    let peers = [
        (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export function outer(callback) { return { invoke: () => callback() }; }\n",
        ),
        (
            "static/host",
            "export function install_label_setter(value) {\n\
                 Object.defineProperty(value, 'label', {\n\
                     set() { globalThis.seen = value.invoke(); },\n\
                     get() { return 'factory'; },\n\
                 });\n\
             }\n",
        ),
    ];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["outer"])
        .with_callback_factory_members("api", &["make"])
        .build()];
    let preserved = run_fixture(
        FixtureOpts::new(SOURCE, vec![])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&preserved, "factory 0 42 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["result", "wrapped"]),
                    &["install_label_setter(result);", "result.label = 'factory';"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_replaced_factory_returning_another_namespace_stays_conservative() {
    const SOURCE: &str = "import { api, other } from './peer.js';\n\
        import { getNamespace } from './host.js';\n\
        other.make = getNamespace;\n\
        const result = other.make(() => 0);\n\
        result.label = 'factory';\n\
        result.make = callback => callback();\n\
        let later = 0;\n\
        const wrapped = api.make(() => later);\n\
        later = 42;\n\
        console.log(result.label, wrapped, later);\n";
    let peers = [
        (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
         export const other = { make(callback) { return { invoke: () => callback() }; } };\n",
        ),
        (
            "static/host",
            "import { api } from './peer.js';\nexport function getNamespace() { return api; }\n",
        ),
    ];
    let contracts = [peer_api_callback_factories(&["make"])
        .with_callback_factory_members("other", &["make"])
        .build()];
    let preserved = run_fixture(
        FixtureOpts::new(SOURCE, vec![])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&preserved, "factory 0 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["result", "wrapped"]),
                    &[
                        "other.make = getNamespace;",
                        "result.label = 'factory';",
                        "result.make = callback => callback();",
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_factory_ignores_unrelated_call_results_outside_mutation_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function lookup() { return {}; }\n\
             const wrapped = api.make(() => {\n\
                 const unrelated = lookup();\n\
                 return { unrelated, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_import_order(&["callbacks/wrapped", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_unrelated_constructor_results_outside_mutation_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const stack = new Error().stack;\n\
                 return { stack, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_import_order(&["callbacks/wrapped", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_keeps_unrelated_constructor_write_receivers_conservative() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const receiver = new Error();\n\
                 receiver.value = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_tracks_namespace_stored_in_local_class_field() {
    assert_namespace_factory_publication_rejected(
        "class Holder { alias = api; }\n\
         const holder = new Holder();\n\
         globalThis.exposed = holder.alias;\n",
    );
}

fn assert_namespace_factory_publication_rejected(prefix: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &format!(
                "import {{ api }} from './peer.js';\n{prefix}\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\nconsole.log(wrapped.invoke());\n"
            ),
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_tracks_class_static_private_constructor_and_inherited_carriers() {
    for prefix in [
        "class Holder { static alias = api; }\n\
         globalThis.exposed = Holder.alias;\n",
        "class Holder { #alias = api; read() { return this.#alias; } }\n\
         globalThis.exposed = new Holder().read();\n",
        "class Holder { read() { return api; } }\n\
         globalThis.exposed = new Holder().read();\n",
        "class Holder { constructor() { this.alias = api; } }\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Holder { constructor() { return { alias: api }; } }\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Base { alias = api; }\n\
         class Holder extends Base {}\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Holder { alias = api; }\n\
         class Empty {}\n\
         let Factory = Holder;\n\
         Factory = Empty;\n\
         globalThis.exposed = new Factory().alias;\n",
        "class Holder { alias = api; }\n\
         class Empty {}\n\
         const choose = true;\n\
         let Factory = choose ? Holder : Empty;\n\
         globalThis.exposed = new Factory().alias;\n",
        "class Holder { payload = { alias: api }; }\n\
         globalThis.exposed = new Holder().payload.alias;\n",
        "class Holder { static value = Holder.value; }\n\
         globalThis.exposed = Holder.value;\n",
    ] {
        assert_namespace_factory_publication_rejected(prefix);
    }
}

#[test]
fn namespace_factory_tracks_namespace_from_object_destructuring() {
    assert_namespace_factory_publication_rejected(
        "const { alias } = { alias: api };\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_tracks_default_from_object_assignment_destructuring() {
    assert_namespace_factory_publication_rejected(
        "let alias;\n\
         ({ alias = api } = {});\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_destructuring_conservative() {
    assert_namespace_factory_publication_rejected(
        "const { alias } = globalThis.untrusted?.namespace;\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_binding_conservative() {
    assert_namespace_factory_publication_rejected(
        "const alias = globalThis.untrusted?.namespace;\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_optional_call_conservative() {
    assert_namespace_factory_publication_rejected(
        "const alias = globalThis.untrusted?.();\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_array_destructuring_conservative() {
    assert_namespace_factory_publication_rejected(
        "const [alias] = [api];\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_class_field_alias_observes_api_mutation_at_runtime() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class Holder { alias = api; }\n\
             globalThis.exposed = new Holder().alias;\n\
             globalThis.exposed.make = callback => {\n\
                 globalThis.changed = true;\n\
                 return { invoke: callback };\n\
             };\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(globalThis.changed, wrapped.invoke());\n",
            vec![],
        )
        .with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "true 42\n");
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
        .with_import_order(&["callbacks/wrapped", "residual"])
        .with_extra_chunks(&[("static/peer", REGISTER_CALLBACK_PEER)])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
        &["callbacks/wrapped"],
        &["eager_use", "sequenced"],
    );
}
