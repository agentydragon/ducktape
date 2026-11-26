//! Private namespace capabilities keep unrelated receivers independent.

use super::peer_api_callback_factories;
use debundle_e2e_support::*;

#[test]
fn private_namespace_capabilities_keep_unrelated_receivers_independent() {
    let source = "import { api } from './peer.js';\n\
        function shadow(api) { return api.unlisted; }\n\
        class Buffer { get value() { return this.item; } set(value) { this.item = value; } }\n\
        const buffer = new Buffer();\n\
        const marker = api.marker;\n\
        const children = api.children.list([1, 2]);\n\
        class Local extends api.Base {}\n\
        const wrapped = api.make(() => later);\n\
        const later = 42;\n\
        buffer.set(children.length);\n\
        console.log(wrapped.invoke(), marker, buffer.value, new Local().value);\n";
    for facade in [
        "export const api = backing;",
        "export const api = { get marker() { return backing.marker; }, get children() { return backing.children; }, get Base() { return backing.Base; }, get make() { return backing.make; } };",
    ] {
        let peer = format!(
            "const backing = {{ marker: 'ready', children: {{ list(items) {{ return items; }} }}, Base: class {{ value = 7; }}, make(callback) {{ return {{ invoke: () => callback() }}; }} }};{facade}\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &binding_members([
                            "shadow", "Buffer", "buffer", "marker", "children", "Local", "wrapped",
                        ]),
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", &peer)])
            .with_chunk_export_purity(&[peer_api_callback_factories(&["make"])
                .with_namespace_independent_members("api", &["make", "marker", "children", "Base"])
                .build()]),
        );
        assert_preserved_entry_output(&fixture, "42 ready 2 7\n");
    }
}

#[test]
fn namespace_capability_privacy_rejects_explicit_namespace_routes() {
    let peers = [(
        "static/peer",
        "export const api = { marker: 'ready', make(callback) { return { invoke: () => callback() }; } };\nexport const other = api;\n",
    )];
    let contracts = [peer_api_callback_factories(&["make"])
        .with_namespace_independent_members("api", &["make", "marker"])
        .with_namespace_independent_members("other", &["make", "marker"])
        .build()];
    for (declarations, setup, binding) in [
        ("", "api.make = eager;", None),
        ("const alias = api;", "alias.make = eager;", Some("alias")),
        (
            "const holder = { api };",
            "holder.api.make = eager;",
            Some("holder"),
        ),
        (
            "const holder = { __proto__: api };",
            "Object.getPrototypeOf(holder).make = eager;",
            Some("holder"),
        ),
        (
            "",
            "Object.defineProperty(api, 'make', { value: eager });",
            None,
        ),
        (
            "",
            "{ Object.defineProperty(api, 'marker', { get() { api.make = eager; return 'ready'; } }); api.marker; }",
            None,
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.call(api);",
            Some("replace"),
        ),
        (
            "function replace() { this.api.make = eager; }",
            "replace.call({ api });",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "Reflect.apply(replace, api, []);",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.apply(api, []);",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.bind(api)();",
            Some("replace"),
        ),
        ("", "({ make: api.make } = { make: eager });", None),
        ("", "[api.make] = [eager];", None),
        ("", "api['make'] = eager;", None),
        ("const alias = other;", "alias.make = eager;", Some("alias")),
    ] {
        eprintln!("namespace route: {declarations} {setup}");
        let source = format!(
            "import {{ api, other }} from './peer.js';\n\
            let later = 0;\n\
            function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
            {declarations}\n{setup}\n\
            const wrapped = api.make(() => later);\n\
            later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        // All setup belongs to wrapped; only the lexical cell and its final
        // assignment belong to the other destination.
        let mut members = vec![Member::new("eager"), Member::new("wrapped")];
        if let Some(binding) = binding {
            members.push(Member::new(binding));
        }
        let split = vec![
            logical_module_with_anon("callbacks/wrapped", &members, &[setup]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, split)
                .with_import_order(&["callbacks/wrapped", "values/later"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let colocated = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&colocated, "0\n");
    }
}

#[test]
fn namespace_capability_privacy_preserves_early_result_invocation() {
    let source = "import { api } from './peer.js';\n\
        let later = 0;\n\
        const wrapped = api.make(() => later);\n\
        const observed = wrapped.invoke();\n\
        later = 42;\nconsole.log(observed);\n";
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [peer_api_callback_factories(&["make"])
        .with_namespace_independent_members("api", &["make"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["wrapped", "observed"]),
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["callbacks/wrapped", "values/later"])
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&colocated, "0\n");
}

#[test]
fn namespace_capability_privacy_requires_total_supported_family_uses() {
    let peers = [(
        "static/peer",
        "export const api = { marker: 1, make(callback) { return { invoke: () => callback() }; } }; export const other = api;\n",
    )];
    for (use_source, complete, timing) in [
        ("const ignored = api['marker'];", true, true),
        ("const ignored = api?.marker;", true, true),
        ("const ignored = api.make?.(() => 1);", true, true),
        ("const ignored = api.marker;", false, true),
        ("const ignored = other.make;", true, true),
        ("api.marker++;", true, true),
        ("delete api.marker;", true, true),
        ("({ value: api.marker } = { value: 2 });", true, true),
        ("for (api.marker of [2]) {}", true, true),
        ("try { api = {}; } catch {}", true, true),
        (
            "try { ({ value: api } = { value: {} }); } catch {}",
            true,
            true,
        ),
        ("try { for (api of [{}]) {} } catch {}", true, true),
        ("export default api;", true, true),
        ("eval('');", true, true),
        ("const ignored = api.marker;", true, false),
    ] {
        eprintln!("namespace use: {use_source}, complete={complete}, timing={timing}");
        let source = format!(
            "import {{ api, other }} from './peer.js';\n\
            class Noise {{ set(value) {{ this.value = value; }} }}\n\
            {use_source}\n\
            const wrapped = api.make(() => later);\n\
            const later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let mut contract = ChunkExportPurityBuilder::new("static/peer")
            .with_namespace_independent_members(
                "api",
                if complete {
                    &["make", "marker"]
                } else {
                    &["make"]
                },
            );
        if timing {
            contract = contract
                .with_callback_factory_members("api", &["make"])
                .with_callback_factory_members("other", &["make"]);
        }
        let contracts = [contract.build()];
        // The empty eval is audited only to get past the existing input gate;
        // dynamic scope must still defeat the namespace privacy proof.
        let overrides: &[&str] = if use_source.starts_with("eval") {
            &["a1_eval"]
        } else {
            &[]
        };
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_import_order(&["residual"])
                .with_admission_overrides(overrides)
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let mut members = vec![Member::new("Noise"), Member::new("wrapped")];
        let setup = if use_source.starts_with("const ignored") {
            members.push(Member::new("ignored"));
            vec![]
        } else if use_source.starts_with("export") {
            vec![]
        } else {
            vec![use_source]
        };
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon("callbacks/wrapped", &members, &setup),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_import_order(&["callbacks/wrapped", "values/later"])
            .with_admission_overrides(overrides)
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}
