//! Encoder and empty-collection inputs select one exact construction and reject wrong shapes, slots or digests.

use super::selected_native_input;
use debundle_e2e_graph_support::reflective_provider::{
    REFLECT_CHUNKS, REFLECT_FILES, unknown_receiver_witness,
};
use debundle_e2e_graph_support::{
    FixtureGraphExt, json_execution_rows, json_has_unknown_call_target, json_node_declaring,
};
use debundle_e2e_support::*;

fn encoder_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const encoder ="))
        .unwrap();
    selected_native_input(
        source,
        declaration,
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    )
}

fn encoder_opts(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(source, vec![])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES)
}

#[test]
fn exact_encoder_input_keeps_adjacent_construction_and_returned_methods_opaque() {
    let source = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst encoder = new TextEncoder();\nconst adjacent = new TextEncoder();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst encoded = encoder.encode('ok');\nconst getter = globalThis.TextEncoder;\nconsole.log(encoded.length, adjacent.encoding, Boolean(getter));\nexport { encoder, adjacent, factory, LaterError, reflect };\n";
    let opts = || {
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values",
                &binding_members([
                    "encoder",
                    "adjacent",
                    "factory",
                    "LaterError",
                    "encoded",
                    "getter",
                ]),
                &[
                    "globalThis.saved = () => factory();",
                    "console.log(encoded.length, adjacent.encoding, Boolean(getter));",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES)
    };
    let control = run_fixture_with_graphs(opts());
    let graph = serde_json::to_value(control.dependency_graph()).unwrap();
    assert!(json_has_unknown_call_target(json_node_declaring(
        &graph, "encoder"
    )));
    let all = run_fixture_with_graphs(
        opts().with_program_premises(
            serde_json::from_value(serde_json::json!({
                "intrinsics_never_written": { "families": "all" }
            }))
            .unwrap(),
        ),
    );
    let all_graph = serde_json::to_value(all.dependency_graph()).unwrap();
    assert!(
        json_has_unknown_call_target(json_node_declaring(&all_graph, "encoder")),
        "old all-family assertions cannot supply a new host identity: {all_graph}"
    );
    let mut input = encoder_input(source);
    input.selector = SourceUnitSelector::ResolvedMember(ResolvedMemberSelector {
        module: "values".into(),
        name: "encoder".into(),
    });
    let fixture =
        run_fixture_with_graphs(opts().with_native_operation_inputs("static/app", vec![input]));
    assert_preserved_entry_output(&fixture, "2 utf-8 true\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program = serde_json::to_value(fixture.program_graph()).unwrap();
    unknown_receiver_witness(&program);
    assert!(
        !json_has_unknown_call_target(json_node_declaring(&graph, "encoder")),
        "{graph}"
    );
    assert!(
        json_has_unknown_call_target(json_node_declaring(&graph, "adjacent")),
        "{graph}"
    );
    assert!(
        json_has_unknown_call_target(json_node_declaring(&graph, "encoded")),
        "{graph}"
    );
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(uses.len(), 1, "{graph}");
    assert_eq!(uses[0]["evidence"]["operation"], "text_encoder_empty");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "encoder");
    assert_eq!(uses[0]["source_location"]["start_line"], 3);
}

#[test]
fn exact_encoder_input_removes_only_selected_reentry_constraint() {
    let source = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst encoder = new TextEncoder();\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(encoder.encoding, LaterError.name);\nexport { encoder, factory, LaterError, reflect };\n";
    let opts = || {
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "encoder",
                    &[Member::new("encoder")],
                    &["globalThis.saved = () => factory();"],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES)
    };
    assert!(
        !run_rejection_fixture_with_graphs(opts())
            .atomic_group_conflicts()
            .is_empty()
    );
    let fixture = run_fixture_with_graphs(
        opts().with_native_operation_inputs("static/app", vec![encoder_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "utf-8 LaterError\n");
}

#[test]
fn encoder_input_rejects_unsupported_shape_and_identity() {
    for initializer in [
        "new TextEncoder('utf-8')",
        "new TextEncoder(...[])",
        "TextEncoder()",
        "new globalThis.TextEncoder()",
        "new (0, TextEncoder)()",
        "(sideEffect(), new TextEncoder())",
        "(globalThis.result = new TextEncoder())",
        "new OtherEncoder()",
    ] {
        let source = format!("const encoder = {initializer};\nexport {{ encoder }};\n");
        expect_rejection_containing_all(
            encoder_opts(&source)
                .with_native_operation_inputs("static/app", vec![encoder_input(&source)]),
            &["native_operation_inputs"],
        );
    }
    let source = "class TextEncoder {}\nconst encoder = new TextEncoder();\nexport { encoder };\n";
    expect_rejection_containing_all(
        encoder_opts(source)
            .with_native_operation_inputs("static/app", vec![encoder_input(source)]),
        &["native_operation_inputs"],
    );
}

#[test]
fn empty_collection_inputs_reject_arguments_wrappers_and_wrong_identity() {
    for (initializer, operation) in [
        ("new Map([])", NativeOperationKind::MapEmpty),
        ("new Map(undefined)", NativeOperationKind::MapEmpty),
        ("new Map(...[])", NativeOperationKind::MapEmpty),
        ("new WeakMap([])", NativeOperationKind::WeakMapEmpty),
        ("new WeakMap(undefined)", NativeOperationKind::WeakMapEmpty),
        ("new WeakMap(...[])", NativeOperationKind::WeakMapEmpty),
        ("new Set([])", NativeOperationKind::SetEmpty),
        ("new Set(undefined)", NativeOperationKind::SetEmpty),
        ("new Set(...[])", NativeOperationKind::SetEmpty),
        ("new globalThis.Map()", NativeOperationKind::MapEmpty),
        (
            "new globalThis.WeakMap()",
            NativeOperationKind::WeakMapEmpty,
        ),
        ("new globalThis.Set()", NativeOperationKind::SetEmpty),
        ("new Alias()", NativeOperationKind::MapEmpty),
        ("new Alias()", NativeOperationKind::WeakMapEmpty),
        ("new (0, Map)()", NativeOperationKind::MapEmpty),
        ("new (0, WeakMap)()", NativeOperationKind::WeakMapEmpty),
        ("(sideEffect(), new Map())", NativeOperationKind::MapEmpty),
        (
            "(sideEffect(), new WeakMap())",
            NativeOperationKind::WeakMapEmpty,
        ),
        (
            "(globalThis.result = new Set())",
            NativeOperationKind::SetEmpty,
        ),
        (
            "(globalThis.result = new WeakMap())",
            NativeOperationKind::WeakMapEmpty,
        ),
        ("new Map()", NativeOperationKind::SetEmpty),
        ("new Map()", NativeOperationKind::WeakMapEmpty),
        ("new WeakMap()", NativeOperationKind::MapEmpty),
        ("new WeakMap()", NativeOperationKind::SetEmpty),
        ("new Set()", NativeOperationKind::MapEmpty),
    ] {
        let declaration = format!("const collection = {initializer};");
        let source = format!("{declaration}\n");
        expect_rejection_containing_all(
            FixtureOpts::new(&source, vec![]).with_native_operation_inputs(
                "static/app",
                vec![selected_native_input(
                    &source,
                    &declaration,
                    "collection",
                    operation,
                )],
            ),
            &["native_operation_inputs"],
        );
    }

    for (source, operation, constructor) in [
        (
            "class Map {}\nconst collection = new Map();\n",
            NativeOperationKind::MapEmpty,
            "const collection = new Map();",
        ),
        (
            "class Set {}\nconst collection = new Set();\n",
            NativeOperationKind::SetEmpty,
            "const collection = new Set();",
        ),
        (
            "class WeakMap {}\nconst collection = new WeakMap();\n",
            NativeOperationKind::WeakMapEmpty,
            "const collection = new WeakMap();",
        ),
        (
            "const Alias = Map;\nconst collection = new Alias();\n",
            NativeOperationKind::MapEmpty,
            "const collection = new Alias();",
        ),
        (
            "const Alias = Set;\nconst collection = new Alias();\n",
            NativeOperationKind::SetEmpty,
            "const collection = new Alias();",
        ),
        (
            "const Alias = WeakMap;\nconst collection = new Alias();\n",
            NativeOperationKind::WeakMapEmpty,
            "const collection = new Alias();",
        ),
        (
            "const collection = new Map();\neval('');\n",
            NativeOperationKind::MapEmpty,
            "const collection = new Map();",
        ),
        (
            "const collection = new Set();\neval('');\n",
            NativeOperationKind::SetEmpty,
            "const collection = new Set();",
        ),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new(source, vec![]).with_native_operation_inputs(
                "static/app",
                vec![selected_native_input(
                    source,
                    constructor,
                    "collection",
                    operation,
                )],
            ),
            &["native_operation_inputs"],
        );
    }

    for (source, operation, constructor, write) in [
        (
            "const collection = new Map();\nMap = replacement;\n",
            NativeOperationKind::MapEmpty,
            "const collection = new Map();",
            "Map",
        ),
        (
            "const collection = new Set();\nSet = replacement;\n",
            NativeOperationKind::SetEmpty,
            "const collection = new Set();",
            "Set",
        ),
        (
            "const collection = new WeakMap();\nWeakMap = replacement;\n",
            NativeOperationKind::WeakMapEmpty,
            "const collection = new WeakMap();",
            "WeakMap",
        ),
        (
            "const collection = new Map();\nglobalThis.Map = replacement;\n",
            NativeOperationKind::MapEmpty,
            "const collection = new Map();",
            "Map",
        ),
        (
            "const collection = new Set();\nglobalThis.Set = replacement;\n",
            NativeOperationKind::SetEmpty,
            "const collection = new Set();",
            "Set",
        ),
        (
            "const collection = new WeakMap();\nglobalThis.WeakMap = replacement;\n",
            NativeOperationKind::WeakMapEmpty,
            "const collection = new WeakMap();",
            "WeakMap",
        ),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new(source, vec![]).with_native_operation_inputs(
                "static/app",
                vec![selected_native_input(
                    source,
                    constructor,
                    "collection",
                    operation,
                )],
            ),
            &[write, "contradicted"],
        );
    }
}

#[test]
fn encoder_input_rejects_known_slot_writes_including_alias_and_sibling() {
    for mutation in [
        "TextEncoder = function replacement() {};",
        "const root = globalThis; root.TextEncoder = function replacement() {};",
        "Object.defineProperty(globalThis, 'TextEncoder', { get() { return function replacement() {}; } });",
    ] {
        let source =
            format!("const encoder = new TextEncoder();\n{mutation}\nexport {{ encoder }};\n");
        expect_rejection_containing_all(
            encoder_opts(&source)
                .with_native_operation_inputs("static/app", vec![encoder_input(&source)]),
            &["TextEncoder", "contradicted"],
        );
    }
    let source =
        "import './mutation.js';\nconst encoder = new TextEncoder();\nexport { encoder };\n";
    expect_rejection_containing_all(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&[(
                "static/mutation",
                "globalThis.TextEncoder = function replacement() {};\n",
            )])
            .with_native_operation_inputs("static/app", vec![encoder_input(source)]),
        &["TextEncoder", "static/mutation"],
    );
}

#[test]
fn encoder_input_rejects_stale_digest_and_mismatched_selector() {
    let source = "const encoder = new TextEncoder();\nexport { encoder };\n";
    let mut stale = encoder_input(source);
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        encoder_opts(source).with_native_operation_inputs("static/app", vec![stale]),
        &["sha256"],
    );
    let mut mismatched = encoder_input(source);
    mismatched.selector = SourceUnitSelector::SourceMatch(source_match_selector(
        "const missing = new TextDecoder();",
        &["missing"],
    ));
    expect_rejection_containing_all(
        encoder_opts(source).with_native_operation_inputs("static/app", vec![mismatched]),
        &["native_operation_inputs"],
    );
}

#[test]
fn unannotated_encoder_census_proves_timing_but_preserves_purity_fallback() {
    let source =
        "const encoder = new TextEncoder();\nconsole.log(encoder.encoding);\nexport { encoder };\n";
    let opts = || FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture_with_graphs(opts());
    assert_preserved_entry_output(&fixture, "utf-8\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let node = json_node_declaring(&graph, "encoder");
    assert!(!json_has_unknown_call_target(node), "{graph}");
    assert_eq!(node["purity"]["kind"], "not_pure");
    assert!(json_execution_rows(&graph, "native_operation_input_uses").is_empty());
    let fixture = run_fixture_with_graphs(
        opts().with_native_operation_inputs("static/app", vec![encoder_input(source)]),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let node = json_node_declaring(&graph, "encoder");
    assert_eq!(node["purity"]["kind"], "pure");
    assert_eq!(
        node["purity"]["dependencies"]["native_operation_inputs"][0]["input_index"],
        0
    );
}

#[test]
fn encoder_input_rejects_ambiguous_constructor_selection() {
    let source = "const encoder = new TextEncoder();\nconst adjacent = new TextEncoder();\nexport { encoder, adjacent };\n";
    expect_rejection_containing_all(
        encoder_opts(source)
            .with_native_operation_inputs("static/app", vec![encoder_input(source)]),
        &["native_operation_inputs"],
    );
}

#[test]
fn encoder_input_selects_one_comma_declarator_without_parentheses() {
    let source = "import { reflect } from './reflect.js';\nvar limit = 1e4, encoder = new TextEncoder;\nconsole.log(limit, encoder.encoding);\nexport { limit, encoder, reflect };\n";
    let input = selected_native_input(
        source,
        "var limit = 1e4, encoder = new TextEncoder;",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let fixture = run_fixture_with_graphs(
        encoder_opts(source).with_native_operation_inputs("static/app", vec![input]),
    );
    assert_preserved_entry_output(&fixture, "10000 utf-8\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(uses.len(), 1, "{graph}");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "encoder");
    assert!(
        !json_has_unknown_call_target(json_node_declaring(&graph, "encoder")),
        "{graph}"
    );
    assert_eq!(
        json_node_declaring(&graph, "encoder")["purity"]["kind"],
        "pure"
    );
}
