use analysis::{DepKind, DependencyGraphReport};
use debundle_e2e_support::*;

fn assert_property_frontier(graph: &DependencyGraphReport, later: usize, earlier: usize) {
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
                                && conflict["cell"]["property"] == "value"
                        })
                    })
        }),
        "property ordering {later} -> {earlier}: {:#?}",
        graph.edges
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
    assert_property_frontier(&graph, 3, 2);
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
    assert_property_frontier(&graph, 2, 1);
    assert_sequenced_order(&graph, 3, 2);
}
