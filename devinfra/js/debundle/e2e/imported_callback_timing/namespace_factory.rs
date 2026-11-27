//! Namespace callback factories compose without eager callback reads and keep tracking namespace carriers.

use super::{API_MAKE_PEER, NAMESPACE_FACTORY, SplitCase, peer_api_callback_factories};
use debundle_e2e_support::*;

const NO_CAUSES: &[&str] = &[];
const EAGER_SEQUENCED: &[&str] = &["eager_use", "sequenced"];
const WITH_RESIDUAL: &[&str] = &["callbacks/wrapped", "values/later", "residual"];

const REGISTER_CALLBACK_PEER: &str = r#"export const api = {
make(callback) { return { invoke: () => callback() }; },
register(callback) { return callback; },
};
"#;

#[test]
fn namespace_factory_admits_split_around_unrelated_values() {
    let composing = [("static/peer", NAMESPACE_FACTORY)];
    let api = [("static/peer", API_MAKE_PEER)];
    let other_member = [(
        "static/peer",
        "export const api = {\n\
             make(callback) { return { invoke: () => callback() }; },\n\
             other: 'member',\n\
         };\n",
    )];
    let register = [("static/peer", REGISTER_CALLBACK_PEER)];
    let two_namespaces = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
         export const other = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let make = [peer_api_callback_factories(&["make"]).build()];
    let make_wrap = [peer_api_callback_factories(&["make", "wrap"]).build()];
    let both_namespaces = [peer_api_callback_factories(&["make"])
        .with_callback_factory_members("other", &["make"])
        .build()];
    for (case, output) in [
        (
            SplitCase {
                name: "namespace factories compose without eager callback reads",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.wrap(api.make(() => later));\n\
                         const later = 42;\nconsole.log(wrapped.invoke());\n",
                wrapped: &["wrapped"],
                peers: &composing,
                contracts: &make_wrap,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "local subclass prototype argument",
                source: "import { api } from './peer.js';\n\
                         class LocalBase {}\n\
                         class LocalChild extends LocalBase {}\n\
                         function observe(value) { void value; }\n\
                         observe(LocalChild.prototype);\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\nconsole.log(wrapped.invoke());\n",
                wrapped: &["wrapped", "LocalBase", "LocalChild", "observe"],
                wrapped_statements: &["observe(LocalChild.prototype);"],
                peers: &composing,
                contracts: &make,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "self-initialized unrelated object",
                source: "import { api } from './peer.js';\n\
                         var state = state || { value: 0 };\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\nconsole.log(wrapped.invoke(), state.value);\n",
                wrapped: &["state", "wrapped"],
                peers: &composing,
                contracts: &make,
                ..Default::default()
            },
            "42 0\n",
        ),
        (
            SplitCase {
                name: "adjacent fresh own-field writes",
                source: r#"import { api } from './peer.js';
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
                wrapped: &["Provider", "provider", "wrapped"],
                wrapped_statements: &["provider.limit = 1 / 0;", "provider.candidates = () => [];"],
                peers: &composing,
                contracts: &make,
                ..Default::default()
            },
            "42 Infinity 0\n",
        ),
        (
            SplitCase {
                name: "member reads inside callback result literals",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => ({ field: api.other, value: later }));\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                peers: &other_member,
                contracts: &make,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "member callees in callback result literals",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => ({ onEvent: api.register(() => later) }));\n\
                         const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
                wrapped: &["wrapped"],
                later_unassigned: true,
                import_order: Some(&["callbacks/wrapped", "residual"]),
                peers: &register,
                contracts: &make,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "unrelated call results outside mutation receivers",
                source: "import { api } from './peer.js';\n\
                         function lookup() { return {}; }\n\
                         const wrapped = api.make(() => {\n\
                             const unrelated = lookup();\n\
                             return { unrelated, value: later };\n\
                         });\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                later_unassigned: true,
                import_order: Some(&["callbacks/wrapped", "residual"]),
                peers: &api,
                contracts: &make,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "unrelated constructor results outside mutation receivers",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => {\n\
                             const stack = new Error().stack;\n\
                             return { stack, value: later };\n\
                         });\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                later_unassigned: true,
                import_order: Some(&["callbacks/wrapped", "residual"]),
                peers: &api,
                contracts: &make,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "audited factory result in another namespace with an adjacent metadata write",
                source: "import { api, other } from './peer.js';\n\
                         const result = other.make(() => 0);\n\
                         result.label = 'factory';\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\n\
                         console.log(result.label, wrapped.invoke());\n",
                wrapped: &["result", "wrapped"],
                wrapped_statements: &["result.label = 'factory';"],
                peers: &two_namespaces,
                contracts: &both_namespaces,
                ..Default::default()
            },
            "factory 42\n",
        ),
    ] {
        case.assert_split_runs(output);
    }
}

#[test]
fn namespace_factory_retains_inherited_static_namespace_alias() {
    let peers = [("static/peer", NAMESPACE_FACTORY)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    let case = SplitCase {
        name: "inherited static namespace alias",
        source: r#"import { api } from './peer.js';
class LocalBase { static shared = api; }
class LocalChild extends LocalBase {}
let later = 0;
LocalChild.shared.make = callback => callback();
const wrapped = api.make(() => later);
later = 42;
console.log(wrapped);
"#,
        wrapped: &["LocalBase", "LocalChild", "wrapped"],
        wrapped_statements: &["LocalChild.shared.make = callback => callback();"],
        later_write: true,
        peers: &peers,
        contracts: &contracts,
        ..Default::default()
    };
    case.assert_colocated_runs("0\n");
    case.assert_split_rejected(EAGER_SEQUENCED);
}

#[test]
fn namespace_factory_keeps_unproven_instance_writes_conservative() {
    let peers = [("static/peer", NAMESPACE_FACTORY)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for prefix in [
        "class Provider { value = 0; constructor() {} }\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Base { value = 0; }\nclass Provider extends Base {}\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ninspect(provider);\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ndelete provider.value;\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.other = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.value = computeValue();\n",
    ] {
        SplitCase {
            name: prefix,
            source: &format!(
                "import {{ api }} from './peer.js';\n\
                 function inspect(value) {{ return value; }}\n\
                 function computeValue() {{ return 0; }}\n{prefix}\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\nconsole.log(wrapped.invoke());\n"
            ),
            wrapped: &["wrapped"],
            import_order: Some(WITH_RESIDUAL),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_rejected(EAGER_SEQUENCED);
    }
}

#[test]
fn audited_export_factory_result_allows_an_adjacent_metadata_write() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
         export function outer(callback) {\n\
             function wrapped() { return callback(); }\n\
             wrapped.kind = 'factory';\nreturn wrapped;\n}\n",
    )];
    for (name, contract) in [
        (
            "callback factory",
            ChunkExportPurityBuilder::new("static/peer").with_callback_factories(&["outer"]),
        ),
        (
            "inline callback factory",
            ChunkExportPurityBuilder::new("static/peer").with_inline_callback_factories(&["outer"]),
        ),
    ] {
        let contracts = [contract
            .with_callback_factory_members("api", &["make"])
            .build()];
        SplitCase {
            name,
            source: "import { api, outer } from './peer.js';\n\
                     const result = outer(() => 0);\n\
                     result.label = 'factory';\n\
                     const wrapped = api.make(() => later);\n\
                     const later = 42;\n\
                     console.log(result.label, wrapped.invoke());\n",
            wrapped: &["result", "wrapped"],
            wrapped_statements: &["result.label = 'factory';"],
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_runs("factory 42\n");
    }
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
    SplitCase {
        name: "opaque mutation before the metadata write",
        source: SOURCE,
        wrapped: &["result", "wrapped"],
        wrapped_statements: &["install_label_setter(result);", "result.label = 'factory';"],
        later_write: true,
        peers: &peers,
        contracts: &contracts,
        ..Default::default()
    }
    .assert_split_rejected(NO_CAUSES);
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
    SplitCase {
        name: "replaced factory returns another namespace",
        source: SOURCE,
        wrapped: &["result", "wrapped"],
        wrapped_statements: &[
            "other.make = getNamespace;",
            "result.label = 'factory';",
            "result.make = callback => callback();",
        ],
        later_write: true,
        peers: &peers,
        contracts: &contracts,
        ..Default::default()
    }
    .assert_split_rejected(NO_CAUSES);
}

#[test]
fn namespace_factory_keeps_unproven_callback_bodies_conservative() {
    let other_member = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
    )];
    let register = [("static/peer", REGISTER_CALLBACK_PEER)];
    let make = [peer_api_callback_factories(&["make"]).build()];
    for case in [
        SplitCase {
            name: "unrelated constructor write receiver",
            source: "import { api } from './peer.js';\n\
                     const wrapped = api.make(() => {\n\
                         const receiver = new Error();\n\
                         receiver.value = 0;\n\
                         return { field: api.other, value: later };\n\
                     });\n\
                     const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            wrapped: &["wrapped"],
            peers: &other_member,
            contracts: &make,
            ..Default::default()
        },
        SplitCase {
            name: "computed member callee in a callback result literal",
            source: "import { api } from './peer.js';\n\
                     const wrapped = api.make(() => ({ onEvent: api[method](() => later) }));\n\
                     const method = 'register';\n\
                     const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
            wrapped: &["wrapped"],
            later_unassigned: true,
            import_order: Some(&["callbacks/wrapped", "residual"]),
            peers: &register,
            contracts: &make,
            ..Default::default()
        },
    ] {
        case.assert_split_rejected(EAGER_SEQUENCED);
    }
}

#[test]
fn namespace_factory_tracks_namespace_carriers() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for (name, prefix) in [
        (
            "self-initializer seed",
            "var state = state || api;\n\
             globalThis.exposed = state;\n",
        ),
        (
            "local class field",
            "class Holder { alias = api; }\n\
             const holder = new Holder();\n\
             globalThis.exposed = holder.alias;\n",
        ),
        (
            "static class field",
            "class Holder { static alias = api; }\n\
             globalThis.exposed = Holder.alias;\n",
        ),
        (
            "private class field",
            "class Holder { #alias = api; read() { return this.#alias; } }\n\
             globalThis.exposed = new Holder().read();\n",
        ),
        (
            "method result",
            "class Holder { read() { return api; } }\n\
             globalThis.exposed = new Holder().read();\n",
        ),
        (
            "constructor field write",
            "class Holder { constructor() { this.alias = api; } }\n\
             globalThis.exposed = new Holder().alias;\n",
        ),
        (
            "constructor return override",
            "class Holder { constructor() { return { alias: api }; } }\n\
             globalThis.exposed = new Holder().alias;\n",
        ),
        (
            "inherited class field",
            "class Base { alias = api; }\n\
             class Holder extends Base {}\n\
             globalThis.exposed = new Holder().alias;\n",
        ),
        (
            "reassigned constructor binding",
            "class Holder { alias = api; }\n\
             class Empty {}\n\
             let Factory = Holder;\n\
             Factory = Empty;\n\
             globalThis.exposed = new Factory().alias;\n",
        ),
        (
            "conditional constructor binding",
            "class Holder { alias = api; }\n\
             class Empty {}\n\
             const choose = true;\n\
             let Factory = choose ? Holder : Empty;\n\
             globalThis.exposed = new Factory().alias;\n",
        ),
        (
            "nested class field payload",
            "class Holder { payload = { alias: api }; }\n\
             globalThis.exposed = new Holder().payload.alias;\n",
        ),
        (
            "self-referential static field",
            "class Holder { static value = Holder.value; }\n\
             globalThis.exposed = Holder.value;\n",
        ),
        (
            "object destructuring",
            "const { alias } = { alias: api };\n\
             globalThis.exposed = alias;\n",
        ),
        (
            "assignment destructuring default",
            "let alias;\n\
             ({ alias = api } = {});\n\
             globalThis.exposed = alias;\n",
        ),
        (
            "unknown destructuring",
            "const { alias } = globalThis.untrusted?.namespace;\n\
             globalThis.exposed = alias;\n",
        ),
        (
            "unknown binding",
            "const alias = globalThis.untrusted?.namespace;\n\
             globalThis.exposed = alias;\n",
        ),
        (
            "unknown optional call",
            "const alias = globalThis.untrusted?.();\n\
             globalThis.exposed = alias;\n",
        ),
        (
            "array destructuring",
            "const [alias] = [api];\n\
             globalThis.exposed = alias;\n",
        ),
    ] {
        SplitCase {
            name,
            source: &format!(
                "import {{ api }} from './peer.js';\n{prefix}\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\nconsole.log(wrapped.invoke());\n"
            ),
            wrapped: &["wrapped"],
            import_order: Some(WITH_RESIDUAL),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_rejected(EAGER_SEQUENCED);
    }
}

#[test]
fn namespace_factory_class_field_alias_observes_api_mutation_at_runtime() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    SplitCase {
        name: "class field alias mutation",
        source: "import { api } from './peer.js';\n\
                 class Holder { alias = api; }\n\
                 globalThis.exposed = new Holder().alias;\n\
                 globalThis.exposed.make = callback => {\n\
                     globalThis.changed = true;\n\
                     return { invoke: callback };\n\
                 };\n\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\n\
                 console.log(globalThis.changed, wrapped.invoke());\n",
        peers: &peers,
        contracts: &contracts,
        ..Default::default()
    }
    .assert_residual_runs("true 42\n");
}
