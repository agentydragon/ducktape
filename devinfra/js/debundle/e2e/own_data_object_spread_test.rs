use analysis::graph::HeapAddressReport;
use analysis::{DependencyGraphReport, EffectCellReport, KnownConflict, PremiseVerdict};
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
) -> HeapAddressReport {
    let execution = node_for_binding(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no execution evidence: {graph:#?}"));
    let reads = &execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no heap frontier: {execution:#?}"))
        .heap_access_provenance
        .executed_operations
        .reads;
    assert_eq!(reads.count, reads.sample.len(), "{binding}: {reads:#?}");
    let shape_origin = reads.sample.iter().find_map(|cell| match cell {
        EffectCellReport::ObjectShape { origin } => Some(origin.clone()),
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
                    if origin == &shape_origin && actual == property
            )),
            "{binding} did not read own property {property:?} from shape {shape_origin:?}: {reads:#?}"
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
        let reads = &heap.heap_access_provenance.executed_operations.reads;
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

fn assert_no_exact_own_property_read(graph: &DependencyGraphReport, binding: &str, property: &str) {
    let execution = node_for_binding(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no execution evidence: {graph:#?}"));
    let heap = execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("{binding} has no heap frontier: {execution:#?}"));
    let provenance = &heap.heap_access_provenance;
    let executed_reads = &provenance.executed_operations.reads;
    let summary_reads = &provenance.conservative_summaries.reads;
    assert_eq!(
        executed_reads.count,
        executed_reads.sample.len(),
        "{binding}: {executed_reads:#?}"
    );
    assert_eq!(
        summary_reads.count,
        summary_reads.sample.len(),
        "{binding}: {summary_reads:#?}"
    );
    let reads = executed_reads
        .sample
        .iter()
        .chain(summary_reads.sample.iter());
    assert!(
        !reads.clone().any(|cell| matches!(
            cell,
            EffectCellReport::OwnProperty { property: actual, .. } if actual == property
        )),
        "{binding} acquired a non-assumed own-property read for {property:?}: executed={executed_reads:#?}, summary={summary_reads:#?}"
    );
}

fn assert_exact_own_property_read(graph: &DependencyGraphReport, property: &str) {
    let mut reads = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .filter_map(|execution| execution.heap_frontier.as_ref())
        .flat_map(|heap| {
            let provenance = &heap.heap_access_provenance;
            provenance
                .executed_operations
                .reads
                .sample
                .iter()
                .chain(provenance.conservative_summaries.reads.sample.iter())
        });
    assert!(
        reads.any(|cell| matches!(
            cell,
            EffectCellReport::OwnProperty { property: actual, .. } if actual == property
        )),
        "no exact executed-or-summary own-property read for {property:?}: {graph:#?}"
    );
}

fn assert_property_frontier(
    graph: &DependencyGraphReport,
    later: usize,
    earlier: usize,
    origin: &HeapAddressReport,
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
                && edge.constrains_init_order
                && matches!(
                    &edge.premise_verdict,
                    Some(PremiseVerdict::Known { conflict })
                        if matches!(
                            conflict.as_ref(),
                            KnownConflict::SharedCell {
                                cell: EffectCellReport::OwnProperty {
                                    origin: actual_origin,
                                    property: actual_property,
                                },
                                ..
                            } if actual_origin == origin && actual_property == property
                        )
                )
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
    let origin = assert_exact_spread_reads(&graph, "copied", &["2", "10", "alpha", "__proto__"]);
    // Moving the later write before the spread would change the copied
    // function value. The exact cell edge is the concrete inversion guard.
    assert_property_frontier(&graph, 4, 3, &origin, "alpha");
}

#[test]
fn existing_own_data_value_store_preserves_later_spread_shape_proof() {
    let source = "const source = { alpha: 1, beta: 2 };\nsource.alpha = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "existing-own-data-store-spread-shape",
    );
    assert_preserved_entry_output(&fixture, "alpha,beta 3\n");

    // Replacing an existing writable data value preserves the enumerable
    // own-key layout that the later spread reads.
    assert_exact_spread_reads(&fixture.dependency_graph(), "copied", &["alpha", "beta"]);
}

#[test]
fn same_owner_own_data_stores_keep_later_spread_shape_proof() {
    let source = "const source = { alpha: 1, beta: 2 };\nsource.alpha = source.beta = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha, copied.beta);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "alpha,beta 3 3\n");

    assert_exact_spread_reads(&fixture.dependency_graph(), "copied", &["alpha", "beta"]);
}

#[test]
fn dynamic_store_in_same_owner_keeps_legacy_spread_barrier() {
    let source = "const source = { alpha: 1, beta: 2 };\nlet key = \"beta\";\n(source.alpha = 3, source[key] = 4);\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.alpha, copied.beta);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "alpha,beta 3 4\n");

    assert_no_exact_spread_reads(
        &fixture.dependency_graph(),
        "copied",
        "dynamic same-owner store",
    );
}

#[test]
fn pooled_spread_with_different_keysets_does_not_certify_old_values_or_absence() {
    let source = "let properties = { alpha: 1 };\nfunction readCopy() { const copied = { ...properties }; return copied.alpha; }\nconst first = readCopy();\nproperties = { beta: 2 };\nconst second = readCopy();\nconsole.log(first, second);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "1 undefined\n");
    assert_no_exact_spread_reads(
        &fixture.dependency_graph(),
        "copied",
        "a pooled spread site observed different source keysets",
    );
}

#[test]
fn pooled_spread_with_a_stable_keyset_keeps_its_exact_data_shape() {
    let source = "const properties = { alpha: 1 };\nfunction readCopy() { const copied = { ...properties }; return copied.alpha; }\nconst first = readCopy();\nproperties.alpha = 2;\nconst second = readCopy();\nconsole.log(first, second);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "1 2\n");
    assert_exact_spread_reads(&fixture.dependency_graph(), "copied", &["alpha"]);
}

#[test]
fn delete_keeps_legacy_spread_barrier() {
    let source = "const source = { alpha: 1, beta: 2 };\ndelete source.alpha;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), copied.beta);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "beta 2\n");

    assert_no_exact_spread_reads(&fixture.dependency_graph(), "copied", "delete");
}

#[test]
fn repeated_literal_with_a_one_generation_property_keeps_only_may_payload() {
    let source = "function make(include) { const object = { stable: 1 }; if (include) object.added = 2; return object; }\nconst first = make(true);\nconst second = make(false);\nconst observed = second.added;\nconsole.log(first.added, observed, second.stable);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "pooled-literal-one-generation-property",
    );
    assert_preserved_entry_output(&fixture, "2 undefined 1\n");

    // The source-site literal's represented generations disagree on `added`.
    // Payload from the first write is retained, but it cannot make the read on
    // the second instance a must-present own-property access.
    assert_no_exact_own_property_read(&fixture.dependency_graph(), "observed", "added");
}

#[test]
fn repeated_literal_with_the_same_existing_key_keeps_its_data_read() {
    let source = "function readStable() { const object = { stable: 1 }; return object.stable; }\nconst first = readStable();\nconst second = readStable();\nconsole.log(first, second);\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "pooled-literal-stable-existing-key",
    );
    assert_preserved_entry_output(&fixture, "1 1\n");
    assert_exact_own_property_read(&fixture.dependency_graph(), "stable");
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
    let origin = assert_exact_spread_reads(&graph, "copied", &["alpha"]);
    assert_property_frontier(&graph, 4, 1, &origin, "alpha");
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
            "uncertain enumeration retains data callbacks without invoking them",
            "let calls = 0;\nconst source = { value: () => { calls += 1; return later; } };\nconst descriptors = Object.getOwnPropertyDescriptors(source);\nObject.defineProperties(source, descriptors);\nconst copied = { ...source };\nconst later = 42;\nconsole.log(calls, copied.value());\n",
            "0 42\n",
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
        (
            "assignment through an existing setter remains conservative",
            "let writes = 0;\nconst source = { set value(value) { writes += value; } };\nsource.value = 3;\nconst copied = { ...source };\nconsole.log(Object.keys(copied).join(\",\"), writes);\n",
            "value 3\n",
        ),
    ];

    for (case, source, expected_output) in cases {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, expected_output);
        assert_no_exact_spread_reads(&fixture.dependency_graph(), "copied", case);
    }
}
