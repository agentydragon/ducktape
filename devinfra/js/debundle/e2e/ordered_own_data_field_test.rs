use analysis::{
    DependencyGraphReport, EffectCellReport, KnownConflict, PremiseVerdict,
    reports::schema::BindingEffectAuthorityReport,
};
use analysis_model::DepKind;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

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
            .unwrap()
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
        "property ordering {later} -> {earlier}: {:#?}",
        graph.edges
    );
}

fn assert_property_shared_cell_premise(
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
            .unwrap()
            .id
    };
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && &edge.source == unit(later)
                && &edge.target == unit(earlier)
                && matches!(
                    &edge.premise_verdict,
                    Some(PremiseVerdict::Known {
                        conflict: KnownConflict::SharedCell {
                            cell: EffectCellReport::OwnProperty {
                                property: observed,
                                ..
                            }
                        }
                    }) if observed == property
                )
        }),
        "property shared-cell premise {later} -> {earlier}: {:#?}",
        graph.edges
    );
}

fn assert_non_assumed_own_property_read(
    graph: &DependencyGraphReport,
    binding: &str,
    property: &str,
) -> u32 {
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} execution evidence: {source:#?}"));
    let heap = execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} heap evidence: {execution:#?}"));
    let reads = &heap.non_assumed_heap_accesses.reads;
    assert_eq!(reads.count, reads.sample.len(), "{reads:#?}");
    reads
        .sample
        .iter()
        .find_map(|cell| match cell {
            EffectCellReport::OwnProperty {
                origin,
                property: observed,
            } if observed == property => Some(*origin),
            _ => None,
        })
        .unwrap_or_else(|| panic!("{binding} lacks an own-property read of {property}: {reads:#?}"))
}

fn assert_non_assumed_length_read(graph: &DependencyGraphReport, binding: &str) -> u32 {
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} execution evidence: {source:#?}"));
    let heap = execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} heap evidence: {execution:#?}"));
    let reads = &heap.non_assumed_heap_accesses.reads;
    assert_eq!(
        reads.count,
        reads.sample.len(),
        "all non-assumed reads must fit the bounded sample: {reads:#?}"
    );
    reads
        .sample
        .iter()
        .find_map(|cell| match cell {
            EffectCellReport::OwnProperty { origin, property } if property == "length" => {
                Some(*origin)
            }
            _ => None,
        })
        .unwrap_or_else(|| {
            panic!("{binding} did not record an exact non-assumed length read: {reads:#?}")
        })
}

fn assert_non_assumed_index_read(graph: &DependencyGraphReport, binding: &str) -> u32 {
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} execution evidence: {source:#?}"));
    let heap = execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} heap frontier: {execution:#?}"));
    let reads = &heap.non_assumed_heap_accesses.reads;
    assert_eq!(reads.count, reads.sample.len(), "{reads:#?}");
    reads
        .sample
        .iter()
        .find_map(|cell| match cell {
            EffectCellReport::OwnProperty { origin, property } if property == "0" => Some(*origin),
            _ => None,
        })
        .unwrap_or_else(|| panic!("{binding} lacks an exact index read: {reads:#?}"))
}

fn assert_no_non_assumed_index_read(graph: &DependencyGraphReport, binding: &str) {
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing {binding} execution evidence: {source:#?}"));
    if let Some(heap) = &execution.heap_frontier {
        let reads = &heap.non_assumed_heap_accesses.reads;
        assert!(
            !reads.sample.iter().any(|cell| matches!(
                cell,
                EffectCellReport::OwnProperty { property, .. }
                    if property.parse::<u32>().is_ok()
            )),
            "unsupported {binding} acquired an exact index read: {reads:#?}"
        );
    }
}

fn assert_no_non_assumed_length_read(graph: &DependencyGraphReport, binding: &str, case: &str) {
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("{case}: missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{case}: missing execution evidence: {source:#?}"));
    assert!(
        !matches!(
            execution.binding_effect_authority.as_ref(),
            Some(BindingEffectAuthorityReport::OperationReceipt { .. })
        ),
        "{case}: unsupported length read acquired ordered operation authority: {execution:#?}"
    );
    if let Some(heap) = &execution.heap_frontier {
        let reads = &heap.non_assumed_heap_accesses.reads;
        assert_eq!(
            reads.count,
            reads.sample.len(),
            "{case}: all non-assumed reads must fit the bounded sample: {reads:#?}"
        );
        assert!(
            !reads.sample.iter().any(|cell| matches!(
                cell,
                EffectCellReport::OwnProperty {
                    property,
                    ..
                } if property == "length"
            )),
            "{case}: unsupported shape recorded an exact non-assumed length read: {reads:#?}"
        );
    }
}

fn assert_push_preserves_root_length_read(
    graph: &DependencyGraphReport,
    later_binding: &str,
    earlier_binding: &str,
    read_origin: u32,
) {
    let source_unit = |binding: &str| {
        graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .unwrap_or_else(|| panic!("missing source unit for {binding}: {graph:#?}"))
            .id
            .clone()
    };
    let source = source_unit(later_binding);
    let target = source_unit(earlier_binding);
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.source == source
                && edge.target == target
                && serde_json::to_value(&edge.sequenced_cause).unwrap()["rule"] == "cell_order"
                && serde_json::to_value(&edge.sequenced_cause).unwrap()["conflicts"]
                    .as_array()
                    .is_some_and(|conflicts| {
                        let has_array_content_read = conflicts.iter().any(|conflict| {
                            conflict["kind"] == "write_after_read"
                                && conflict["cell"]["kind"] == "array_contents"
                                && conflict["cell"]["origin"] == read_origin
                        });
                        let has_length_frontier = conflicts.iter().any(|conflict| {
                            conflict["kind"] == "frontier_after_cell"
                                && conflict["cell"]["kind"] == "own_property"
                                && conflict["cell"]["property"] == "length"
                                && conflict["cell"]["origin"] == read_origin
                        });
                        has_array_content_read && has_length_frontier
                    })
        }),
        "push {later_binding} must preserve root length read {earlier_binding}: {:#?}",
        graph.edges,
    );
}

fn assert_sequenced_order(graph: &DependencyGraphReport, later: usize, earlier: usize) {
    let target = &graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == earlier)
        .unwrap()
        .id;
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.statement_ordinal.0 == later
                && &edge.target == target
        }),
        "ordering {later} -> {earlier}: {:#?}",
        graph.edges
    );
}

fn assert_no_fallback_suppressing_authority(fixture: &Fixture, binding: &str, case: &str) {
    let graph = fixture.dependency_graph();
    let source = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap_or_else(|| panic!("{case}: missing {binding} source unit: {graph:#?}"));
    let execution = source
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("{case}: missing ordered execution report: {source:#?}"));
    let fallback_safe = matches!(
        execution.binding_effect_authority.as_ref(),
        Some(
            BindingEffectAuthorityReport::Unclassified
                | BindingEffectAuthorityReport::OrderedRootInitializerUncovered { .. }
                | BindingEffectAuthorityReport::LegacyExecution { complete: false }
        )
    );
    assert!(
        fallback_safe,
        "{case}: unsafe input must retain fallback authority: {execution:#?}"
    );
}

#[test]
fn root_reads_stay_on_both_sides_of_an_alias_write() {
    // The literal's present data read is pure, but still observes mutable
    // contents. Its initializer assertion does not authorize reordering.
    let fixture = run_fixture(
        FixtureOpts::new(
            "const record = { value: 7 };\nconst alias = record;\nconst before = record.value;\nalias.value = 42;\nconst after = record.value;\nconsole.log(before, after);\n",
            vec![
                logical_module("record", &[Member::new("record"), Member::new("alias")]),
                logical_module("z_before", &[Member::new("before").with_initializer_purity()]),
                logical_module_with_anon("m_write", &[], &["alias.value = 42;"]),
                logical_module("a_after", &[Member::new("after")]),
                logical_module_with_anon("observe", &[], &["console.log(before, after);"]),
            ],
        ).with_dataflow_aware_s_chain(),
    );
    let graph = fixture.dependency_graph();
    assert_property_frontier(&graph, 3, 2, "value");
    assert_sequenced_order(&graph, 4, 3);
    assert_preserved_entry_output(&fixture, "7 42\n");
    let coverage = fixture.effect_translation_validation();
    assert!(
        coverage["coverage"]["unknown_coverage"]["general_heap"]
            .as_u64()
            .unwrap()
            > 0
    );
}

#[test]
fn root_ordinary_object_write_is_a_typed_shared_cell_premise_fact() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 7 };\nconst before = record.value;\nrecord.value = 42;\nconst after = record.value;\nconsole.log(before, after);\n",
        vec![
            logical_module("record", &[Member::new("record")]),
            logical_module("before", &[Member::new("before")]),
            logical_module_with_anon("write", &[], &["record.value = 42;"]),
            logical_module("after", &[Member::new("after")]),
            logical_module_with_anon("observe", &[], &["console.log(before, after);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42\n");

    let graph = fixture.dependency_graph();
    let origin = assert_non_assumed_own_property_read(&graph, "after", "value");
    let writer = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .unwrap_or_else(|| panic!("missing member-write source unit: {graph:#?}"));
    let execution = writer
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("missing member-write execution evidence: {writer:#?}"));
    let heap = execution
        .heap_frontier
        .as_ref()
        .unwrap_or_else(|| panic!("missing member-write heap evidence: {execution:#?}"));
    let writes = &heap.non_assumed_heap_accesses.writes;
    assert_eq!(writes.count, writes.sample.len(), "{writes:#?}");
    assert!(
        writes.sample.iter().any(|cell| matches!(
            cell,
            EffectCellReport::OwnProperty {
                origin: observed_origin,
                property,
            } if *observed_origin == origin && property == "value"
        )),
        "the source-covered member write must retain its possible own-property cell: {writes:#?}"
    );
    assert_property_shared_cell_premise(&graph, 3, 2, "value");

    // Exact own-cell evidence supplements the established source opacity; it
    // does not certify all possible effects of a member write.
    let coverage = fixture.effect_translation_validation();
    assert!(
        coverage["coverage"]["unknown_coverage"]["general_heap"]
            .as_u64()
            .unwrap()
            > 0
    );
}

#[test]
fn compound_owner_keeps_the_value_read_before_its_later_mutation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const record = { value: 7 };\nfunction mutate() { record.value = 42; return 1; }\nconst combined = record.value + mutate();\nconst after = record.value;\nconsole.log(combined, after);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("record"),
                Member::new("mutate"),
                Member::new("combined"),
                Member::new("after"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "8 42\n");
    let graph = fixture.dependency_graph();
    let compound = &graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .unwrap()
        .id;
    assert!(
        graph
            .edges
            .iter()
            .filter(|edge| &edge.source == compound || &edge.target == compound)
            .all(|edge| {
                !serde_json::to_value(&edge.sequenced_cause).unwrap()["conflicts"]
                    .as_array()
                    .is_some_and(|conflicts| {
                        conflicts
                            .iter()
                            .any(|conflict| conflict["cell"]["kind"] == "own_property")
                    })
            })
    );
}

#[test]
fn compound_initializer_captures_a_callback_before_later_argument_mutation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst holder = { callback: () => early };\nfunction mutate() { holder.callback = () => later; return 0; }\nfunction take(value, unused) { return value; }\nconst selected = take(holder.callback, mutate());\nconst result = selected();\nconst later = (result, 42);\nconsole.log(result, later);\n",
        vec![
            logical_module("a_record", &[Member::new("early"), Member::new("holder")]),
            logical_module("m_mutator", &[Member::new("mutate")]),
            logical_module("n_take", &[Member::new("take")]),
            logical_module(
                "z_observed",
                &[Member::new("selected"), Member::new("result")],
            ),
            logical_module("x_later", &[Member::new("later")]),
            logical_module_with_anon("observe", &[], &["console.log(result, later);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42\n");
}

#[test]
fn root_member_callee_is_selected_before_a_mutating_argument() {
    let fixture = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst holder = { run: () => early };\nfunction mutate() { holder.run = () => later; return 0; }\nconst result = holder.run(mutate());\nconst later = (result, 42);\nconsole.log(result, later);\n",
        vec![
            logical_module("a_record", &[Member::new("early"), Member::new("holder")]),
            logical_module("m_mutator", &[Member::new("mutate")]),
            logical_module("z_observed", &[Member::new("result")]),
            logical_module("x_later", &[Member::new("later")]),
            logical_module_with_anon("observe", &[], &["console.log(result, later);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("root member-call result source unit");
    let execution = result
        .synchronous_execution
        .as_ref()
        .expect("root execution report");
    assert!(execution.complete, "{execution:#?}");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        execution.binding_effect_authority.as_ref()
    else {
        panic!("member selection and calls have lexical receipts: {execution:#?}");
    };
    assert_eq!(
        receipt.calls.len(),
        2,
        "both the mutating argument and saved member target are represented: {receipt:#?}"
    );
    let early = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "early")
        })
        .expect("selected callback's observed binding source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == early.id
                && edge.binding.as_deref() == Some("early")
        }),
        "the selected target's observed binding read remains a graph edge: {graph:#?}"
    );
}

#[test]
fn invalid_or_borrowed_own_data_receivers_do_not_seal_root_receipts() {
    let frozen = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst later = 42;\nconst holder = Object.freeze({ run: () => early });\nfunction mutate() { holder.run = () => later; return 0; }\nconst result = holder.run(mutate());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("early"),
                Member::new("later"),
                Member::new("holder"),
                Member::new("mutate"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_error(&frozen, "Cannot assign to read only property 'run'");
    assert_no_fallback_suppressing_authority(&frozen, "result", "frozen receiver");

    let descriptor = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst later = 42;\nconst source = Object.freeze({ run: () => early });\nconst holder = {};\nObject.defineProperties(holder, Object.getOwnPropertyDescriptors(source));\nfunction mutate() { holder.run = () => later; return 0; }\nconst result = holder.run(mutate());\nconsole.log(result);\n",
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("early"),
                Member::new("later"),
                Member::new("source"),
                Member::new("holder"),
                Member::new("mutate"),
                Member::new("result"),
            ],
            &["Object.defineProperties(holder, Object.getOwnPropertyDescriptors(source));"],
        )],
    ));
    assert_preserved_entry_error(&descriptor, "Cannot assign to read only property 'run'");
    assert_no_fallback_suppressing_authority(&descriptor, "result", "non-writable descriptor");

    let accessor = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst later = 42;\nconst holder = { get run() { return () => early; }, set run(value) { console.log('setter'); } };\nfunction mutate() { holder.run = () => later; return 0; }\nconst result = holder.run(mutate());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("early"),
                Member::new("later"),
                Member::new("holder"),
                Member::new("mutate"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&accessor, "setter\n7\n");
    assert_no_fallback_suppressing_authority(&accessor, "result", "accessor receiver");

    let tainted = run_fixture(FixtureOpts::new(
        "globalThis.accept = function(value) {};\nconst early = 7;\nconst later = 42;\nconst holder = { run: () => early };\nglobalThis.accept(holder);\nfunction mutate() { holder.run = () => later; return 0; }\nconst result = holder.run(mutate());\nconsole.log(result);\n",
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("early"),
                Member::new("later"),
                Member::new("holder"),
                Member::new("mutate"),
                Member::new("result"),
            ],
            &[
                "globalThis.accept = function(value) {};",
                "globalThis.accept(holder);",
            ],
        )],
    ));
    assert_preserved_entry_output(&tainted, "7\n");
    assert_no_fallback_suppressing_authority(&tainted, "result", "externally tainted receiver");

    let borrowed = run_fixture(FixtureOpts::new(
        "const holder = { value: 1 };\nfunction mutate(record) { record.value = 42; return 0; }\nfunction take() { return 0; }\nconst result = take(mutate(holder));\nconsole.log(result, holder.value);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("holder"),
                Member::new("mutate"),
                Member::new("take"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&borrowed, "0 42\n");
    assert_no_fallback_suppressing_authority(&borrowed, "result", "borrowed formal receiver");
}

#[test]
fn root_record_children_keep_their_values_before_a_later_call() {
    let fixture = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst holder = { callback: () => early };\nfunction mutate() { holder.callback = () => later; return 0; }\nconst selected = { callback: holder.callback, unused: mutate() };\nconst result = selected.callback();\nconst later = (result, 42);\nconsole.log(result, later);\n",
        vec![
            logical_module("a_record", &[Member::new("early"), Member::new("holder")]),
            logical_module("m_mutator", &[Member::new("mutate")]),
            logical_module(
                "z_observed",
                &[Member::new("selected"), Member::new("result")],
            ),
            logical_module("x_later", &[Member::new("later")]),
            logical_module_with_anon("observe", &[], &["console.log(result, later);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42\n");
}

#[test]
fn a_throwing_root_operand_keeps_later_calls_unexecuted() {
    let fixture = run_fixture(FixtureOpts::new(
        "function take(value, unused) { return value; }\nfunction stop() { throw 'stopped'; }\nfunction later() { console.log('unexpected'); }\nconst result = take(stop(), later());\n",
        vec![logical_module(
            "all",
            &[
                Member::new("take"),
                Member::new("stop"),
                Member::new("later"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_error(&fixture, "stopped");
}

#[test]
fn an_own_getter_keeps_its_callback_effects() {
    let fixture = run_fixture(FixtureOpts::new(
        "const events = [];\nconst record = { get value() { events.push('get'); return 7; } };\nconst observed = record.value;\nconsole.log(observed, events.join(','));\n",
        vec![logical_module(
            "all",
            &[
                Member::new("events"),
                Member::new("record"),
                Member::new("observed"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 get\n");
    let graph = fixture.dependency_graph();
    assert!(
        !graph
            .nodes
            .iter()
            .find(|node| node.statement_ordinal.0 == 2)
            .unwrap()
            .synchronous_execution
            .as_ref()
            .unwrap()
            .complete
    );
}

#[test]
fn repeated_record_allocation_does_not_discharge_the_old_heap_frontier() {
    let fixture = run_fixture(FixtureOpts::new(
        "function make() { return { value: 1 }; }\nconst early = make();\nconst opaque = globalThis.makeSchema();\nconst late = make();\nconst observed = late.value;\nconsole.log(early, observed);\n",
        vec![
            logical_module("factory", &[Member::new("make")]),
            logical_module("early", &[Member::new("early")]),
            logical_module("opaque", &[Member::new("opaque")]),
            logical_module("late", &[Member::new("late")]),
            logical_module("observed", &[Member::new("observed").with_initializer_purity()]),
        ],
    ).with_dataflow_aware_s_chain());
    assert_sequenced_order(&fixture.dependency_graph(), 4, 2);
}

#[test]
fn nested_local_reads_and_writes_compose_into_the_callers_heap_frontier() {
    let fixture = run_fixture(FixtureOpts::new(
        "function snapshot() { const record = { value: 7 }; const before = record.value; record.value = 42; return before; }\nconst first = snapshot();\nconst opaque = globalThis.makeSchema();\nconst second = snapshot();\nconsole.log(first, second);\n",
        vec![
            logical_module("factory", &[Member::new("snapshot").with_purity(MemberPurity::Pure)]),
            logical_module("first", &[Member::new("first")]),
            logical_module("opaque", &[Member::new("opaque")]),
            logical_module("second", &[Member::new("second")]),
        ],
    ).with_dataflow_aware_s_chain());
    let graph = fixture.dependency_graph();
    assert_property_frontier(&graph, 2, 1, "value");
    assert_sequenced_order(&graph, 3, 2);
}

#[test]
fn a_root_dense_array_length_read_orders_against_push_and_preserves_output() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "const store = [];\nconst gamma = store.length;\nconst alpha = store.push(1);\nconsole.log(gamma, alpha);\n",
            vec![
                logical_module("array", &[Member::new("store")]),
                logical_module(
                    "read",
                    &[Member::new("gamma").with_initializer_purity()],
                ),
                logical_module("push", &[Member::new("alpha")]),
                logical_module_with_anon("observe", &[], &["console.log(gamma, alpha);"]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "0 1\n");
    let graph = fixture.dependency_graph();
    let length_origin = assert_non_assumed_length_read(&graph, "gamma");
    assert_push_preserves_root_length_read(&graph, "alpha", "gamma", length_origin);
}

#[test]
fn root_array_index_read_records_its_exact_slot_and_stays_before_a_write() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "const values = [7];\nconst before = values[0];\nvalues[0] = 42;\nconst after = values[0];\nconsole.log(before, after);\n",
            vec![
                logical_module("values", &[Member::new("values")]),
                logical_module("before", &[Member::new("before").with_initializer_purity()]),
                logical_module_with_anon("write", &[], &["values[0] = 42;"]),
                logical_module("after", &[Member::new("after")]),
                logical_module_with_anon("observe", &[], &["console.log(before, after);"]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "7 42\n");
    let graph = fixture.dependency_graph();
    assert_non_assumed_index_read(&graph, "before");
    assert_no_non_assumed_index_read(&graph, "after");
    assert_property_frontier(&graph, 2, 1, "0");
}

#[test]
fn root_array_index_read_refuses_absent_or_unsettled_slots() {
    for (source, output) in [
        (
            "const values = [7];\nconst key = 0;\nconst read = values[key];\nconsole.log(read);\n",
            "7\n",
        ),
        (
            "let calls = 0;\nfunction index() { calls++; return 0; }\nconst values = [7];\nconst read = values[index()];\nconsole.log(read, calls);\n",
            "7 1\n",
        ),
        (
            "const values = [7];\nconst read = values['00'];\nconsole.log(read);\n",
            "undefined\n",
        ),
        (
            "const values = [, 1];\nconst read = values[0];\nconsole.log(read);\n",
            "undefined\n",
        ),
        (
            "const seed = [7];\nconst values = [...seed];\nconst read = values[0];\nconsole.log(read);\n",
            "7\n",
        ),
        (
            "const values = [7];\nconst read = values[1];\nconsole.log(read);\n",
            "undefined\n",
        ),
        (
            "function make() { return [7]; }\nconst first = make();\nconst values = make();\nconst read = values[0];\nconsole.log(read);\n",
            "7\n",
        ),
        (
            "const values = [7];\nvalues[0] = 42;\nconst read = values[0];\nconsole.log(read);\n",
            "42\n",
        ),
        (
            "const values = [7];\nObject.defineProperty(values, '0', { get() { return 42; } });\nconst read = values[0];\nconsole.log(read);\n",
            "42\n",
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, output);
        assert_no_non_assumed_index_read(&fixture.dependency_graph(), "read");
    }
}

#[test]
fn push_cannot_be_forced_before_a_root_array_length_read() {
    let source = "const store = [];\nconst append = () => store.push(1);\nconst gamma = store.length;\nconst keep = append;\nconst alpha = append();\nconsole.log(gamma, alpha);\n";
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("array", &[Member::new("store")]),
                logical_module("push", &[Member::new("append"), Member::new("alpha")]),
                logical_module(
                    "read",
                    &[
                        Member::new("gamma").with_initializer_purity(),
                        Member::new("keep"),
                    ],
                ),
                logical_module_with_anon("observe", &[], &["console.log(gamma, alpha);"]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "0 1\n",
        "",
    );
    assert_gate_rejected_banner(&rejected.entry_path(), &rejected.report_path("cycles.json"));
    // The forced cyclic layout reaches append's lexical binding before its
    // initializer, while the original evaluates normally.
    assert_node_error(&rejected.entry_path(), "ReferenceError");

    let graph = rejected.dependency_graph();
    let length_origin = assert_non_assumed_length_read(&graph, "gamma");
    assert_push_preserves_root_length_read(&graph, "alpha", "gamma", length_origin);
    let cycles = rejected.cycles();
    assert!(
        cycles.iter().any(|cycle| {
            cycle["rejection"] == "mutual_constraining_cycle"
                && cycle["modules"].as_array().is_some_and(|modules| {
                    modules.iter().any(|module| module.as_str() == Some("read"))
                        && modules.iter().any(|module| module.as_str() == Some("push"))
                })
        }),
        "the forced module inversion must be refused as a read/push cycle: {cycles:#?}"
    );
}

#[test]
fn unsupported_root_length_reads_keep_fallback_and_runtime_behavior() {
    let getter = run_fixture(FixtureOpts::new(
        "const events = [];\nconst record = { get length() { events.push('get'); return 7; } };\nconst gamma = record.length;\nconsole.log(gamma, events.join(','));\n",
        vec![logical_module(
            "all",
            &[
                Member::new("events"),
                Member::new("record"),
                Member::new("gamma"),
            ],
        )],
    ));
    assert_preserved_entry_output(&getter, "7 get\n");
    assert_no_non_assumed_length_read(&getter.dependency_graph(), "gamma", "length getter");

    let unknown = run_fixture(FixtureOpts::new(
        "const store = Math.random() ? [] : [];\nconst gamma = store.length;\nconst alpha = store.push(1);\nconsole.log(gamma, alpha);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("store"),
                Member::new("gamma"),
                Member::new("alpha"),
            ],
        )],
    ));
    assert_preserved_entry_output(&unknown, "0 1\n");
    assert_no_non_assumed_length_read(
        &unknown.dependency_graph(),
        "gamma",
        "unknown or multi-array receiver",
    );

    let holey = run_fixture(FixtureOpts::new(
        "const store = [, 1];\nconst gamma = store.length;\nconst alpha = store.push(2);\nconsole.log(gamma, alpha);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("store"),
                Member::new("gamma"),
                Member::new("alpha"),
            ],
        )],
    ));
    assert_preserved_entry_output(&holey, "2 3\n");
    assert_no_non_assumed_length_read(&holey.dependency_graph(), "gamma", "holey array");

    let pooled = run_fixture(FixtureOpts::new(
        "function make() { return []; }\nconst store = make();\nconst spare = make();\nconst gamma = store.length;\nconst alpha = store.push(1);\nconsole.log(gamma, alpha);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("make"),
                Member::new("store"),
                Member::new("spare"),
                Member::new("gamma"),
                Member::new("alpha"),
            ],
        )],
    ));
    assert_preserved_entry_output(&pooled, "0 1\n");
    assert_no_non_assumed_length_read(
        &pooled.dependency_graph(),
        "gamma",
        "pooled array literal site",
    );
}
