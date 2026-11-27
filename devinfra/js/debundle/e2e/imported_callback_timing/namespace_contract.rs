//! Namespace factory contracts stay conservative under mutation, aliasing and early result use.

use super::{
    API_MAKE_PEER, NAMESPACE_FACTORY, SplitCase, expect_namespace_factory_rejection, modules,
    peer_api_callback_factories,
};
use debundle_e2e_support::*;

const NO_CAUSES: &[&str] = &[];
const EAGER_SEQUENCED: &[&str] = &["eager_use", "sequenced"];
const WITH_RESIDUAL: &[&str] = &["callbacks/wrapped", "values/later", "residual"];

#[test]
fn namespace_factory_admits_split_around_unrelated_writes() {
    let audited_methods = [(
        "static/peer",
        "export const api = {\n\
             make(callback) { return { invoke: value => callback(value) }; },\n\
             createRef() { return {}; },\n\
             other: 'member',\n\
         };\n",
    )];
    let api_peer = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for (case, output) in [
        (
            SplitCase {
                name: "audited method results and unrelated callback receivers",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(ref => {\n\
                             ref.current[dynamic] = api.createRef();\n\
                             return { field: api.other, value: later };\n\
                         });\n\
                         const dynamic = 'slot';\n\
                         const later = 42;\n\
                         console.log(wrapped.invoke({ current: {} }).value);\n",
                wrapped: &["wrapped"],
                import_order: Some(WITH_RESIDUAL),
                peers: &audited_methods,
                contracts: &contracts,
                ..Default::default()
            },
            "42\n",
        ),
        (
            SplitCase {
                name: "fresh container identity stays separate from its contents",
                source: "import { api } from './peer.js';\n\
                         const holder = { api, metadata: 0 };\n\
                         holder.metadata = 1;\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\n\
                         console.log(holder.metadata, wrapped.invoke());\n",
                wrapped: &["holder", "wrapped"],
                wrapped_statements: &["holder.metadata = 1;"],
                peers: &api_peer,
                contracts: &contracts,
                ..Default::default()
            },
            "1 42\n",
        ),
        (
            SplitCase {
                name: "adjacent fresh error metadata writes",
                source: "import { api } from './peer.js';\n\
                         function helper(result, message, first, second) {\n\
                           globalThis.fixtureValue = 1;\n\
                           Object.keys(globalThis);\n\
                           Object.getOwnPropertyDescriptor(globalThis, 'Error');\n\
                           return (result = Error(message), result.name = 'factory', result.error = first, result.suppressed = second, result);\n\
                         }\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\n\
                         const error = helper(null, 'message', 1, 2);\n\
                         console.log(wrapped.invoke(), error.name, error.error, error.suppressed);\n",
                wrapped: &["wrapped"],
                import_order: Some(WITH_RESIDUAL),
                peers: &api_peer,
                contracts: &contracts,
                ..Default::default()
            },
            "42 factory 1 2\n",
        ),
        (
            SplitCase {
                name: "unrelated destructuring member writes",
                source: "import { api } from './peer.js';\n\
                         const target = {};\n\
                         ({ kind: target.kind } = { kind: 'metadata' });\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\nconsole.log(wrapped.invoke(), target.kind);\n",
                wrapped: &["wrapped", "target"],
                wrapped_statements: &["({ kind: target.kind } = { kind: 'metadata' });"],
                peers: &api_peer,
                contracts: &contracts,
                ..Default::default()
            },
            "42 metadata\n",
        ),
    ] {
        case.assert_split_runs(output);
    }
}

#[test]
fn namespace_container_publication_and_uncertain_receivers_remain_conservative() {
    let peer = [("static/peer", API_MAKE_PEER)];
    let purity = [peer_api_callback_factories(&["make"]).build()];
    let write = "holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };";
    let mut cases = vec![(
        "namespace inside a fresh container can still replace a factory member".to_string(),
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const holder = { api, metadata: 0 };\n\
         holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };\n\
         const wrapped = api.make(() => later);\n\
         later = 42;\n\
         console.log(globalThis.observed, wrapped.invoke());\n"
            .to_string(),
        vec![write],
        "0 42\n",
    )];
    for (setup, side_effects, expected) in [
        (
            "const holder = { api }; globalThis.published = holder;",
            vec!["globalThis.published = holder;"],
            "undefined 42\n",
        ),
        (
            "let holder = { metadata: 0 }; holder = api; holder.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder = api;",
                "holder.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { get member() { return api; } }; holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { member: 0 }; Object.defineProperty(holder, 'member', { get: () => api }); holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "Object.defineProperty(holder, 'member', { get: () => api });",
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = new Proxy({ member: api }, {}); holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { __proto__: api }; holder.__proto__.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.__proto__.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\nlet later = 0;\n{setup}\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(globalThis.observed, wrapped.invoke());\n"
        );
        cases.push((setup.to_string(), source, side_effects, expected));
    }
    for (name, source, side_effects, expected) in &cases {
        let case = SplitCase {
            name,
            source,
            wrapped: &["holder", "wrapped"],
            wrapped_statements: side_effects,
            later_write: true,
            peers: &peer,
            contracts: &purity,
            ..Default::default()
        };
        case.assert_colocated_runs(expected);
        case.assert_split_rejected(EAGER_SEQUENCED);
    }
}

#[test]
fn namespace_factory_contract_rejects_unproven_namespace_use() {
    const OTHER_PEER: &str = "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n";
    let api = [("static/peer", API_MAKE_PEER)];
    let other = [("static/peer", OTHER_PEER)];
    let nested_other = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: { value: 'member' } };\n",
    )];
    let empty_other = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: {} };\n",
    )];
    let composing = [("static/peer", NAMESPACE_FACTORY)];
    let unlisted = [(
        "static/peer",
        "export const api = { other(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let make = [peer_api_callback_factories(&["make"]).build()];
    let make_wrap = [peer_api_callback_factories(&["make", "wrap"]).build()];
    let other_member_shapes = ["api['other']", "api.other.value"].map(|member_read| {
        (
            member_read,
            format!(
                "import {{ api }} from './peer.js';\n\
                 const wrapped = api.make(() => ({{ field: {member_read}, value: later }}));\n\
                 const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            ),
        )
    });
    let mut cases = vec![
        (
            SplitCase {
                name: "callback returns the namespace",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => ({ field: api, value: later }));\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                peers: &api,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "callback returns a namespace alias",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => {\n\
                             const namespace = api;\n\
                             return { field: namespace, value: later };\n\
                         });\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                peers: &api,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "computed namespace mutation",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => {\n\
                             api[dynamic] = callback => callback();\n\
                             return { field: api.other, value: later };\n\
                         });\n\
                         const dynamic = 'make';\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                import_order: Some(WITH_RESIDUAL),
                peers: &other,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "dynamic write through a member alias",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => {\n\
                             const target = api.other;\n\
                             target[dynamic] = 0;\n\
                             return { field: api.other, value: later };\n\
                         });\n\
                         const dynamic = 'make';\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                import_order: Some(WITH_RESIDUAL),
                peers: &other,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "unknown call result as a write receiver",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => {\n\
                             const receiver = lookup();\n\
                             receiver[dynamic] = 0;\n\
                             return { field: api.other, value: later };\n\
                         });\n\
                         const dynamic = 'make';\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                import_order: Some(WITH_RESIDUAL),
                peers: &other,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "prototype setter member value",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => ({ __proto__: api.other, value: later }));\n\
                         const later = 42;\nconsole.log(wrapped.invoke().value);\n",
                wrapped: &["wrapped"],
                peers: &empty_other,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ),
        (
            SplitCase {
                name: "composed result invoked early",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.wrap(api.make(() => later));\n\
                         const outcome = wrapped.invoke();\n\
                         const later = 42;\nconsole.log(outcome);\n",
                wrapped: &["wrapped", "outcome"],
                peers: &composing,
                contracts: &make_wrap,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "replaced member",
                source: "import { api } from './peer.js';\n\
                         api.make = callback => callback();\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\nconsole.log(wrapped);\n",
                wrapped: &["wrapped"],
                peers: &composing,
                contracts: &make,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "result published before initialization",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.wrap(api.make(() => later));\n\
                         globalThis.saved = wrapped;\n\
                         const outcome = host();\n\
                         const later = 42;\nconsole.log(outcome);\n",
                wrapped: &["wrapped", "outcome"],
                peers: &composing,
                contracts: &make_wrap,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "unlisted member stays opaque",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.other(() => later);\n\
                         const later = 42;\nconsole.log(wrapped);\n",
                wrapped: &["wrapped"],
                peers: &unlisted,
                contracts: &make,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "namespace alias mutation",
                source: "import { api } from './peer.js';\n\
                         const alias = api;\n\
                         alias.make = callback => callback();\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\nconsole.log(wrapped);\n",
                wrapped: &["alias", "wrapped"],
                peers: &composing,
                contracts: &make,
                ..Default::default()
            },
            NO_CAUSES,
        ),
        (
            SplitCase {
                name: "altered retained object hides an early accessor read",
                source: "import { api } from './peer.js';\n\
                         const inner = api.make(() => 0);\n\
                         const configured = Object.defineProperty(inner, 'invoke', { get() { return later; } });\n\
                         const wrapped = api.wrap(inner);\n\
                         const later = 42;\nconsole.log(wrapped.invoke());\n",
                wrapped: &["inner", "configured", "wrapped"],
                peers: &composing,
                contracts: &make_wrap,
                ..Default::default()
            },
            NO_CAUSES,
        ),
    ];
    for (member_read, source) in &other_member_shapes {
        cases.push((
            SplitCase {
                name: member_read,
                source,
                wrapped: &["wrapped"],
                peers: &nested_other,
                contracts: &make,
                ..Default::default()
            },
            EAGER_SEQUENCED,
        ));
    }
    for (case, causes) in cases {
        case.assert_split_rejected(causes);
    }
}

#[test]
fn pure_member_assertion_alone_does_not_defer_callback_reads() {
    expect_namespace_factory_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_renames(chunk_rename_with_pure_members(
            "api",
            "api",
            Some("import_specifier"),
            &["make"],
        )),
        &[],
    );
}

#[test]
fn fresh_error_metadata_proof_keeps_namespace_mutation_conservative() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for (helper, setup, has_global_alias, import_order) in [
        (
            "function helper(result, first) { result.make = first; return (result = Error('x'), result.make = first, result); }",
            None,
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { function Error() { return api; } return (result = Error('x'), result.make = first, result); }",
            None,
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x', { cause: api }).cause, result.make = first, result); }",
            None,
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { function replace() { result = api; } return (result = Error('x'), replace(), result.make = first, result); }",
            None,
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = lookup(), result.make = first, result); } function lookup() { return api; }",
            None,
            false,
            WITH_RESIDUAL,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } globalThis.Error = function() { return api; };",
            Some("globalThis.Error = function() { return api; };"),
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } globalThis['Error'] = function() { return api; };",
            Some("globalThis['Error'] = function() { return api; };"),
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Reflect.set(globalThis, 'Error', function() { return api; });",
            Some("Reflect.set(globalThis, 'Error', function() { return api; });"),
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Object.defineProperty(globalThis, 'Error', { value: function() { return api; } });",
            Some(
                "Object.defineProperty(globalThis, 'Error', { value: function() { return api; } });",
            ),
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } const globalAlias = globalThis; globalAlias.Error = function() { return api; };",
            Some("globalAlias.Error = function() { return api; };"),
            true,
            &["callbacks/wrapped", "values/later"][..],
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Error = function() { return api; };",
            Some("Error = function() { return api; };"),
            false,
            &["callbacks/wrapped", "values/later"][..],
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n{helper}\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             helper(api, eager);\n\
             let later = 0;\nconst wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let statements: Vec<&str> = ["helper(api, eager);"].into_iter().chain(setup).collect();
        let case = SplitCase {
            name: helper,
            source: &source,
            wrapped: if has_global_alias {
                &["wrapped", "helper", "eager", "globalAlias"][..]
            } else {
                &["wrapped", "helper", "eager"]
            },
            wrapped_statements: &statements,
            later_write: true,
            import_order: Some(import_order),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        };
        case.assert_split_rejected(NO_CAUSES);
        case.assert_residual_runs("0\n");
    }
}

#[test]
fn fresh_error_metadata_proof_requires_every_write_to_be_adjacent() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for body in [
        "result.name = 'before'; return (result = Error(message), result.name = 'factory', result);",
        "return (result = Error(message), noop(), result.name = 'factory', result);",
        "return (result = Error(message), result.name = 'factory', result.name += 'suffix', result);",
        "return (result = Error(message), result.stack = 'factory', result);",
        "return (result = Error(message), result.__proto__ = null, result);",
        "return (result = Error(message), result.name = unknownValue, result);",
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function noop() {{}}\n\
             function helper(result, message) {{ {body} }}\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        SplitCase {
            name: body,
            source: &source,
            wrapped: &["wrapped"],
            import_order: Some(WITH_RESIDUAL),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_rejected(NO_CAUSES);
        SplitCase {
            name: body,
            source: &source,
            wrapped: &["wrapped", "later"],
            later_unassigned: true,
            import_order: Some(&["callbacks/wrapped", "residual"]),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_runs("42\n");
    }
}

#[test]
fn destructuring_namespace_member_writes_cannot_hide_early_callback_reads() {
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for (setup, peer) in [
        ("({ make: api.make } = { make: eager });", API_MAKE_PEER),
        ("[api.make] = [eager];", API_MAKE_PEER),
        ("({ make: api.make = eager } = {});", API_MAKE_PEER),
        ("[api.make = eager] = [];", API_MAKE_PEER),
        (
            "({ nested: { make: api.make } } = { nested: { make: eager } });",
            API_MAKE_PEER,
        ),
        ("[[api.make]] = [[eager]];", API_MAKE_PEER),
        ("({ make: api['make'] } = { make: eager });", API_MAKE_PEER),
        (
            "for ({ make: api.make } of [{ make: eager }]) {}",
            API_MAKE_PEER,
        ),
        (
            "({ ...api.methods } = { make: eager });",
            "const deferred = callback => ({ invoke: () => callback() });\n\
             export const api = { methods: { make: deferred }, make(callback) { return this.methods.make(callback); } };\n",
        ),
        (
            "[...api.methods] = [eager];",
            "const deferred = callback => ({ invoke: () => callback() });\n\
             export const api = { methods: [deferred], make(callback) { return this.methods[0](callback); } };\n",
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             let later = 0;\n{setup}\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let case = SplitCase {
            name: setup,
            source: &source,
            wrapped: &["wrapped", "eager"],
            wrapped_statements: &[setup],
            later_write: true,
            import_order: Some(&["callbacks/wrapped", "values/later"]),
            peers: &[("static/peer", peer)],
            contracts: &contracts,
            ..Default::default()
        };
        case.assert_split_rejected(NO_CAUSES);
        case.assert_residual_runs("0\n");
    }
}

#[test]
fn destructuring_member_values_keep_namespace_publication_visible() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for setup in [
        "({ value: target.value } = { value: api });",
        "({ value: target.value = api } = {});",
        "[target.value = api] = [];",
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             const target = {{}};\nlet later = 0;\n{setup}\n\
             target.value.make = eager;\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let case = SplitCase {
            name: setup,
            source: &source,
            wrapped: &["wrapped", "target", "eager"],
            wrapped_statements: &[setup, "target.value.make = eager;"],
            later_write: true,
            import_order: Some(&["callbacks/wrapped", "values/later"]),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        };
        case.assert_split_rejected(NO_CAUSES);
        case.assert_residual_runs("0\n");
    }
}
