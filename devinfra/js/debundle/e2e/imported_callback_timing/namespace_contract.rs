//! Namespace factory contracts stay conservative under mutation, aliasing and early result use.

use super::{
    NAMESPACE_FACTORY, expect_namespace_factory_contract_rejection,
    expect_namespace_factory_rejection, modules, peer_api_callback_factories,
};
use debundle_e2e_support::*;

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
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: value => callback(value) }; },\n\
                 createRef() { return {}; },\n\
                 other: 'member',\n\
             };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_keeps_fresh_container_identity_separate_from_contents() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const holder = { api, metadata: 0 };\n\
             holder.metadata = 1;\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(holder.metadata, wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["holder", "wrapped"]),
                    &["holder.metadata = 1;"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "1 42\n");
}

#[test]
fn namespace_inside_fresh_container_can_still_replace_a_factory_member() {
    let source = "import { api } from './peer.js';\n\
                  let later = 0;\n\
                  const holder = { api, metadata: 0 };\n\
                  holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };\n\
                  const wrapped = api.make(() => later);\n\
                  later = 42;\n\
                  console.log(globalThis.observed, wrapped.invoke());\n";
    let peer = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let purity = [peer_api_callback_factories(&["make"]).build()];
    let write = "holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };";
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &binding_members(["holder", "wrapped", "later"]),
                &[write, "later = 42;"],
            )],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["holder", "wrapped"]),
                    &[write],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_container_publication_and_uncertain_receivers_remain_conservative() {
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
        let peer = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let purity = [peer_api_callback_factories(&["make"]).build()];
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &binding_members(["holder", "wrapped", "later"]),
                    &side_effects
                        .iter()
                        .copied()
                        .chain(["later = 42;"])
                        .collect::<Vec<_>>(),
                )],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
        );
        assert_preserved_entry_output(&colocated, expected);
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["holder", "wrapped"]),
                        &side_effects,
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_contract_stays_conservative_when_callback_returns_namespace() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ field: api, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        ),
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        &["make"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_stays_conservative_for_namespace_aliases() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const namespace = api;\n\
                 return { field: namespace, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        ),
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        &["make"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_computed_namespace_mutation() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 api[dynamic] = callback => callback();\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"]),
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        &["make"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_dynamic_write_through_member_alias() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const target = api.other;\n\
                 target[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"]),
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        &["make"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_unknown_call_results_as_write_receivers() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const receiver = lookup();\n\
                 receiver[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"]),
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        &["make"],
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
        expect_namespace_factory_contract_rejection(
            FixtureOpts::new(&source, modules()),
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: { value: 'member' } };\n",
            &["make"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_contract_rejects_prototype_setter_member_values() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ __proto__: api.other, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            modules(),
        ),
        "export const api = { make(callback) { return { invoke: () => callback() }; }, other: {} };\n",
        &["make"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_composed_result_invoked_early_is_rejected() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             const outcome = wrapped.invoke();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "outcome"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        NAMESPACE_FACTORY,
        &["make", "wrap"],
        &[],
    );
}

#[test]
fn namespace_factory_contract_does_not_trust_a_replaced_member() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             api.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        ),
        NAMESPACE_FACTORY,
        &["make"],
        &[],
    );
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
fn namespace_result_published_before_initialization_is_rejected() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             globalThis.saved = wrapped;\n\
             const outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "outcome"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        NAMESPACE_FACTORY,
        &["make", "wrap"],
        &[],
    );
}

#[test]
fn unlisted_namespace_member_stays_opaque() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.other(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        ),
        "export const api = { other(callback) { return { invoke: () => callback() }; } };\n",
        &["make"],
        &[],
    );
}

#[test]
fn namespace_alias_mutation_disables_the_factory_contract() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const alias = api;\n\
             alias.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["alias", "wrapped"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        NAMESPACE_FACTORY,
        &["make"],
        &[],
    );
}

#[test]
fn altered_retained_object_cannot_hide_an_early_accessor_read() {
    expect_namespace_factory_contract_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const inner = api.make(() => 0);\n\
             const configured = Object.defineProperty(inner, 'invoke', { get() { return later; } });\n\
             const wrapped = api.wrap(inner);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["inner", "configured", "wrapped"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        NAMESPACE_FACTORY,
        &["make", "wrap"],
        &[],
    );
}

#[test]
fn namespace_factory_ignores_adjacent_fresh_error_metadata_writes() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
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
            modules(),
        ).with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 factory 1 2\n");
}

#[test]
fn fresh_error_metadata_proof_keeps_namespace_mutation_conservative() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
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
            &["callbacks/wrapped", "values/later", "residual"][..],
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
        let mut callback_members = vec![
            Member::new("wrapped"),
            Member::new("helper"),
            Member::new("eager"),
        ];
        if has_global_alias {
            callback_members.push(Member::new("globalAlias"));
        }
        let mut callback_statements = vec!["helper(api, eager);"];
        if let Some(setup) = setup {
            callback_statements.push(setup);
        }
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &callback_members,
                        &callback_statements,
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_import_order(import_order)
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}

#[test]
fn fresh_error_metadata_proof_requires_every_write_to_be_adjacent() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
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
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, modules())
                .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "later"]),
                )],
            )
            .with_import_order(&["callbacks/wrapped", "residual"])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
    }
}

#[test]
fn destructuring_namespace_member_writes_cannot_hide_early_callback_reads() {
    const PEER: &str =
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n";
    for (setup, peer) in [
        ("({ make: api.make } = { make: eager });", PEER),
        ("[api.make] = [eager];", PEER),
        ("({ make: api.make = eager } = {});", PEER),
        ("[api.make = eager] = [];", PEER),
        (
            "({ nested: { make: api.make } } = { nested: { make: eager } });",
            PEER,
        ),
        ("[[api.make]] = [[eager]];", PEER),
        ("({ make: api['make'] } = { make: eager });", PEER),
        ("for ({ make: api.make } of [{ make: eager }]) {}", PEER),
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
        let peers = [("static/peer", peer)];
        let contracts = [peer_api_callback_factories(&["make"]).build()];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["wrapped", "eager"]),
                        &[setup],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_import_order(&["callbacks/wrapped", "values/later"])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}

#[test]
fn unrelated_destructuring_member_writes_keep_factory_callbacks_deferred() {
    let source = "import { api } from './peer.js';\n\
                  const target = {};\n\
                  ({ kind: target.kind } = { kind: 'metadata' });\n\
                  const wrapped = api.make(() => later);\n\
                  const later = 42;\nconsole.log(wrapped.invoke(), target.kind);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "target"]),
                    &["({ kind: target.kind } = { kind: 'metadata' });"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
    );
    assert_preserved_entry_output(&fixture, "42 metadata\n");
}

#[test]
fn destructuring_member_values_keep_namespace_publication_visible() {
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
        let peers = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let contracts = [peer_api_callback_factories(&["make"]).build()];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["wrapped", "target", "eager"]),
                        &[setup, "target.value.make = eager;"],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_import_order(&["callbacks/wrapped", "values/later"])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}
