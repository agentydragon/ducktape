//! Certified, borrowed and spread `Set` inputs keep the frontier and callback composition they need.

use super::selected_native_input;
use analysis_domain::item_views::StatementKind;
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, json_node_declaring, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn certified_fresh_sets_split_after_unrelated_constructor_heap_barrier() {
    const DECLARATION: &str =
        "var seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);";
    const SOURCE: &str = "import { Box } from './box.js';\nconst prior = ['p'];\nconst memo = new Box(() => 1);\nvar seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);\nexport { prior, memo, seed, sibling, merged };\nconsole.log(prior.length, merged.size);\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "sets",
                &binding_members(["seed", "sibling", "merged",]),
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(&[(
            "static/box",
            "export class Box { #callback; constructor(callback) { this.#callback = callback; } value() { return this.#callback(); } }\n",
        )])
        .with_extra_chunk_logical_modules(&[(
            "static/box",
            vec![logical_module(
                "provider",
                &[Member::new("Box").with_purity(MemberPurity::PureNew)],
            )],
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/box")
            .with_retained_callback_constructor("Box", 1, &[0])
            .build()])
        .with_native_operation_inputs(
            "static/app",
            vec![
                selected_native_input(
                    SOURCE,
                    DECLARATION,
                    "seed",
                    NativeOperationKind::SetFromPrimitiveArray,
                ),
                selected_native_input(
                    SOURCE,
                    DECLARATION,
                    "sibling",
                    NativeOperationKind::SetFromPrimitiveArray,
                ),
            ],
        ),
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    let memo = source_unit_for_binding(&graph, "memo");
    let seed = source_unit_for_binding(&graph, "seed");
    for binding in ["seed", "sibling", "merged"] {
        let set = source_unit_for_binding(&graph, binding);
        assert!(
            !graph.edges.iter().any(|edge| {
                edge.source == set && edge.target == memo && edge.edge_kind == DepKind::Sequenced
            }),
            "fresh {binding} construction still depends on an earlier opaque effect: {graph:#?}"
        );
    }
    let merged = source_unit_for_binding(&graph, "merged");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == merged && edge.target == seed && edge.edge_kind == DepKind::EagerUse
        }),
        "inferred spread lost its source Set allocation dependency: {graph:#?}"
    );
    let observation = graph
        .nodes
        .iter()
        .find(|node| node.statement_kind == StatementKind::SideEffect)
        .expect("the following console observation");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == observation.id
                && edge.target == seed
                && edge.edge_kind == DepKind::Sequenced
        }),
        "the following observation lost the live Set frontier: {graph:#?}"
    );
    assert!(
        !graph.edges.iter().any(|edge| {
            graph
                .nodes
                .iter()
                .any(|node| node.statement_kind == StatementKind::Export && node.id == edge.source)
                && edge.edge_kind == DepKind::Sequenced
        }),
        "a local export list must not execute a heap event: {graph:#?}"
    );
}

#[test]
fn borrowed_and_static_member_sets_keep_their_existing_frontier() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { Box } from './box.js';\nconst source = ['a'];\nconst alias = source;\nconst memo = new Box(() => 1);\nconst seed = new Set(alias);\nconst lookup = { key: 'token' };\nconst unique = new Set([lookup.key]);\nconsole.log(seed.size, unique.size);\n",
            vec![],
        )
        .with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/box",
            "export class Box { #callback; constructor(callback) { this.#callback = callback; } value() { return this.#callback(); } }\n",
        )])
        .with_extra_chunk_logical_modules(&[(
            "static/box",
            vec![logical_module(
                "provider",
                &[Member::new("Box").with_purity(MemberPurity::PureNew)],
            )],
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/box")
            .with_retained_callback_constructor("Box", 1, &[0])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "1 1\n");
    let graph = fixture.dependency_graph();
    let seed = source_unit_for_binding(&graph, "seed");
    let memo = source_unit_for_binding(&graph, "memo");
    assert!(
        graph.edges.iter().any(|edge| edge.source == seed
            && edge.target == memo
            && edge.edge_kind == DepKind::Sequenced),
        "borrowed array's live frontier disappeared: {graph:#?}"
    );
    let graph = serde_json::to_value(graph).unwrap();
    assert_eq!(
        json_node_declaring(&graph, "seed")["synchronous_execution"]["complete"],
        true
    );
    assert_eq!(
        json_node_declaring(&graph, "unique")["purity"]["kind"],
        "pure",
        "the static-member source keeps its existing native purity admission: {graph}"
    );
    assert_eq!(
        json_node_declaring(&graph, "unique")["synchronous_execution"]["complete"],
        true,
        "the static-member source keeps its existing effect frontier: {graph}"
    );
}

#[test]
fn direct_borrowed_root_keeps_its_runtime_prefix_and_contents_frontier() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { Box } from './box.js';\nconst input = ['a'];\nconst memo = new Box(() => 1);\nconst result = new Set(input);\nconsole.log(result.size, result.has('a'));\n",
            vec![],
        )
        .with_import_order(&["residual"])
        .with_extra_chunks(&[(
            "static/box",
            "export class Box { #callback; constructor(callback) { this.#callback = callback; } value() { return this.#callback(); } }\n",
        )])
        .with_extra_chunk_logical_modules(&[(
            "static/box",
            vec![logical_module(
                "provider",
                &[Member::new("Box").with_purity(MemberPurity::PureNew)],
            )],
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/box")
            .with_retained_callback_constructor("Box", 1, &[0])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "1 true\n");
    let graph = fixture.dependency_graph();
    let result = source_unit_for_binding(&graph, "result");
    let memo = source_unit_for_binding(&graph, "memo");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result && edge.target == memo && edge.edge_kind == DepKind::Sequenced
        }),
        "the borrowed input keeps its live ArrayContents frontier: {graph:#?}"
    );
    let graph = serde_json::to_value(graph).unwrap();
    assert_eq!(
        json_node_declaring(&graph, "result")["synchronous_execution"]["complete"],
        true
    );
}

#[test]
fn inferred_set_spread_preserves_factory_callback_composition() {
    const DECLARATION: &str =
        "var seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);";
    const SOURCE: &str = "globalThis.saved = () => factory();\nvar seed = new Set(['a']), sibling = new Set(['b']), merged = new Set([...seed, 'c']);\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconsole.log(merged.size, LaterError.name);\nexport { factory, LaterError };\n";
    let inputs = || {
        vec![
            selected_native_input(
                SOURCE,
                DECLARATION,
                "seed",
                NativeOperationKind::SetFromPrimitiveArray,
            ),
            selected_native_input(
                SOURCE,
                DECLARATION,
                "sibling",
                NativeOperationKind::SetFromPrimitiveArray,
            ),
        ]
    };
    let opts = || {
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "sets",
                    &binding_members(["seed", "sibling", "merged"]),
                    &["globalThis.saved = () => factory();"],
                ),
                logical_module("factory", &[Member::new("factory")]),
                logical_module("later", &[Member::new("LaterError")]),
            ],
        )
    };
    let fixture =
        run_fixture_with_graphs(opts().with_native_operation_inputs("static/app", inputs()));
    assert_preserved_entry_output(&fixture, "2 LaterError\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        json_node_declaring(&graph, "merged")["synchronous_execution"]["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|proof| proof["conclusion"] == "primitive_set_iteration"),
        "the census-proved spread retains inferred provenance: {graph}"
    );
}

#[test]
fn repeated_var_input_selects_the_matched_declaration_site() {
    const DECLARATION: &str = "var seed = new Set(['a']);";
    const SOURCE: &str =
        "var seed;\nvar seed = new Set(['a']);\nconsole.log(seed.size);\nexport { seed };\n";
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE, vec![logical_module("sets", &[Member::new("seed")])])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![selected_native_input(
                    SOURCE,
                    DECLARATION,
                    "seed",
                    NativeOperationKind::SetFromPrimitiveArray,
                )],
            ),
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn inferred_spread_keeps_later_alias_mutation_order() {
    let source = "var seed = new Set([1]), merged = new Set([...seed, 3]);\nconst alias = merged;\nalias.add(4);\nconst after = merged.has(4);\nconsole.log(after);\nexport {};\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "sets",
                &binding_members(["seed", "merged", "alias"]),
            )],
        )
        .with_import_order(&["sets", "residual"]),
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
