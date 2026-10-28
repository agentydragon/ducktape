//! A local array content mutation must stay ordered before a separate reader.
use analysis::{DepKind, DependencyGraphReport};
use debundle_e2e_support::*;

fn source_unit_for_ordinal(graph: &DependencyGraphReport, ordinal: usize) -> &str {
    graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == ordinal)
        .unwrap_or_else(|| panic!("no dependency graph node with ordinal {ordinal}"))
        .id
        .as_str()
}

fn assert_sequenced_edge(graph: &DependencyGraphReport, later: usize, earlier: usize) {
    let later = source_unit_for_ordinal(graph, later);
    let earlier = source_unit_for_ordinal(graph, earlier);
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced && edge.source == later && edge.target == earlier
        }),
        "expected Sequenced edge {later} -> {earlier}; edges: {:#?}",
        graph.edges,
    );
}

fn assert_no_sequenced_edge(graph: &DependencyGraphReport, a: usize, b: usize) {
    let a = source_unit_for_ordinal(graph, a);
    let b = source_unit_for_ordinal(graph, b);
    let found: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && ((edge.source == a && edge.target == b)
                    || (edge.source == b && edge.target == a))
        })
        .collect();
    assert!(
        found.is_empty(),
        "independent array cells must not be sequenced: {found:#?}"
    );
}

#[test]
fn push_precedes_aliased_pure_reader_across_modules_in_both_modes() {
    // A pure function can still return a value that depends on mutable heap
    // contents. This contract asserts absence of side effects; it does not
    // make `readFirst()` referentially transparent across the preceding push.
    let source = "const values = [];\nconst alias = values;\nfunction readFirst() { return alias[0]; }\nvalues.push(42);\nconst first = readFirst();\nconsole.log(first);\n";
    for dataflow_aware in [false, true] {
        let mut opts = FixtureOpts::new(
            source,
            vec![
                logical_module("values", &[Member::new("values"), Member::new("alias")]),
                logical_module(
                    "a_reader",
                    &[
                        Member::new("readFirst").with_purity(MemberPurity::Pure),
                        Member::new("first"),
                    ],
                ),
                logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
                logical_module_with_anon("observe", &[], &["console.log(first);"]),
            ],
        );
        if dataflow_aware {
            opts = opts.with_dataflow_aware_s_chain();
        }
        let fixture = run_fixture(opts);
        assert_preserved_entry_output(&fixture, "42\n");
        assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
    }
}

#[test]
fn pure_reader_stays_before_later_array_write() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "const values = [7];\nconst alias = values;\nfunction readFirst() { return alias[0]; }\nconst first = readFirst();\nvalues[0] = 42;\nconsole.log(first);\n",
            vec![
                logical_module("values", &[Member::new("values"), Member::new("alias")]),
                logical_module(
                    "a_reader",
                    &[
                        Member::new("readFirst").with_purity(MemberPurity::Pure),
                        Member::new("first"),
                    ],
                ),
                logical_module_with_anon("z_mutation", &[], &["values[0] = 42;"]),
                logical_module_with_anon("observe", &[], &["console.log(first);"]),
            ],
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
}

#[test]
fn independent_array_readers_do_not_create_a_sequence_edge() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "const left = [1];\nconst right = [2];\nconst leftFirst = left[0];\nconst rightFirst = right[0];\nconsole.log(leftFirst, rightFirst);\n",
            vec![
                logical_module("left", &[Member::new("left")]),
                logical_module("right", &[Member::new("right")]),
                logical_module("a_left_read", &[Member::new("leftFirst")]),
                logical_module("b_right_read", &[Member::new("rightFirst")]),
                logical_module_with_anon("observe", &[], &["console.log(leftFirst, rightFirst);"]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    assert_no_sequenced_edge(&graph, 2, 3);
}

#[test]
fn unknown_call_after_nested_array_publication_stays_before_pure_reader() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [1];\nconst alias = values;\nfunction readFirst() { return alias[0]; }\nconst before = readFirst();\nglobalThis.saved = { nested: { values } };\nglobalThis.mutate = () => { globalThis.saved.nested.values[0] = 7; };\nglobalThis.mutate();\nconst after = readFirst();\nconsole.log(before, after);\n",
        vec![
            logical_module("values", &[Member::new("values"), Member::new("alias")]),
            logical_module(
                "a_reader",
                &[Member::new("readFirst").with_purity(MemberPurity::Pure)],
            ),
            logical_module_with_anon(
                "z_publish_and_mutate",
                &[Member::new("before"), Member::new("after")],
                &[
                    "globalThis.saved = { nested: { values } };",
                    "globalThis.mutate = () => { globalThis.saved.nested.values[0] = 7; };",
                    "globalThis.mutate();",
                ],
            ),
            logical_module_with_anon("observe", &[], &["console.log(before, after);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 7\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
    assert_sequenced_edge(&fixture.dependency_graph(), 5, 4);
    assert_sequenced_edge(&fixture.dependency_graph(), 6, 5);
    assert_sequenced_edge(&fixture.dependency_graph(), 7, 6);
}

#[test]
fn inline_reader_call_is_a_heap_barrier_when_call_summary_is_unavailable() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [7];\nconst first = (() => values[1])();\nvalues.push(42);\nconsole.log(first);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module("a_reader", &[Member::new("first")]),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(first);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "undefined\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 2, 1);
}

#[test]
fn returned_closure_call_is_a_heap_barrier_when_body_summary_is_unavailable() {
    for dataflow_aware in [false, true] {
        let mut opts = FixtureOpts::new(
            "const values = [];\nfunction makeReader() { return () => values.length; }\nconst read = makeReader();\nconst first = read();\nvalues.push(42);\nconsole.log(first);\n",
            vec![
                logical_module("values", &[Member::new("values")]),
                logical_module(
                    "a_factory",
                    &[
                        Member::new("makeReader").with_purity(MemberPurity::Pure),
                        Member::new("read").with_purity(MemberPurity::Pure),
                    ],
                ),
                logical_module("b_reader", &[Member::new("first")]),
                logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
                logical_module_with_anon("observe", &[], &["console.log(first);"]),
            ],
        );
        if dataflow_aware {
            opts = opts.with_dataflow_aware_s_chain();
        }
        let fixture = run_fixture(opts);
        assert_preserved_entry_output(&fixture, "0\n");
        assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
    }
}

#[test]
fn aliased_function_call_is_a_heap_barrier_when_body_summary_is_unavailable() {
    for dataflow_aware in [false, true] {
        let mut opts = FixtureOpts::new(
            "const values = [];\nfunction readLength() { return values.length; }\nconst alias = readLength;\nconst first = alias();\nvalues.push(42);\nconsole.log(first);\n",
            vec![
                logical_module(
                    "a_reader",
                    &[
                        Member::new("readLength").with_purity(MemberPurity::Pure),
                        Member::new("alias"),
                        Member::new("first"),
                    ],
                ),
                logical_module("values", &[Member::new("values")]),
                logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
                logical_module_with_anon("observe", &[], &["console.log(first);"]),
            ],
        );
        if dataflow_aware {
            opts = opts.with_dataflow_aware_s_chain();
        }
        let fixture = run_fixture(opts);
        assert_preserved_entry_output(&fixture, "0\n");
        assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
    }
}
