//! Source-defined record factories defer members until later use or publication.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

#[test]
fn source_empty_map_record_factory_defers_members_and_getter() {
    let peer = [(
        "static/peer",
        "import { readLater } from './app.js';\nexport function factory() { const state = new Map(); return { put(key, value) { state.set(key, value); }, get size() { return state.size; }, label() { return readLater().name; } }; }\n",
    )];
    let source = "import { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nqueue.put('answer', 42);\nconsole.log(queue.size, queue.label(), readLater().name);\nexport { queue, Later };\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![
                logical_module("queue", &binding_members(["queue", "readLater"])),
                logical_module("later", &[Member::new("Later")]),
            ],
        )
        .with_extra_chunks(&peer),
    );
    assert_preserved_entry_output(&fixture, "1 Later Later\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["factory"]["returned_record"];
    assert_eq!(summary["defining_module"], "static/peer");
    assert_eq!(summary["export"], "factory");
    assert_eq!(summary["local"], "factory");
    assert_eq!(summary["source_location"]["source_path"], "static/peer.js");
    assert_eq!(summary["members"][1]["name"], "size");
    assert_eq!(
        summary["members"][1]["captures"],
        serde_json::json!(["state"])
    );
    assert_eq!(
        summary["members"][2]["captures"],
        serde_json::json!(["readLater"])
    );
    let graph = fixture.dependency_graph();
    let certificate = &graph.imported_callable_summaries[0];
    assert!(certificate.callback_factory_evidence.is_empty());
    let record = certificate.returned_record.as_ref().unwrap();
    use analysis_vocabulary::provenance::InferenceRule;
    assert_eq!(record.inference.conclusion, InferenceRule::ReturnedRecord);
    assert_eq!(
        record.inference.derivation_rules,
        std::collections::BTreeSet::from([
            InferenceRule::NativeIntegrity,
            InferenceRule::NativeCallIdentity,
            InferenceRule::EmptyCollectionConstruction,
            InferenceRule::StableFunction,
            InferenceRule::ReturnedRecord,
        ])
    );
    assert!(record.inference.value_input_refs.is_empty());
    assert_eq!(record.defining_module, "static/peer");
    assert_eq!(record.export, "factory");
    assert_eq!(
        record.source_location.as_ref().unwrap().source_path,
        "static/peer.js"
    );
    assert!(record.members[2].captures.contains("readLater"));
    let queue = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "queue")
        })
        .unwrap();
    let execution = queue.synchronous_execution.as_ref().unwrap();
    assert!(execution.inference_uses.contains(
        &analysis_vocabulary::report_schema::InferenceUseReport::from(&record.inference)
    ));
    assert!(execution.callback_factory_uses.is_empty());
    let uses = &execution.returned_record_uses;
    assert!(
        uses.iter().any(|proof| proof.kind
            == analysis_vocabulary::report_schema::ReturnedRecordUseKind::CallTiming)
    );
    assert!(uses.iter().any(|proof| proof.kind
        == analysis_vocabulary::report_schema::ReturnedRecordUseKind::ReturnCapability));
    assert!(uses.iter().all(|proof| proof.summary_ref == certificate.id
        && proof.source_location.as_ref().unwrap().start_line == 3));
}

#[test]
fn source_record_capabilities_activate_on_later_use_and_publication() {
    let peer = "import { later } from './app.js';\nexport function factory() { let state = new Map; return { read() { return later; }, get value() { return later; }, has(key) { return state.has(key); } }; }\n";
    let host = "export function consume(record) { return record.read(); }\nexport function consumeSaved() { return globalThis.saved.read(); }\n";
    for (expression, publication) in [
        ("queue.value", ""),
        ("queue.read()", ""),
        ("consume(queue)", ""),
        ("consumeSaved()", "globalThis.saved = queue;\n"),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nimport {{ consume, consumeSaved }} from './host.js';\nexport let later = 0;\nconst queue = factory();\n{publication}const observed = {expression};\nlater = 42;\nconsole.log(observed, later);\n"
        );
        let peers = [("static/peer", peer), ("static/host", host)];
        let mut statements = vec![];
        if !publication.is_empty() {
            statements.push("globalThis.saved = queue;");
        }
        let mut together_statements = statements.clone();
        together_statements.push("later = 42;");
        let together = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "together",
                    &binding_members(["queue", "observed", "later"]),
                    &together_statements,
                )],
            )
            .with_extra_chunks(&peers),
        );
        assert_preserved_entry_output(&together, "0 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "queue",
                        &binding_members(["queue", "observed"]),
                        &statements,
                    ),
                    logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
                ],
            )
            .with_extra_chunks(&peers),
            &["queue", "later"],
            &[],
        );
    }
}

#[test]
fn source_record_factory_rejects_eager_or_unstable_capabilities() {
    let source = "import { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nexport { queue, Later };\n";
    for provider in [
        "export function factory() { const state = new Map(); return { [readLater().name]() { return state.size; } }; }",
        "export function factory() { const state = new Map(); return { value: ({ get value() { return readLater(); } }).value }; }",
        "export function factory() { readLater(); const state = new Map(); return { read() { return state.size; } }; }",
        "const Map = function () { readLater(); }; export function factory() { const state = new Map(); return { read() { return state.size; } }; }",
        "export function factory() { const state = new Map(); return { read() { return state.size; } }; } factory = function () { return readLater(); };",
        "Object.defineProperty(globalThis, 'Map', { value: function () { readLater(); } }); export function factory() { const state = new Map(); return { read() { return state.size; } }; }",
        "globalThis.Map = function () { readLater(); }; export function factory() { const state = new Map(); return { read() { return state.size; } }; }",
        "export function factory() { const state = new Map(); return { ...{ read() { return state.size; } } }; }",
    ] {
        let provider = format!("import {{ readLater }} from './app.js';\n{provider}\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                source,
                vec![
                    logical_module("queue", &binding_members(["queue", "readLater"])),
                    logical_module("later", &[Member::new("Later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", &provider)]),
            &["queue", "later"],
            &[],
        );
    }
}

#[test]
fn source_record_factory_with_unresolved_builtin_keeps_conservative_policy() {
    let source = "import 'node:fs';\nimport { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, readLater().name);\nexport { queue, Later };\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![
        logical_module("queue", &binding_members(["queue", "readLater"])),
        logical_module("later", &[Member::new("Later")]),
    ]).with_extra_chunks(&[("static/peer", "export function factory() { let state = new Map(); return { get size() { return state.size; } }; }\n")]));
    assert_preserved_entry_output(&fixture, "0 Later\n");
    let policy: serde_json::Value =
        read_json(&fixture.report_root.join("static/app/export_reentry.json"));
    assert_eq!(policy["policy"], "conservative");
}

#[test]
fn source_record_factory_does_not_cross_reexports() {
    let source = "import { factory } from './bridge.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nexport { queue, Later };\n";
    expect_atomic_conflict_rejection(FixtureOpts::new(source, vec![
        logical_module("queue", &binding_members(["queue", "readLater"])),
        logical_module("later", &[Member::new("Later")]),
    ]).with_extra_chunks(&[
        ("static/bridge", "export { factory } from './peer.js';\n"),
        ("static/peer", "import { readLater } from './app.js';\nexport function factory() { let state = new Map(); return { read() { return state.size + readLater().name.length; } }; }\n"),
    ]), &["queue", "later"], &[]);
}

#[test]
fn source_record_deferred_live_cell_access_preserves_the_tdz() {
    let peer = "import { later } from './app.js';\nexport function factory() { let state = new Map; return { read() { return later; }, get value() { return later; }, has(key) { return state.has(key); } }; }\n";
    for expression in ["queue.value", "queue.read()"] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nconst queue = factory();\nconst observed = (() => {{ try {{ return {expression}; }} catch (error) {{ return error.name; }} }})();\nexport const later = 42;\nconsole.log(observed, later);\n"
        );
        let peers = [("static/peer", peer)];
        let together = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "together",
                    &binding_members(["queue", "observed", "later"]),
                )],
            )
            .with_extra_chunks(&peers),
        );
        assert_preserved_entry_output(&together, "ReferenceError 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("queue", &binding_members(["queue", "observed"])),
                    logical_module("later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers),
            &["queue", "later"],
            &[],
        );
    }
}

#[test]
fn unused_or_ineligible_source_record_factory_has_no_graph_proof_use() {
    let provider = "export function factory() { let state = new Map(); return { get size() { return state.size; } }; }\n";
    for (source, priority) in [
        (
            "import { factory } from './peer.js';\nconsole.log(42);\n",
            &[][..],
        ),
        (
            "import { factory } from './peer.js';\nfactory(123);\nconsole.log(42);\n",
            &[][..],
        ),
        (
            "import { factory } from './peer.js';\nfunction use(factory) { return factory(); }\nconsole.log(use(() => 42));\n",
            &["residual"][..],
        ),
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![])
                .with_import_order(priority)
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let graph = fixture.dependency_graph();
        assert!(graph.imported_callable_summaries.is_empty());
        assert!(graph.nodes.iter().all(|node| {
            node.synchronous_execution
                .as_ref()
                .is_none_or(|execution| execution.returned_record_uses.is_empty())
        }));
    }
}

fn source_record_with_global_namespace(namespace: &str) -> Fixture {
    run_fixture(source_record_namespace::opts(&[
        source_record_namespace::PEER,
        ("static/namespace", namespace),
    ]))
}

#[test]
fn source_record_native_integrity_preserves_symbol_namespace_writes() {
    let fixture = source_record_with_global_namespace(
        "const namespace = Symbol.for('example.registry');\nconst alias = namespace;\nexport function publish(value) { globalThis[alias] = value; }\nexport function clear() { delete globalThis[namespace]; }\n",
    );
    assert_preserved_entry_output(&fixture, "0 Later\n");
}

#[test]
fn source_record_native_integrity_preserves_closed_entries_namespace_writes() {
    let fixture = source_record_with_global_namespace(
        "export function publish() { const root = globalThis; for (const [key, value] of Object.entries({ first: 1, second: 2 })) { Object.defineProperty(root, key, { value, configurable: true }); } }\n",
    );
    assert_preserved_entry_output(&fixture, "0 Later\n");
}

#[test]
fn source_record_native_integrity_rejects_unknown_or_rebound_keys() {
    for namespace in [
        "export function publish(key, value) { globalThis[key] = value; }",
        "const key = 'Map'; export function publish(value) { globalThis[key] = value; }",
        "let key = Symbol.for('example.registry'); export function replace() { key = 'Map'; } export function publish(value) { globalThis[key] = value; }",
        "let key = Symbol.for('example.registry'); export function replace() { [key] = ['Map']; } export function publish(value) { globalThis[key] = value; }",
        "let key = Symbol.for('example.registry'); export function publish(value) { for (key of ['Map']) globalThis[key] = value; }",
        "export function publish(key, value) { globalThis[key] = value; var key = Symbol.for('example.registry'); }",
        "export function publish(Symbol, value) { const key = Symbol.for('example.registry'); globalThis[key] = value; }",
        "export function publish(Object) { for (const [key, value] of Object.entries({ safe: 1 })) globalThis[key] = value; }",
        "export function publish() { for (let [key, value] of Object.entries({ safe: 1 })) { key = 'Map'; globalThis[key] = value; } }",
        "export function publish() { for (const [key, value] of Object.entries({ Map: 1 })) globalThis[key] = value; }",
        "export function publish() { for (const [key, value] of Object.entries({ ['safe']: 1 })) globalThis[key] = value; }",
        "export function publish() { for (const [key, value] of Object.entries({ get safe() { return 1; } })) globalThis[key] = value; }",
        "export function publish() { for (const [key = 'Map', value] of Object.entries({ safe: 1 })) globalThis[key] = value; }",
        "export function mutate() { Map++; }",
        "const key = Symbol.for('example.registry'); export function replace() { globalThis.box = Symbol; globalThis.box.for = () => 'Map'; globalThis[Symbol.for('other.registry')] = function () {}; }",
        "const key = Symbol.for('example.registry'); export function replace() { globalThis.box = Symbol; const alias = globalThis.box; alias.for = () => 'Map'; globalThis[Symbol.for('other.registry')] = function () {}; }",
        "const key = Symbol.for('example.registry'); export function replace() { globalThis[key] = Symbol; globalThis[key].for = () => 'Map'; globalThis[Symbol.for('other.registry')] = function () {}; }",
        "const key = Symbol.for('example.registry'); export function replace() { globalThis[key] = Symbol; const alias = globalThis[key]; alias.for = () => 'Map'; globalThis[Symbol.for('other.registry')] = function () {}; }",
    ] {
        source_record_namespace::expect_split_conflict(&[
            source_record_namespace::PEER,
            ("static/namespace", namespace),
        ]);
    }
}

#[test]
fn source_record_native_integrity_checks_other_chunk_operation_replacement() {
    for (namespace, replacement) in [
        (
            "const key = Symbol.for('example.registry'); export function publish(value) { globalThis[key] = value; }",
            "export function replace() { Symbol.for = () => 'Map'; }",
        ),
        (
            "export function publish() { for (const [key, value] of Object.entries({ safe: 1 })) globalThis[key] = value; }",
            "export function replace() { Object.entries = () => [['Map', 1]]; }",
        ),
        (
            "export function publish() { for (const [key, value] of Object.entries({ safe: 1 })) globalThis[key] = value; }",
            "export function replace() { Array.prototype[Symbol.iterator] = function* () { yield 'Map'; }; }",
        ),
    ] {
        let namespace = format!("import './replacement.js';\n{namespace}");
        source_record_namespace::expect_split_conflict(&[
            source_record_namespace::PEER,
            ("static/namespace", &namespace),
            ("static/replacement", replacement),
        ]);
    }
}

#[test]
fn source_record_native_integrity_handles_long_aliases_and_cycles() {
    let mut namespace = String::from("const key0 = Symbol.for('example.registry');\n");
    for index in 1..=256 {
        namespace.push_str(&format!("const key{index} = key{};\n", index - 1));
    }
    namespace.push_str("export function publish(value) { globalThis[key256] = value; }\n");
    let fixture = source_record_with_global_namespace(&namespace);
    assert_preserved_entry_output(&fixture, "0 Later\n");
    source_record_namespace::expect_split_conflict(&[
        source_record_namespace::PEER,
        (
            "static/namespace",
            "var first = second; var second = first; export function publish(value) { globalThis[first] = value; }",
        ),
    ]);
}

#[test]
fn source_record_unknown_global_slot_mutation_preserves_constructor_reentry() {
    let source = "import { factory } from './peer.js';\nimport './namespace.js';\nexport function readLater() { return Later; }\nglobalThis.saved = readLater;\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, readLater().name);\nexport { queue, Later };\n";
    let namespace = "const key = Symbol.for('example.registry');\nglobalThis[key] = Symbol;\nglobalThis[key].for = () => 'Map';\nglobalThis[Symbol.for('other.registry')] = function () { try { globalThis.saved(); return { size: 'ready' }; } catch (error) { return { size: error.name }; } };\n";
    let chunks = [
        source_record_namespace::PEER,
        ("static/namespace", namespace),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&chunks),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError Later\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "queue",
                    &binding_members(["queue", "readLater"]),
                    &["globalThis.saved = readLater;"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        )
        .with_import_order(&["queue", "later"])
        .with_extra_chunks(&chunks),
        &["queue", "later"],
        &[],
    );
}
