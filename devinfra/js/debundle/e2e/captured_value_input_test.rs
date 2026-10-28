//! Author-conditioned captured ordinary-object values stay bounded to one
//! stable provider binding and retain provenance through native checks.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const PROVIDER: &str = "import { readLater } from './app.js';\nvar captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { get touched() { globalThis.runtimeSettings.events.push('getter'); return true; } });\nexport function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }\n";
const OPAQUE: &str = "globalThis.runtimeSettings = { events: [], get state() { this.events.push('capture'); return { value: 'ready', child: new Map() }; } };\n";
const PROVIDER_CHUNKS: &[(&str, &str)] = &[("static/peer", PROVIDER)];
// The raw fixture entry is `static/app.js`; the emitted entry is
// `static/app/entry.js`. Both resolve their relative `./opaque.js` import.
const OPAQUE_FILES: &[(&str, &str)] = &[
    ("static/opaque.js", OPAQUE),
    ("static/app/opaque.js", OPAQUE),
];

fn source_hash(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn input_for(source: &str, declaration: &str) -> ValueInputContract {
    ValueInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [captured]\n",
            declaration.trim()
        ))
        .expect("valid declaration source match"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: source_hash(source),
        expected_export: None,
        note: Some("Neutral fixture audits this captured object.".to_string()),
    }
}

fn captured_input(source: &str) -> ValueInputContract {
    let declaration = source
        .lines()
        .find(|line| line.contains("captured = globalThis.runtimeSettings.state;"))
        .expect("fixture includes the selected capture declaration");
    input_for(source, declaration)
}

fn fixture_opts(inputs: Vec<ValueInputContract>) -> FixtureOpts<'static> {
    let source = "import './opaque.js';\nimport { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconst localMap = new Map();\nconst copied = [...[1, 2]];\nconsole.log(queue.size, queue.label(), localMap.size, copied.length, globalThis.runtimeSettings.events.join(','));\nexport { queue, Later };\n";
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module("queue", &[Member::new("queue"), Member::new("readLater")]),
            logical_module("later", &[Member::new("Later")]),
        ],
    )
    .with_extra_chunks(PROVIDER_CHUNKS)
    .with_extra_files(OPAQUE_FILES);
    if inputs.is_empty() {
        opts
    } else {
        opts.with_value_inputs("static/peer", inputs)
    }
}

fn input_refs(value: &Value) -> bool {
    value.as_array().is_some_and(|refs| {
        refs.iter().any(|reference| {
            reference["defining_chunk"] == "static/peer" && reference["input_index"] == 0
        })
    })
}

#[test]
fn unmaterialized_provider_value_input_restores_only_the_captured_receiver() {
    expect_atomic_conflict_rejection(fixture_opts(Vec::new()), &["queue", "later"], &[]);

    let fixture = run_fixture(fixture_opts(vec![captured_input(PROVIDER)]));
    assert_preserved_entry_output(&fixture, "0 Later 0 2 capture,getter\n");

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let witness = graph["native_integrity_witnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|witness| {
            witness["chunk_id"] == "static/peer"
                && witness["operation"] == "mutation_call"
                && input_refs(&witness["value_input_refs"])
        })
        .expect("direct ordinary receiver refinement remains in census evidence");
    assert_eq!(
        witness["receiver_roots"],
        serde_json::json!(["unknown_global_value"])
    );
    assert!(witness["affected_roots"].as_array().unwrap().is_empty());
    assert_eq!(
        witness["value_input_evidence"][0]["selected_binding"],
        "captured"
    );
    assert_eq!(
        witness["value_input_evidence"][0]["source_path"],
        "static/peer.js"
    );
    let parser_input_file = witness["value_input_evidence"][0]["parser_input_file"]
        .as_str()
        .expect("parser input provenance path");
    assert!(std::path::Path::new(parser_input_file).is_absolute());
    assert!(parser_input_file.ends_with("/static/peer.js"));
    assert!(
        witness["value_input_evidence"][0]["spec_location"]
            .as_str()
            .is_some_and(|path| path.ends_with(
                "/transform_spec.yaml:chunk_analysis_options.static/peer.value_inputs[0]"
            ))
    );

    let array_guard_use = graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["synchronous_execution"]["native_integrity_uses"]
            .as_array()
            .is_some_and(|uses| {
                uses.iter().any(|usage| {
                    usage["guard"] == "array_intrinsics" && input_refs(&usage["value_input_refs"])
                })
            })
    });
    assert!(
        array_guard_use,
        "native guard must report its author-conditioned input: {graph}"
    );

    let native_call_use = graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["synchronous_execution"]["native_integrity_uses"]
            .as_array()
            .is_some_and(|uses| {
                uses.iter().any(|usage| {
                    usage["guard"] == "native_call_identity"
                        && input_refs(&usage["value_input_refs"])
                })
            })
    });
    assert!(
        native_call_use,
        "successful native-call identity must report its author-conditioned input: {graph}"
    );

    let record_use = graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["synchronous_execution"]["returned_record_uses"]
            .as_array()
            .is_some_and(|uses| {
                uses.iter()
                    .any(|usage| input_refs(&usage["native_integrity_value_inputs"]))
            })
    });
    assert!(
        record_use,
        "imported returned-record use must retain input provenance: {graph}"
    );
}

#[test]
fn captured_primitive_predicate_return_preserves_cli_runtime() {
    let provider = "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { ready: true });\nexport function isReady() { if (captured.ready) return false; return true; }\n";
    let chunks = [("static/peer", provider)];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import './opaque.js';\nimport { isReady } from './peer.js';\nconsole.log(isReady(), globalThis.runtimeSettings.events.join(','));\n",
            vec![],
        )
        .with_extra_chunks(&chunks)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/peer", vec![captured_input(provider)]),
    );
    assert_preserved_entry_output(&fixture, "false capture\n");

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["chunk_id"] == "static/peer"
                    && witness["operation"] == "mutation_call"
                    && input_refs(&witness["value_input_refs"])
            }),
        "captured predicate must preserve consumed input provenance: {graph}"
    );
}

#[test]
fn fresh_graph_does_not_hide_stale_source_pin_or_wrong_export() {
    let input = captured_input(PROVIDER);
    let mut stale = input.clone();
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        fixture_opts(vec![stale]),
        &["stale audited_source_sha256", "static/peer"],
    );

    let mut wrong_export = input;
    wrong_export.expected_export = Some("factory".to_string());
    expect_rejection_containing_all(
        fixture_opts(vec![wrong_export]),
        &["expected_export", "captured"],
    );

    let mut wrong_claim = captured_input(PROVIDER);
    wrong_claim.source_match = serde_yaml::from_str(
        "match: 'var captured = globalThis.runtimeSettings.state;'\nbindings: [different]\n",
    )
    .expect("valid but wrong placeholder claim");
    expect_rejection_containing_all(
        fixture_opts(vec![wrong_claim]),
        &["neither declared nor used"],
    );

    let missing_provider = FixtureOpts::new("console.log('ready');\n", vec![])
        .with_extra_chunks(PROVIDER_CHUNKS)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/missing", vec![captured_input(PROVIDER)]);
    expect_rejection_containing_all(missing_provider, &["names no loaded provider chunk"]);

    let out_of_closure = FixtureOpts::new("console.log('ready');\n", vec![])
        .with_extra_chunks(PROVIDER_CHUNKS)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/peer", vec![captured_input(PROVIDER)]);
    expect_rejection_containing_all(
        out_of_closure,
        &["provider is outside the existing analyzed import closure"],
    );

    let mut multiple_bindings = captured_input(PROVIDER);
    multiple_bindings.source_match = serde_yaml::from_str(
        "match: 'var captured = globalThis.runtimeSettings.state;'\nbindings: [captured, other]\n",
    )
    .expect("valid source match with an unsupported multi-binding claim");
    expect_rejection_containing_all(
        fixture_opts(vec![multiple_bindings]),
        &["must select exactly one declared binding"],
    );
}

#[test]
fn recognized_native_and_any_rebinding_reject_the_value_input() {
    let native = "const captured = globalThis.Map;\nObject.assign(captured, {});\n";
    for (source, selector, expected) in [
        (
            native,
            "const captured = globalThis.Map;",
            "recognized native/global identity",
        ),
        (
            "let captured = globalThis.runtimeSettings.state;\ncaptured ||= {};\nObject.assign(captured, {});\n",
            "let captured = globalThis.runtimeSettings.state;",
            "possible reassignment",
        ),
        (
            "let captured = globalThis.runtimeSettings.state;\nfunction reset() { captured++; }\nObject.assign(captured, {});\n",
            "let captured = globalThis.runtimeSettings.state;",
            "possible reassignment",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nvar captured = globalThis.runtimeSettings.state;\nObject.assign(captured, {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "repeated or pattern declaration sites",
        ),
        (
            "let captured = globalThis.runtimeSettings.state;\n({ captured } = { captured: {} });\nObject.assign(captured, {});\n",
            "let captured = globalThis.runtimeSettings.state;",
            "possible reassignment",
        ),
        (
            "let captured = globalThis.runtimeSettings.state;\nfor (captured of []) {}\nObject.assign(captured, {});\n",
            "let captured = globalThis.runtimeSettings.state;",
            "possible reassignment",
        ),
        (
            "let captured = globalThis.runtimeSettings.state;\nfunction reset() { eval('captured = {}'); }\nObject.assign(captured, {});\n",
            "let captured = globalThis.runtimeSettings.state;",
            "dynamic-scope contamination",
        ),
        (
            "const captured = new Proxy({}, {});\nObject.assign(captured, {});\n",
            "const captured = new Proxy({}, {});",
            "unsupported initializer category",
        ),
        (
            "const captured = 1;\nObject.assign(captured, {});\n",
            "const captured = 1;",
            "unsupported initializer category",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, {});\n",
            "Object.assign(captured, {});",
            "must be declared by its pattern",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nfunction getChild() { return captured.child; }\nObject.assign(getChild(), {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "only synchronous direct same-binding or statically primitive returns are supported",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nfunction getMaybeChild() { return captured.ready ? false : captured.child; }\nObject.assign(captured, {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "only synchronous direct same-binding or statically primitive returns are supported",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nfunction getUnknown() { return captured?.run?.(); }\nObject.assign(captured, {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "only synchronous direct same-binding or statically primitive returns are supported",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nasync function getAsync() { return captured.ready ? false : true; }\nObject.assign(captured, {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "only synchronous direct same-binding or statically primitive returns are supported",
        ),
        (
            "var captured = globalThis.runtimeSettings.state;\nfunction* getGenerator() { yield captured.child; return false; }\nObject.assign(captured, {});\n",
            "var captured = globalThis.runtimeSettings.state;",
            "only synchronous direct same-binding or statically primitive returns are supported",
        ),
    ] {
        let chunks = [("static/peer", source)];
        let mut opts = FixtureOpts::new("import './peer.js';\nconsole.log(1);\n", vec![])
            .with_extra_chunks(&chunks);
        let input = input_for(source, selector);
        opts = opts.with_value_inputs("static/peer", vec![input]);
        expect_rejection_containing_all(opts, &[expected]);
    }
}

#[test]
fn descendants_and_early_alias_observations_keep_unknown_global_barriers() {
    let descendant = "import { readLater } from './app.js';\nvar captured = globalThis.runtimeSettings.state;\nObject.assign(captured.child, {});\nfunction getCaptured() { return captured; }\nObject.assign(getCaptured(), {});\nexport function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }\n";
    let chunks = [("static/peer", descendant)];
    let input = captured_input(descendant);
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import './opaque.js';\nimport { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, queue.label(), Later.name);\nexport { queue, Later };\n",
            vec![
                logical_module("queue", &[Member::new("queue"), Member::new("readLater")]),
                logical_module("later", &[Member::new("Later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/peer", vec![input]),
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["queue", "later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    let retained_barriers = graph["native_integrity_witnesses"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|witness| {
            witness["chunk_id"] == "static/peer"
                && witness["operation"] == "mutation_call"
                && witness["receiver_roots"] == serde_json::json!(["unknown_global_value"])
                && witness["affected_roots"] == serde_json::json!(["global_object"])
                && input_refs(&witness["value_input_refs"])
        })
        .count();
    assert!(
        retained_barriers >= 2,
        "member and direct-return call descendants must retain unknown-global barriers: {graph}"
    );
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["chunk_id"] == "static/peer"
                    && witness["operation"] == "mutation_call"
                    && witness["receiver_roots"] == serde_json::json!(["unknown_global_value"])
                    && witness["affected_roots"] == serde_json::json!(["global_object"])
                    && input_refs(&witness["value_input_refs"])
            }),
        "a descendant receiver must retain its unknown-global guard: {graph}"
    );

    let before_init = "import { readLater } from './app.js';\nconst alias = captured;\nconst captured = globalThis.runtimeSettings.state;\nObject.assign(alias, {});\nexport function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }\n";
    let chunks = [("static/peer", before_init)];
    let input = captured_input(before_init);
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import './opaque.js';\nimport { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, queue.label(), Later.name);\nexport { queue, Later };\n",
            vec![
                logical_module("queue", &[Member::new("queue")]),
                logical_module("later", &[Member::new("Later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/peer", vec![input]),
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["queue", "later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["chunk_id"] == "static/peer"
                    && witness["operation"] == "mutation_call"
                    && witness["receiver_roots"] == serde_json::json!(["unknown_global_value"])
                    && witness["affected_roots"] == serde_json::json!(["global_object"])
                    && witness["value_input_refs"]
                        .as_array()
                        .is_none_or(Vec::is_empty)
            }),
        "a pre-initialization alias must keep its prior unknown receiver: {graph}"
    );
}

#[test]
fn native_constructor_instance_does_not_inherit_constructor_root() {
    let instance = [(
        "static/peer",
        "const instance = new Map();\nObject.assign(instance, {});\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new("import './peer.js';\nconsole.log('ready');\n", vec![])
            .with_extra_chunks(&instance),
    );
    assert_preserved_entry_output(&fixture, "ready\n");

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        !graph["native_integrity_witnesses"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .any(|witness| {
                witness["chunk_id"] == "static/peer"
                    && witness["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| roots.iter().any(|root| root == "map"))
            })
    );
}

#[test]
fn actual_native_mutation_in_another_chunk_still_blocks_map_capability() {
    let mutation = "Map.prototype.set = function () {};\n";
    let chunks = [
        ("static/peer", PROVIDER),
        ("static/native-mutation", mutation),
    ];
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import './opaque.js';\nimport './native-mutation.js';\nimport { factory } from './peer.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, queue.label().name);\nexport { queue, Later };\n",
            vec![
                logical_module("queue", &[Member::new("queue"), Member::new("readLater")]),
                logical_module("later", &[Member::new("Later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_extra_files(OPAQUE_FILES)
        .with_value_inputs("static/peer", vec![captured_input(PROVIDER)]),
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        let claims = conflict["claims"].as_array().expect("conflict claims");
        ["queue", "later"].iter().all(|module| {
            claims
                .iter()
                .any(|claim| claim["module"].as_str() == Some(*module))
        })
    }));
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["chunk_id"] == "static/native-mutation"
                    && witness["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| roots.iter().any(|root| root == "map"))
            }),
        "an actual Map prototype write must retain its global census barrier: {graph}"
    );
}

#[test]
fn prototype_absence_keeps_captured_census_provenance() {
    let provider = "var captured = globalThis.runtimeSettings.state;\ncaptured.marker = 1;\nexport const marker = 1;\n";
    let source = "import './opaque.js';\nimport { marker } from './peer.js';\nclass Box { constructor(config) { this.value = config.value ?? 3; } }\nconst result = new Box({});\nconsole.log(result.value, marker);\n";
    let chunks = [("static/peer", provider)];
    let inputs = vec![captured_input(provider)];
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&chunks)
            .with_extra_files(OPAQUE_FILES)
            .with_value_inputs("static/peer", inputs),
    );
    assert_preserved_entry_output(&fixture, "3 1\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let result = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|names| names.iter().any(|name| name["binding"] == "result"))
        })
        .expect("result node");
    assert!(
        result["synchronous_execution"]["native_prototype_property_checks"]
            .as_array()
            .is_some_and(|checks| checks.iter().any(|check| check["intact"] == true)),
        "{result}"
    );
    assert!(
        result["synchronous_execution"]["native_integrity_uses"]
            .as_array()
            .is_some_and(|uses| uses
                .iter()
                .any(|usage| usage["guard"] == "ordinary_object_prototype"
                    && input_refs(&usage["value_input_refs"]))),
        "{result}"
    );
}

#[test]
fn primitive_set_split_keeps_the_same_input_in_purity_and_callback_timing() {
    let provider = "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { ready: true });\nexport const ready = true;\n";
    let source = "import './opaque.js';\nimport { ready } from './peer.js';\nglobalThis.saved = () => later;\nconst unique = new Set(['a', 'b']);\nconst later = 42;\nconsole.log(ready, unique.size, globalThis.saved());\n";
    let chunks = [("static/peer", provider)];
    let modules = || {
        vec![
            logical_module_with_anon(
                "collection",
                &[Member::new("unique")],
                &["globalThis.saved = () => later;"],
            ),
            logical_module("value", &[Member::new("later")]),
        ]
    };
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_extra_files(OPAQUE_FILES),
        &["collection", "value"],
        &[],
    );
    let fixture = run_fixture(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_extra_files(OPAQUE_FILES)
            .with_value_inputs("static/peer", vec![captured_input(provider)]),
    );
    assert_preserved_entry_output(&fixture, "true 2 42\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let unique = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|names| names.iter().any(|name| name["binding"] == "unique"))
        })
        .expect("Set construction node");
    assert_eq!(unique["purity"]["kind"], "pure", "{unique}");
    assert!(
        input_refs(&unique["purity"]["dependencies"]["value_inputs"]),
        "{unique}"
    );
    assert_eq!(
        unique["synchronous_execution"]["complete"], true,
        "{unique}"
    );
    assert!(
        unique["synchronous_execution"]["native_integrity_uses"]
            .as_array()
            .is_some_and(|uses| uses
                .iter()
                .any(|usage| usage["guard"] == "set_iterable"
                    && input_refs(&usage["value_input_refs"]))),
        "{unique}"
    );
}
