//! Selected `Set` and `WeakMap` constructions stay site-local while protocol writes and iterator aliases reject or stay opaque.

use super::{rejected_opts, selected_native_input};
use analysis_model::DepKind;
use debundle_e2e_graph_support::effect_validation::assert_commutation_accepted;
use debundle_e2e_graph_support::reflective_provider::{
    REFLECT_CHUNKS, REFLECT_FILES, unknown_receiver_witness,
};
use debundle_e2e_graph_support::{
    FixtureGraphExt, json_execution_rows, json_has_unknown_call_target, json_node_declaring,
};
use debundle_e2e_support::*;

fn primitive_set_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const made = new Set("))
        .expect("fixture has a direct Set initializer");
    native_operation_input(
        source,
        declared_source_unit(declaration.trim(), "made"),
        NativeOperationKind::SetFromPrimitiveArray,
        "Neutral fixture audits this one Set construction site.",
    )
}

fn resolved_member_native_input(
    source: &str,
    module: &str,
    name: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    native_operation_input(
        source,
        member_source_unit(module, name),
        operation,
        "Neutral fixture audits this exact native operation.",
    )
}

#[test]
fn selected_primitive_set_construction_removes_its_opaque_frontier() {
    const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nvar firstReason = 'alpha', secondReason = 'beta';\nconst made = new Set([firstReason, secondReason]);\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(made.size, LaterError.name);\nexport { factory, LaterError, reflect };\n";
    let opts = || {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "made",
                    &binding_members(["firstReason", "secondReason", "made"]),
                    &["globalThis.saved = () => factory();"],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES)
    };
    let control = run_rejection_fixture_with_graphs(opts());
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&serde_json::to_value(control.program_graph()).unwrap());
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
        json_has_unknown_call_target(json_node_declaring(&control_graph, "made")),
        "unproved Set construction still has a call frontier: {control_graph}"
    );
    let fixture = run_fixture_with_graphs(
        opts().with_native_operation_inputs("static/app", vec![primitive_set_input(SOURCE)]),
    );
    assert_preserved_entry_output(&fixture, "2 LaterError\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        !json_has_unknown_call_target(json_node_declaring(&graph, "made")),
        "the selected constructor should consume its native input: {graph}"
    );
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(
        uses.len(),
        1,
        "exactly the selected Set site uses the premise: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "set_from_primitive_array");
    unknown_receiver_witness(&program_graph);
}

#[test]
fn resolved_members_emit_only_the_selected_set_and_empty_map_sites() {
    const SET_DECLARATION: &str = "const made = new Set([firstReason, secondReason]);";
    const MAP_DECLARATION: &str = "const cache = new Map();";
    const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nvar firstReason = 'alpha', secondReason = 'beta';\nconst made = new Set([firstReason, secondReason]);\nconst cache = new Map();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(made.size, cache.size, LaterError.name, globalThis.saved().name);\nexport { factory, LaterError, reflect };\n";
    let make_opts = |inputs: Vec<NativeOperationInputContract>| {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module(
                    "collections",
                    &[
                        Member::new("firstReason"),
                        Member::new("secondReason"),
                        Member::source_alpha_target("setValues", "made", SET_DECLARATION),
                        Member::source_alpha_target("emptyCache", "cache", MAP_DECLARATION),
                    ],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES)
        .with_native_operation_inputs("static/app", inputs)
    };
    let set_input = resolved_member_native_input(
        SOURCE,
        "collections",
        "setValues",
        NativeOperationKind::SetFromPrimitiveArray,
    );
    let map_input = resolved_member_native_input(
        SOURCE,
        "collections",
        "emptyCache",
        NativeOperationKind::MapEmpty,
    );

    let absent = run_rejection_fixture_with_graphs(make_opts(vec![]));
    assert!(
        absent
            .atomic_group_conflicts()
            .iter()
            .any(
                |conflict| conflict["claims"].as_array().is_some_and(|claims| {
                    ["collections", "later"].iter().all(|module| {
                        claims
                            .iter()
                            .any(|claim| claim["module"].as_str() == Some(*module))
                    })
                }),
            ),
        "without native premises the collection/later split must remain blocked: {:#?}",
        absent.atomic_group_conflicts()
    );

    for (input, selected_binding, unselected_binding, expected_member) in [
        (&set_input, "made", "cache", "setValues"),
        (&map_input, "cache", "made", "emptyCache"),
    ] {
        let rejected = run_rejection_fixture_with_graphs(make_opts(vec![input.clone()]));
        let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
        let uses = json_execution_rows(&graph, "native_operation_input_uses");
        assert_eq!(
            uses.len(),
            1,
            "only the selected sibling uses the premise: {graph}"
        );
        assert_eq!(uses[0]["evidence"]["selected_binding"], selected_binding);
        assert_eq!(
            uses[0]["evidence"]["selector"]["resolved_member"]["name"], expected_member,
            "the input must resolve through the readable logical-member name: {graph}"
        );
        assert!(
            json_has_unknown_call_target(json_node_declaring(&graph, unselected_binding)),
            "an adjacent unselected constructor must keep its opaque call frontier: {graph}"
        );
        assert!(
            rejected
                .atomic_group_conflicts()
                .iter()
                .any(
                    |conflict| conflict["claims"].as_array().is_some_and(|claims| {
                        ["collections", "later"].iter().all(|module| {
                            claims
                                .iter()
                                .any(|claim| claim["module"].as_str() == Some(*module))
                        })
                    }),
                ),
            "certifying an adjacent site must not admit this split: {:#?}",
            rejected.atomic_group_conflicts()
        );
    }

    let fixture = run_fixture_with_graphs(make_opts(vec![set_input, map_input]));
    assert_preserved_entry_output(&fixture, "2 0 LaterError LaterError\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/collections.js",
        &["setValues", "emptyCache"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/collections.js",
        &["const setValues = new Set", "const emptyCache = new Map"],
        &[],
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(
        uses.len(),
        2,
        "each resolved member selects one constructor: {graph}"
    );
    for (binding, member) in [("made", "setValues"), ("cache", "emptyCache")] {
        assert!(
            uses.iter().any(|use_site| {
                use_site["evidence"]["selected_binding"] == binding
                    && use_site["evidence"]["selector"]["resolved_member"]["module"]
                        == "collections"
                    && use_site["evidence"]["selector"]["resolved_member"]["name"] == member
            }),
            "missing resolved-member premise for {binding}: {uses:#?}"
        );
    }
    unknown_receiver_witness(&serde_json::to_value(fixture.program_graph()).unwrap());
}

#[test]
fn primitive_set_owner_keeps_its_effect_coverage_dependency() {
    const SOURCE: &str = "const emptyMap = new Map();\nconst made = new Set(['alpha', 'beta']);\nconsole.log(made.size, emptyMap.size);\n";
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE, vec![logical_module("sets", &[Member::new("made")])])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![
                    primitive_set_input(SOURCE),
                    selected_native_input(
                        SOURCE,
                        "const emptyMap = new Map();",
                        "emptyMap",
                        NativeOperationKind::MapEmpty,
                    ),
                ],
            ),
    );
    assert_preserved_entry_output(&fixture, "2 0\n");

    let report = fixture.effect_translation_validation();
    assert!(report["legacy_gate_accepts"].as_bool().unwrap_or(false));
    assert_commutation_accepted(
        &report,
        "the reordered native owners have complete effect coverage",
    );
    assert_eq!(
        report["emitted_order"],
        serde_json::json!([1, 0, 2]),
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"]
            .as_u64()
            .unwrap_or_default(),
        0,
        "the exact native constructor result completes its named source owner: {report:#}"
    );
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["native_operation_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "both completed native owners retain their exact author premises: {report:#}"
    );
}

#[test]
fn weak_map_empty_input_is_site_local_and_keeps_later_behavior_opaque() {
    const SOURCE: &str = "function publish(key, value) { globalThis[key] = value; }\nconst key = {};\nconst cache = new WeakMap();\nlet other = new WeakMap();\nconst present = cache.has(key);\nconsole.log(present, Boolean(other));\n";
    let opts = || FixtureOpts::new(SOURCE, vec![]).with_import_order(&["residual"]);

    let fallback = run_fixture_with_graphs(opts());
    assert_preserved_entry_output(&fallback, "false true\n");
    let fallback_graph = serde_json::to_value(fallback.dependency_graph()).unwrap();
    assert!(json_has_unknown_call_target(json_node_declaring(
        &fallback_graph,
        "cache"
    )));

    let fixture = run_fixture_with_graphs(opts().with_native_operation_inputs(
        "static/app",
        vec![selected_native_input(
            SOURCE,
            "const cache = new WeakMap();",
            "cache",
            NativeOperationKind::WeakMapEmpty,
        )],
    ));
    assert_preserved_entry_output(&fixture, "false true\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        !json_has_unknown_call_target(json_node_declaring(&graph, "cache")),
        "the selected empty constructor should consume its exact-site input: {graph}"
    );
    assert!(
        json_has_unknown_call_target(json_node_declaring(&graph, "other")),
        "the input must not establish program-wide WeakMap integrity: {graph}"
    );
    assert!(
        json_has_unknown_call_target(json_node_declaring(&graph, "present")),
        "empty construction must not certify later WeakMap methods: {graph}"
    );
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(
        uses.len(),
        1,
        "only the selected constructor uses the input: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "weak_map_empty");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "cache");
}

#[test]
fn weak_map_empty_input_rejects_known_sibling_chunk_writes() {
    const SOURCE: &str =
        "import './mutation.js';\nconst collection = new WeakMap();\nexport { collection };\n";
    for mutation in [
        "WeakMap = function replacement() {};\n",
        "globalThis.WeakMap = function replacement() {};\n",
    ] {
        let input = selected_native_input(
            SOURCE,
            "const collection = new WeakMap();",
            "collection",
            NativeOperationKind::WeakMapEmpty,
        );
        expect_rejection_containing_all(
            FixtureOpts::new(SOURCE, vec![])
                .with_extra_chunks(&[("static/mutation", mutation)])
                .with_native_operation_inputs("static/app", vec![input]),
            &["global WeakMap replacement", "static/mutation"],
        );
    }
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
        "var reason = 'safe';\nconst made = new Set([reason]);\n(reason) = {};\nexport {};\n",
        "var reason = 'safe';\nconst made = new Set([reason]);\n[reason] = [{}];\nexport {};\n",
        "var reason = 'safe';\nconst made = new Set([reason]);\nfor (reason of [{}]) {}\nexport {};\n",
        "var reason = 'safe';\nconst made = new Set([reason]);\nfor (reason in { changed: true }) {}\nexport {};\n",
        "var reason = 'safe';\nconst made = new Set([reason]);\n(reason)++;\nexport {};\n",
    ] {
        expect_rejection_containing_all(
            rejected_opts(source, "made", primitive_set_input(source)),
            &[
                "direct unshadowed new Set([primitive literals or earlier stable primitive bindings])",
            ],
        );
    }
}

#[test]
fn dependent_array_iterator_key_remains_an_opaque_witness() {
    let source = "const made = new Set([1, 2]);\nconst before = Array.prototype[Symbol.iterator];\nArray.prototype[Symbol.iterator] = function replacement() {};\nArray.prototype[Symbol.iterator] = before;\nconsole.log(made.size);\nexport {};\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
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
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
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
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "done\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
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
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![primitive_set_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(
        json_execution_rows(&graph, "native_operation_input_uses").len(),
        1
    );
}

#[test]
fn construction_premise_preserves_later_aliased_mutator_order() {
    let source = "const made = new Set([1]);\nconst alias = made;\nalias.add(2);\nalias.delete(1);\nalias.clear();\nconst count = made.size;\nconsole.log(count);\nexport {};\n";
    let fixture = run_fixture_with_graphs(
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
