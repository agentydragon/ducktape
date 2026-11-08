use analysis::{DependencyGraphReport, EffectCellReport};
use analysis_model::DepKind;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

fn node_for_binding<'a>(
    graph: &'a DependencyGraphReport,
    binding: &str,
) -> &'a analysis::SourceUnitReport {
    graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("no source unit declares {binding}: {graph:#?}"))
}

fn assert_exact_spread_reads(
    graph: &DependencyGraphReport,
    binding: &str,
    expected_properties: &[&str],
) -> u32 {
    let execution = node_for_binding(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no execution evidence: {graph:#?}"));
    let reads = &execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no heap frontier: {execution:#?}"))
        .non_assumed_heap_accesses
        .reads;
    assert_eq!(reads.count, reads.sample.len(), "{binding}: {reads:#?}");
    let shape_origin = reads.sample.iter().find_map(|cell| match cell {
        EffectCellReport::ObjectShape { origin } => Some(*origin),
        _ => None,
    });
    let shape_origin = shape_origin.unwrap_or_else(|| {
        panic!("{binding} did not retain a non-assumed object-shape read: {reads:#?}")
    });
    for property in expected_properties {
        assert!(
            reads.sample.iter().any(|cell| matches!(
                cell,
                EffectCellReport::OwnProperty { origin, property: actual }
                    if *origin == shape_origin && actual == property
            )),
            "{binding} did not read own property {property:?} from shape #{shape_origin}: {reads:#?}"
        );
    }
    shape_origin
}

fn assert_no_exact_spread_reads(graph: &DependencyGraphReport, binding: &str, case: &str) {
    let execution = node_for_binding(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{case}: {binding} has no execution evidence: {graph:#?}"));
    if let Some(heap) = &execution.heap_frontier {
        let reads = &heap.non_assumed_heap_accesses.reads;
        assert_eq!(reads.count, reads.sample.len(), "{case}: {reads:#?}");
        assert!(
            !reads.sample.iter().any(|cell| matches!(
                cell,
                EffectCellReport::ObjectShape { .. } | EffectCellReport::OwnProperty { .. }
            )),
            "{case}: refused spread acquired exact own-property reads: {reads:#?}"
        );
    }
}

fn assert_property_frontier(
    graph: &DependencyGraphReport,
    later: usize,
    earlier: usize,
    property: &str,
) {
    let unit = |ordinal| {
        &graph
            .nodes
            .iter()
            .find(|node| node.statement_ordinal.0 == ordinal)
            .unwrap_or_else(|| panic!("missing source unit {ordinal}: {graph:#?}"))
            .id
    };
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && &edge.source == unit(later)
                && &edge.target == unit(earlier)
                && serde_json::to_value(&edge.sequenced_cause).unwrap()["conflicts"]
                    .as_array()
                    .is_some_and(|conflicts| {
                        conflicts.iter().any(|conflict| {
                            conflict["cell"]["kind"] == "own_property"
                                && conflict["cell"]["property"] == property
                        })
                    })
        }),
        "spread read/write inversion for {property:?} was not blocked: {:#?}",
        graph.edges
    );
}

#[test]
fn settled_single_spread_reads_shape_and_properties_and_preserves_order_and_identity() {
    let source = "let later = 0;\nconst original = () => later;\nconst source = { 10: original, 2: original, alpha: original, [\"__proto__\"]: original };\nconst copied = { ...source };\nsource.alpha = () => -1;\nlater = 42;\nconsole.log(Object.keys(copied).join(\",\"), copied[\"__proto__\"] === original, copied.alpha());\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "2,10,alpha,__proto__ true 42\n");

    let graph = fixture.dependency_graph();
    assert_exact_spread_reads(&graph, "copied", &["2", "10", "alpha", "__proto__"]);
    // Moving the later write before the spread would change the copied
    // function value. The exact cell edge is the concrete inversion guard.
    assert_property_frontier(&graph, 4, 3, "alpha");
}

#[test]
fn candidate_that_moves_source_write_before_spread_is_typed_as_conflicting() {
    let source = "const source = { alpha: 3 };\nconst copied = { ...source };\nconst observed = helper();\nfunction helper() { return 1; }\nsource.alpha = 4;\nconsole.log(copied.alpha);\n";
    let baseline = run_fixture(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&baseline, "3\n");

    // Keep `copied` and the observation in the entry. Its static imports run
    // first: importing `helper` reaches the writer module, which imports only
    // the `source` provider and writes alpha before the entry evaluates the
    // spread. The provider has no dependency back to the entry.
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "b_writer",
                    &[Member::new("helper")],
                    &["source.alpha = 4;"],
                ),
                logical_module("z_source", &[Member::new("source")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );

    let graph = rejected.dependency_graph();
    assert_property_frontier(&graph, 4, 1, "alpha");
    let report = rejected.candidate_relocation_assessment();
    let unit_id = |ordinal| {
        graph
            .nodes
            .iter()
            .find(|node| node.statement_ordinal.0 == ordinal)
            .unwrap_or_else(|| panic!("missing source unit {ordinal}: {graph:#?}"))
            .id
            .strip_prefix("unit:")
            .unwrap()
            .parse::<u64>()
            .unwrap()
    };
    let position = |ordinal| {
        report["emitted_order"]
            .as_array()
            .expect("candidate emitted order")
            .iter()
            .position(|unit| unit.as_u64() == Some(unit_id(ordinal)))
            .unwrap_or_else(|| panic!("candidate omitted unit {ordinal}: {report:#}"))
    };
    assert!(
        position(4) < position(1),
        "the concrete candidate must move the alpha write before the spread: {report:#}"
    );
    assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
    assert_eq!(report["decision"]["status"], "conflict", "{report:#}");
    assert_eq!(
        report["decision"]["uncovered_modeled_may_conflict"], true,
        "the moved write must be refused for its own-property conflict: {report:#}"
    );
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
}

#[test]
fn changed_or_nondata_enumerability_keeps_the_conservative_spread_path() {
    let cases = [
        (
            "delete and re-add changes own-key order",
            "const source = { first: 1, second: 2 };\ndelete source.first;\nsource.first = 1;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.first);\n",
            "second,first 1\n",
        ),
        (
            "callable delete invalidates retained source shape",
            "const source = { first: 1, second: 2 };\nfunction removeFirst() { delete source.first; }\nremoveFirst();\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.first);\n",
            "second undefined\n",
        ),
        (
            "unresolved binding dynamic receiver revokes candidates",
            "const source = { alpha: 3 };\nglobalThis.unknownReceiver = {};\nconst target = globalThis.unknownReceiver;\nconst key = \"unrelated\";\ntarget[key] = 4;\nconst copied = { ...source };\nconsole.log(copied.alpha, source.alpha);\n",
            "3 3\n",
        ),
        (
            "mixed literal spread remains conservative",
            "const source = { value: 7 };\nconst copied = { before: 0, ...source, after: 2 };\nconsole.log(Object.keys(copied).join(\",\"), copied.value);\n",
            "before,value,after 7\n",
        ),
        (
            "descriptor-map application invalidates shape",
            "const source = { visible: 1, hidden: 2 };\nconst descriptors = Object.getOwnPropertyDescriptors(source);\nconst target = {};\nObject.defineProperties(target, descriptors);\nconst copied = { ...target };\nconsole.log(Object.keys(copied).join(\",\"), copied.hidden);\n",
            "visible,hidden 2\n",
        ),
        (
            "direct descriptor mutation remains conservative",
            "const source = { visible: 1, hidden: 2 };\nObject.defineProperty(source, \"hidden\", { enumerable: false });\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.hidden);\n",
            "visible undefined\n",
        ),
        (
            "accessor source remains conservative",
            "let reads = 0;\nconst source = { get value() { reads += 1; return 7; } };\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.value, reads);\n",
            "value 7 1\n",
        ),
        (
            "setter source remains conservative",
            "let writes = 0;\nconst source = { set value(value) { writes += value; } };\nconst copied = { ...source };\nconsole.log(Object.keys(copied).length, writes);\n",
            "1 0\n",
        ),
    ];

    for (case, source, expected_output) in cases {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, expected_output);
        assert_no_exact_spread_reads(&fixture.dependency_graph(), "copied", case);
    }
}
