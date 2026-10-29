//! A narrowly selected native call can retain the existing call model while
//! unknown receiver writes elsewhere in the provider remain visible.

use analysis::DepKind;
use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst marker = Symbol('marker');\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst made = factory();\nglobalThis.unrelatedKey = 'safe';\nconsole.log(marker.description, made.name, globalThis.unrelatedKey);\nexport { marker, factory, LaterError, reflect };\n";
const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
const REFLECT_CHUNKS: &[(&str, &str)] = &[("static/reflect", REFLECT_SOURCE)];
const BASELINE_CONFLICT_MODULES: &[&str] = &["marker", "later"];

fn source_hash(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn input_for(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [marker]\n",
            declaration.trim()
        ))
        .expect("valid declaration source match"),
        operation: NativeOperationKind::Symbol,
        audited_source_sha256: source_hash(source),
        note: Some("Neutral fixture audits this one initialization call.".to_string()),
    }
}

fn marker_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const marker ="))
        .expect("fixture has a direct marker initializer");
    input_for(source, declaration)
}

fn primitive_set_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const made = new Set("))
        .expect("fixture has a direct Set initializer");
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [made]\n",
            declaration.trim()
        ))
        .expect("valid Set declaration source match"),
        operation: NativeOperationKind::SetFromPrimitiveArray,
        audited_source_sha256: source_hash(source),
        note: Some("Neutral fixture audits this one Set construction site.".to_string()),
    }
}

fn selected_set_input(
    source: &str,
    declaration: &str,
    binding: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [{binding}]\n",
            declaration.trim()
        ))
        .expect("valid Set declaration source match"),
        operation,
        audited_source_sha256: source_hash(source),
        note: Some("Neutral fixture audits this exact Set operation.".to_string()),
    }
}

#[test]
fn certified_same_output_set_spread_removes_only_its_constructor_barrier() {
    const DECLARATION: &str =
        "var seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);";
    const SOURCE: &str = "globalThis.saved = () => factory();\nvar seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(merged.size, LaterError.name);\nexport { factory, LaterError };\n";
    let inputs = || {
        vec![
            selected_set_input(
                SOURCE,
                DECLARATION,
                "seed",
                NativeOperationKind::SetFromPrimitiveArray,
            ),
            selected_set_input(
                SOURCE,
                DECLARATION,
                "sibling",
                NativeOperationKind::SetFromPrimitiveArray,
            ),
            selected_set_input(
                SOURCE,
                DECLARATION,
                "merged",
                NativeOperationKind::SetFromSetSpread,
            ),
        ]
    };
    let opts = || {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "sets",
                    &[
                        Member::new("seed"),
                        Member::new("sibling"),
                        Member::new("merged"),
                    ],
                    &["globalThis.saved = () => factory();"],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
    };
    let control = run_rejection_fixture(
        opts().with_native_operation_inputs("static/app", inputs()[..2].to_vec()),
    );
    assert!(
        !control.atomic_group_conflicts().is_empty(),
        "the unproved spread must still block the split"
    );
    let fixture = run_fixture(opts().with_native_operation_inputs("static/app", inputs()));
    assert_preserved_entry_output(&fixture, "2 LaterError\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        3,
        "all exact Set operations must retain evidence: {graph}"
    );
    assert!(
        uses.iter()
            .any(|use_site| use_site["evidence"]["operation"] == "set_from_set_spread")
    );
    assert!(
        !has_unknown_call_target(node_for_binding(&graph, "merged")),
        "certified spread must remove its exact constructor frontier: {graph}"
    );
}

#[test]
fn set_spread_requires_every_intervening_constructor_and_one_explicit_output() {
    const DECLARATION: &str =
        "var seed = new Set([1]), sibling = new Set([2]), merged = new Set([...seed, 3]);";
    let source = format!("{DECLARATION}\nconsole.log(merged.size);\nexport {{}};\n");
    let seed = selected_set_input(
        &source,
        DECLARATION,
        "seed",
        NativeOperationKind::SetFromPrimitiveArray,
    );
    let sibling = selected_set_input(
        &source,
        DECLARATION,
        "sibling",
        NativeOperationKind::SetFromPrimitiveArray,
    );
    let spread = selected_set_input(
        &source,
        DECLARATION,
        "merged",
        NativeOperationKind::SetFromSetSpread,
    );
    let same_output = || {
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "sets",
                &[
                    Member::new("seed"),
                    Member::new("sibling"),
                    Member::new("merged"),
                ],
            )],
        )
    };
    expect_rejection_containing_all(
        same_output()
            .with_native_operation_inputs("static/app", vec![seed.clone(), spread.clone()]),
        &["uncertified intervening source unit"],
    );
    expect_rejection_containing_all(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("seed", &[Member::new("seed"), Member::new("sibling")]),
                logical_module("merged", &[Member::new("merged")]),
            ],
        )
        .with_native_operation_inputs("static/app", vec![seed, sibling, spread]),
        &["same original declaration and output"],
    );
}

#[test]
fn set_spread_interval_accepts_zero_or_multiple_inert_siblings() {
    for siblings in [0, 2] {
        let middle = (0..siblings)
            .map(|index| format!("sibling{index} = new Set([{index}]), "))
            .collect::<String>();
        let declaration =
            format!("var seed = new Set([1]), {middle}merged = new Set([...seed, 3]);");
        let source = format!("{declaration}\nconsole.log(merged.size);\nexport {{}};\n");
        let mut bindings = vec![Member::new("seed")];
        let mut inputs = vec![selected_set_input(
            &source,
            &declaration,
            "seed",
            NativeOperationKind::SetFromPrimitiveArray,
        )];
        for index in 0..siblings {
            let name = ["sibling0", "sibling1"][index];
            bindings.push(Member::new(name));
            inputs.push(selected_set_input(
                &source,
                &declaration,
                name,
                NativeOperationKind::SetFromPrimitiveArray,
            ));
        }
        bindings.push(Member::new("merged"));
        inputs.push(selected_set_input(
            &source,
            &declaration,
            "merged",
            NativeOperationKind::SetFromSetSpread,
        ));
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![logical_module("sets", &bindings)])
                .with_native_operation_inputs("static/app", inputs),
        );
        assert_preserved_entry_output(&fixture, "2\n");
    }
}

#[test]
fn set_spread_rejects_intervening_mutators_and_callbacks() {
    for intervening in [
        "seed.add(4)",
        "(seed[Symbol.iterator] = function* () { yield 9; })",
        "unknown()",
    ] {
        let declaration = format!(
            "var seed = new Set([1]), intervening = {intervening}, merged = new Set([...seed, 3]);"
        );
        let source = format!("{declaration}\nconsole.log(merged.size);\nexport {{}};\n");
        let inputs = vec![
            selected_set_input(
                &source,
                &declaration,
                "seed",
                NativeOperationKind::SetFromPrimitiveArray,
            ),
            selected_set_input(
                &source,
                &declaration,
                "merged",
                NativeOperationKind::SetFromSetSpread,
            ),
        ];
        expect_rejection_containing_all(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "sets",
                    &[
                        Member::new("seed"),
                        Member::new("intervening"),
                        Member::new("merged"),
                    ],
                )],
            )
            .with_native_operation_inputs("static/app", inputs),
            &["uncertified intervening source unit"],
        );
    }
}

#[test]
fn set_spread_rejects_native_protocol_replacement_in_another_chunk() {
    const DECLARATION: &str = "var seed = new Set([1]), merged = new Set([...seed, 3]);";
    const MUTATION: &str = "const proto = Set.prototype; Object.defineProperty(proto, 'add', { value: function replacement() {} }); export {};\n";
    let source = format!(
        "import './mutation.js';\n{DECLARATION}\nconsole.log(merged.size);\nexport {{}};\n"
    );
    let inputs = vec![
        selected_set_input(
            &source,
            DECLARATION,
            "seed",
            NativeOperationKind::SetFromPrimitiveArray,
        ),
        selected_set_input(
            &source,
            DECLARATION,
            "merged",
            NativeOperationKind::SetFromSetSpread,
        ),
    ];
    expect_rejection_containing_all(
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "sets",
                &[Member::new("seed"), Member::new("merged")],
            )],
        )
        .with_extra_chunks(&[("static/mutation", MUTATION)])
        .with_native_operation_inputs("static/app", inputs),
        &[
            "required Set construction protocol slot write",
            "static/mutation",
        ],
    );
}

#[test]
fn certified_spread_keeps_later_alias_mutation_order() {
    const DECLARATION: &str = "var seed = new Set([1]), merged = new Set([...seed, 3]);";
    let source = format!(
        "{DECLARATION}\nconst alias = merged;\nalias.add(4);\nconst after = merged.has(4);\nconsole.log(after);\nexport {{}};\n"
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "sets",
                &[
                    Member::new("seed"),
                    Member::new("merged"),
                    Member::new("alias"),
                ],
            )],
        )
        .with_native_operation_inputs(
            "static/app",
            vec![
                selected_set_input(
                    &source,
                    DECLARATION,
                    "seed",
                    NativeOperationKind::SetFromPrimitiveArray,
                ),
                selected_set_input(
                    &source,
                    DECLARATION,
                    "merged",
                    NativeOperationKind::SetFromSetSpread,
                ),
            ],
        ),
    );
    assert_preserved_entry_output(&fixture, "true\n");
    let graph = fixture.dependency_graph();
    let unit = |ordinal| {
        graph
            .nodes
            .iter()
            .find(|node| node.statement_ordinal.0 == ordinal)
            .expect("fixture source unit")
            .id
            .as_str()
    };
    for (later, earlier) in [(3, 1), (4, 3)] {
        assert!(
            graph.edges.iter().any(|edge| {
                edge.edge_kind == DepKind::Sequenced
                    && edge.source == unit(later)
                    && edge.target == unit(earlier)
            }),
            "later alias Set effects must retain S order: {later} -> {earlier}: {:#?}",
            graph.edges
        );
    }
}

#[test]
fn selected_primitive_set_construction_removes_its_opaque_frontier() {
    const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst made = new Set([1, 2]);\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(made.size, LaterError.name);\nexport { factory, LaterError, reflect };\n";
    let opts = || {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "made",
                    &[Member::new("made")],
                    &["globalThis.saved = () => factory();"],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)])
    };
    let control = run_rejection_fixture(opts());
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&control_graph);
    let conflicts = control.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["made", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "the unproved constructor must block this split: {conflicts:#?}"
    );
    assert!(
        has_unknown_call_target(node_for_binding(&control_graph, "made")),
        "unproved Set construction still has a call frontier: {control_graph}"
    );
    let fixture = run_fixture(
        opts().with_native_operation_inputs("static/app", vec![primitive_set_input(SOURCE)]),
    );
    assert_preserved_entry_output(&fixture, "2 LaterError\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        !has_unknown_call_target(node_for_binding(&graph, "made")),
        "the selected constructor should consume its native input: {graph}"
    );
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        1,
        "exactly the selected Set site uses the premise: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "set_from_primitive_array");
    unknown_receiver_witness(&graph);
}

#[test]
fn source_known_set_construction_protocol_writes_reject_the_input() {
    for mutation in [
        "globalThis.Set = function replacement() {};",
        "Set.prototype.add = function replacement() {};",
        "const proto = Set.prototype; delete proto.add;",
        "Object.defineProperty(Set.prototype, 'add', { get() { return function replacement() {}; } });",
    ] {
        let source = format!(
            "const made = new Set([1, 2]);\n{mutation}\nconsole.log(made.size);\nexport {{}};\n"
        );
        assert!(
            std::panic::catch_unwind(|| {
                expect_rejection_containing_all(
                    rejected_opts(&source, "made", primitive_set_input(&source)),
                    &["required native slot write"],
                );
            })
            .is_ok(),
            "source-known protocol mutation escaped validation: {mutation}"
        );
    }
    for source in [
        "const made = new Set([1, 2]);\nconst Set = function replacement() {};\nexport {};\n",
        "const made = new Set([...values]);\nexport {};\n",
        "const made = new Set([unknown]);\nexport {};\n",
    ] {
        expect_rejection_containing_all(
            rejected_opts(source, "made", primitive_set_input(source)),
            &["direct unshadowed new Set([primitive literals])"],
        );
    }
}

#[test]
fn dependent_array_iterator_key_remains_an_opaque_witness() {
    let source = "const made = new Set([1, 2]);\nconst before = Array.prototype[Symbol.iterator];\nArray.prototype[Symbol.iterator] = function replacement() {};\nArray.prototype[Symbol.iterator] = before;\nconsole.log(made.size);\nexport {};\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|witness| {
                witness["target_slots"].as_array().is_some_and(|slots| {
                    slots.iter().any(|slot| {
                        slot["receiver"] == serde_json::json!({"prototype": "array"})
                            && slot["key"] == "symbol_iterator"
                    })
                }) && witness["key_dependencies"]
                    .as_array()
                    .is_some_and(|deps| deps.iter().any(|dependency| dependency == "symbol"))
            })),
        "computed Symbol.iterator writes must retain their dependency: {graph}"
    );
}

#[test]
fn array_iterator_next_alias_retains_a_typed_opaque_witness() {
    let source = "const made = new Set([1, 2]);\nconst proto = Object.getPrototypeOf([][Symbol.iterator]());\nconst original = proto.next;\nproto.next = function replacement() { return { done: true }; };\nproto.next = original;\nconsole.log(made.size);\nexport {};\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|witness| {
                witness["target_slots"].as_array().is_some_and(|slots| {
                    slots.iter().any(|slot| {
                        slot["receiver"] == "array_iterator_prototype"
                            && slot["key"] == serde_json::json!({ "string": "next" })
                    })
                }) && witness["key_dependencies"]
                    .as_array()
                    .is_some_and(|deps| deps.iter().any(|dependency| dependency == "symbol"))
            })),
        "Array iterator prototype aliases must stay typed but dependency-opaque: {graph}"
    );
}

#[test]
fn set_iterator_next_alias_retains_a_typed_opaque_witness() {
    let source = "const proto = Object.getPrototypeOf(new Set()[Symbol.iterator]());\nconst original = proto.next;\nproto.next = function replacement() { return { done: true }; };\nproto.next = original;\nconsole.log('done');\nexport {};\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "done\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|witness| {
                witness["target_slots"].as_array().is_some_and(|slots| {
                    slots.iter().any(|slot| {
                        slot["receiver"] == "set_iterator_prototype"
                            && slot["key"] == serde_json::json!({ "string": "next" })
                    })
                }) && witness["key_dependencies"].as_array().is_some_and(|deps| {
                    deps.iter().any(|dependency| dependency == "set")
                        && deps.iter().any(|dependency| dependency == "symbol")
                })
            })),
        "Set iterator prototype aliases must stay typed and dependency-opaque: {graph}"
    );
}

#[test]
fn cross_chunk_set_protocol_mutation_rejects_the_input() {
    const MUTATION: &str =
        "const proto = Set.prototype; proto.add = function replacement() {};\nexport {};\n";
    let source = "import './mutation.js';\nconst made = new Set([1, 2]);\nconsole.log(made.size);\nexport {};\n";
    let opts = rejected_opts(source, "made", primitive_set_input(source))
        .with_extra_chunks(&[("static/mutation", MUTATION)]);
    expect_rejection_containing_all(
        opts,
        &[
            "required Set construction protocol slot write",
            "static/mutation",
        ],
    );
}

#[test]
fn shadowed_local_set_receiver_does_not_contradict_native_construction() {
    let source = "const made = new Set([1, 2]);\nfunction touch(Set) { Set.prototype.add = function replacement() {}; }\ntouch({ prototype: {} });\nconsole.log(made.size);\nexport {};\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(native_input_uses(&graph).len(), 1);
}

#[test]
fn construction_premise_preserves_later_aliased_mutator_order() {
    let source = "const made = new Set([1]);\nconst alias = made;\nalias.add(2);\nalias.delete(1);\nalias.clear();\nconst count = made.size;\nconsole.log(count);\nexport {};\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "0\n");
    let graph = fixture.dependency_graph();
    let unit = |ordinal| {
        graph
            .nodes
            .iter()
            .find(|node| node.statement_ordinal.0 == ordinal)
            .expect("fixture source unit")
            .id
            .as_str()
    };
    for (later, earlier) in [(3, 2), (4, 3), (5, 4)] {
        assert!(
            graph.edges.iter().any(|edge| {
                edge.edge_kind == DepKind::Sequenced
                    && edge.source == unit(later)
                    && edge.target == unit(earlier)
            }),
            "selected construction must retain later Set method and accessor order: {later} -> {earlier}: {:#?}",
            graph.edges
        );
    }
}

fn split_opts(input: Option<NativeOperationInputContract>) -> FixtureOpts<'static> {
    let opts = FixtureOpts::new(
        SOURCE,
        vec![
            logical_module_with_anon(
                "marker",
                &[Member::new("marker")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    );
    let opts = opts
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)]);
    match input {
        Some(input) => opts.with_native_operation_inputs("static/app", vec![input]),
        None => opts,
    }
}

fn rejected_opts<'a>(
    source: &'a str,
    selected_binding: &'static str,
    input: NativeOperationInputContract,
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module("selected", &[Member::new(selected_binding)])],
    )
    .with_native_operation_inputs("static/app", vec![input])
}

fn native_input_uses(graph: &Value) -> Vec<Value> {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect()
}

fn node_for_binding<'a>(graph: &'a Value, binding: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|candidate| candidate["binding"] == binding)
                })
        })
        .unwrap_or_else(|| panic!("no source node for {binding}: {graph}"))
}

fn has_unknown_call_target(node: &Value) -> bool {
    node["synchronous_execution"]["precision_losses"]
        .as_array()
        .is_some_and(|losses| {
            losses
                .iter()
                .any(|loss| loss["reason"]["code"] == "unknown_call_target")
        })
}

fn unknown_receiver_witness(graph: &Value) -> &Value {
    graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .find(|witness| {
            witness["operation"] == "mutation_call"
                && witness["receiver_roots"]
                    .as_array()
                    .is_some_and(|roots| roots.iter().any(|root| root == "unknown_global_value"))
                && witness["source_location"]["source_path"] == "static/reflect.js"
        })
        .unwrap_or_else(|| panic!("reflective helper's unknown receiver is absent: {graph}"))
}

#[test]
fn selected_symbol_call_removes_only_its_synthetic_reentry_barrier() {
    let control = run_rejection_fixture(split_opts(None));
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&control_graph);
    let conflicts = control.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            BASELINE_CONFLICT_MODULES.iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "unexpected no-input atomic-group conflicts: {conflicts:#?}"
    );
    assert!(
        has_unknown_call_target(node_for_binding(&control_graph, "marker")),
        "without the input, the selected initializer retains its unknown-call precision loss: {control_graph}"
    );

    let fixture = run_fixture(split_opts(Some(marker_input(SOURCE))));
    assert_preserved_entry_output(&fixture, "marker LaterError safe\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const entry = await import('./static/app/entry.js');\nconsole.log(entry.marker.description, entry.factory().name, entry.LaterError.name);\nconst receiver = [];\nconst target = Object.getPrototypeOf(receiver);\nconst key = '__native_operation_input_probe__';\nconst before = Object.getOwnPropertyDescriptor(target, key);\ntry { entry.reflect(receiver, key); console.log(target[key]); } finally { if (before) Object.defineProperty(target, key, before); else delete target[key]; }\n",
        "marker LaterError LaterError\nchanged\n",
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        1,
        "only the selected call consumes this input: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "symbol");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
    assert_eq!(uses[0]["source_location"]["source_path"], "static/app.js");
    assert_eq!(uses[0]["source_location"]["start_line"], 3);

    let unknown_receiver_witness = unknown_receiver_witness(&graph);
    assert!(
        unknown_receiver_witness["affected_roots"]
            .as_array()
            .is_some_and(|roots| roots.iter().any(|root| root == "global_object")),
        "the unknown receiver remains connected to the global object: {unknown_receiver_witness}"
    );
    assert!(
        unknown_receiver_witness["value_input_refs"]
            .as_array()
            .is_none_or(Vec::is_empty),
        "this premise must not refine the reflective helper's receiver: {graph}"
    );
    assert!(
        unknown_receiver_witness["target_slots"]
            .as_array()
            .is_some_and(|slots| slots
                .iter()
                .any(|slot| { slot["receiver"] == "unknown" && slot["key"] == "unknown" })),
        "the opaque reflective write must retain its unknown slot witness: {graph}"
    );
}

#[test]
fn source_selected_symbol_input_rejects_unsupported_or_contradicted_facts() {
    for (source, expected) in [
        (
            "const marker = Symbol('marker');\nconst Symbol = function(value) { return value; };\nexport { marker };\n",
            "shadowed Symbol",
        ),
        (
            "const marker = Symbol('marker');\nconst originalSymbol = globalThis.Symbol;\nglobalThis.Symbol = function replacement() {};\nglobalThis.Symbol = originalSymbol;\nexport { marker };\n",
            "global Symbol replacement",
        ),
        (
            "Object.defineProperty(globalThis, 'Symbol', { configurable: true, get() { return function replacement() {}; } });\nconst marker = Symbol('marker');\nexport { marker };\n",
            "global Symbol replacement",
        ),
        (
            "const marker = Symbol(globalThis.markerDescription);\nexport { marker };\n",
            "syntactically primitive argument",
        ),
        (
            "const marker = Symbol?.('marker');\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const marker = Symbol(...['marker']);\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const selected = globalThis['Symbol']('marker');\nexport { selected };\n",
            "direct uncomputed Symbol identifier",
        ),
    ] {
        let declaration = source
            .lines()
            .find(|line| line.contains("const marker =") || line.contains("const selected ="))
            .expect("negative fixture has selected declaration");
        let selected_name = if declaration.starts_with("const selected") {
            "selected"
        } else {
            "marker"
        };
        let mut input = input_for(source, declaration);
        if selected_name == "selected" {
            input.source_match = serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [selected]\n",
                declaration.trim()
            ))
            .unwrap();
        }
        expect_rejection_containing_all(rejected_opts(source, selected_name, input), &[expected]);
    }
}

#[test]
fn source_hash_is_checked_before_a_symbol_input_is_used() {
    let source = "const marker = Symbol('marker');\nexport { marker };\n";
    let mut input = marker_input(source);
    input.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        rejected_opts(source, "marker", input),
        &["audited_source_sha256"],
    );
}

#[test]
fn unselected_symbol_site_keeps_the_strict_fallback() {
    let source = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst marker = Symbol('marker');\nconst other = Symbol('other');\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst made = factory();\nconsole.log(marker.description, other.description, made.name);\nexport { marker, other, factory, LaterError, reflect };\n";
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "marker",
                &[Member::new("marker")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("other", &[Member::new("other")]),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    )
    .with_extra_chunks(REFLECT_CHUNKS)
    .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)])
    .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let rejected = run_rejection_fixture(opts);
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["other", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "unselected Symbol call must retain the synthetic reentry conflict: {conflicts:#?}"
    );
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    assert!(
        has_unknown_call_target(node_for_binding(&graph, "other")),
        "the unselected primitive Symbol call must retain its unknown-call precision loss: {graph}"
    );
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        1,
        "other Symbol call has no author evidence: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
}

#[test]
fn known_symbol_replacement_in_another_analyzed_chunk_contradicts_the_input() {
    const MUTATION: &str = "const originalSymbol = globalThis.Symbol;\nglobalThis.Symbol = function replacement() {};\nglobalThis.Symbol = originalSymbol;\nexport {};\n";
    let source = "import './mutation.js';\nconst marker = Symbol('marker');\nexport { marker };\n";
    let input = marker_input(source);
    let opts = FixtureOpts::new(
        source,
        vec![logical_module("marker", &[Member::new("marker")])],
    )
    .with_extra_chunks(&[("static/mutation", MUTATION)])
    .with_native_operation_inputs("static/app", vec![input]);
    expect_rejection_containing_all(opts, &["global Symbol replacement", "static/mutation"]);
}

#[test]
fn exact_global_symbol_slot_rejects_aliases_and_reflective_writes() {
    for mutation in [
        "const root = globalThis; root.Symbol = function replacement() {};",
        "const root = globalThis; delete root.Symbol;",
        "const root = globalThis; Object.defineProperty(root, 'Symbol', { get() { return function replacement() {}; } });",
        "const root = globalThis; Object.assign(root, { Symbol: function replacement() {} });",
        "const root = globalThis; Object.defineProperties(root, { Symbol: { value: function replacement() {} } });",
    ] {
        let source =
            format!("const marker = Symbol('marker');\n{mutation}\nexport {{ marker }};\n");
        expect_rejection_containing_all(
            rejected_opts(&source, "marker", marker_input(&source)),
            &["global Symbol replacement"],
        );
    }
}

#[test]
fn unrelated_global_write_preserves_exact_symbol_input() {
    let source = "const marker = Symbol('marker');\nconst root = globalThis;\nObject.assign(root, { unrelated: 1 });\nconsole.log(marker.description);\nexport { marker };\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![marker_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "marker\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(native_input_uses(&graph).len(), 1);
    assert!(
        graph["native_integrity_witnesses"]
            .as_array()
            .expect("native mutation witnesses")
            .iter()
            .any(
                |witness| witness["target_slots"]
                    .as_array()
                    .is_some_and(|slots| slots.iter().any(|slot| {
                        slot["receiver"] == "global_object" && slot["key"]["string"] == "unrelated"
                    }))
            )
    );
}

#[test]
fn native_slot_census_preserves_paths_keys_and_opaque_writes_across_chunks() {
    let source = "import './mutation.js';\nconsole.log('ok');\n";
    let mutation = "export function mutate() { const proto = Set.prototype; proto.add = function(value) { return value; }; Set.sibling = 1; Array.prototype[Symbol.iterator] = function* () {}; Object.defineProperty(proto, 'delete', { value: function() {} }); const unknown = globalThis.maybe; Object.defineProperty(unknown, 'add', { value: function() {} }); }\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/mutation", mutation)]),
    );
    assert_preserved_entry_output(&fixture, "ok\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let slots: Vec<_> = graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .filter(|witness| witness["source_location"]["source_path"] == "static/mutation.js")
        .flat_map(|witness| witness["target_slots"].as_array().into_iter().flatten())
        .collect();
    for expected in [
        serde_json::json!({"receiver": {"prototype": "set"}, "key": {"string": "add"}}),
        serde_json::json!({"receiver": {"constructor": "set"}, "key": {"string": "sibling"}}),
        serde_json::json!({"receiver": {"prototype": "array"}, "key": "symbol_iterator"}),
        serde_json::json!({"receiver": {"prototype": "set"}, "key": {"string": "delete"}}),
        serde_json::json!({"receiver": "unknown", "key": {"string": "add"}}),
    ] {
        assert!(
            slots.contains(&&expected),
            "missing {expected} from {slots:?}"
        );
    }
}

#[test]
fn primitive_argument_throw_is_preserved_by_the_selected_call_path() {
    let source = "console.log('before');\nconst marker = Symbol(1n + 1);\nconsole.log('after');\nexport { marker };\n";
    let opts = FixtureOpts::new(source, vec![])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let fixture = run_fixture(opts);
    assert_preserved_entry_error(&fixture, "TypeError");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = native_input_uses(&graph);
    assert_eq!(uses.len(), 1, "the exact selected call is still reported");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
}
