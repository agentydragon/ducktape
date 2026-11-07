//! A local array content mutation must stay ordered before a separate reader.
use analysis::DependencyGraphReport;
use analysis_model::DepKind;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;
use spec::{IntrinsicFamilies, IntrinsicFamily, IntrinsicsNeverWritten};

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

fn assert_sequenced_path(graph: &DependencyGraphReport, later: usize, earlier: usize) {
    let later = source_unit_for_ordinal(graph, later).to_string();
    let earlier = source_unit_for_ordinal(graph, earlier).to_string();
    let mut pending = std::collections::VecDeque::from([later]);
    let mut reached = std::collections::BTreeSet::new();
    while let Some(current) = pending.pop_front() {
        if !reached.insert(current.clone()) {
            continue;
        }
        pending.extend(
            graph
                .edges
                .iter()
                .filter(|edge| edge.edge_kind == DepKind::Sequenced && edge.source == current)
                .map(|edge| edge.target.clone()),
        );
    }
    assert!(
        reached.contains(&earlier),
        "expected a Sequenced path from the later unit to the earlier unit; reached: {reached:#?}, edges: {:#?}",
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

const MAP_HEAP_FRONTIER_SOURCE: &str = r#"
const values = [];
values.push('first');
const cache = new Map();
values.push('second');
console.log(values.join(','), cache.size);
"#;
const UNKNOWN_HEAP_FRONTIER_SOURCE: &str = r#"
const values = [];
values.push('first');
const cache = new UnlistedCollection();
values.push('second');
console.log(values.join(','), cache.size);
"#;
const FIRST_PUSH: &str = "values.push('first');";
const SECOND_PUSH: &str = "values.push('second');";
const OBSERVE_ORDER: &str = "console.log(values.join(','), cache.size);";

fn heap_frontier_options(source: &'static str, family: IntrinsicFamily) -> FixtureOpts<'static> {
    let modules = vec![
        logical_module_with_anon(
            "values",
            &[Member::new("values")],
            &[FIRST_PUSH, SECOND_PUSH],
        ),
        logical_module("cache", &[Member::new("cache")]),
        logical_module_with_anon("observe", &[], &[OBSERVE_ORDER]),
    ];
    let premises = ProgramPremises {
        intrinsics_never_written: Some(IntrinsicsNeverWritten {
            families: IntrinsicFamilies::Listed([family].into_iter().collect()),
            note: Some("neutral source contains no Map writes".to_string()),
        }),
        ..ProgramPremises::default()
    };
    FixtureOpts::new(source, modules)
        .with_host_behavior(HostBehavior::default())
        .with_program_premises(premises)
}

#[test]
fn same_owner_completed_map_initialization_does_not_create_own_heap_barrier() {
    let fixture = run_fixture(heap_frontier_options(
        MAP_HEAP_FRONTIER_SOURCE,
        IntrinsicFamily::Map,
    ));
    assert_preserved_entry_output(&fixture, "first,second 0\n");
}

#[test]
fn frozen_namespace_initialization_does_not_create_own_heap_barrier() {
    let source = "const values = [];\nvalues.push('first');\nfunction run() { return 1; }\nconst facade = Object.freeze(Object.defineProperty({__proto__: null, run}, Symbol.toStringTag, {value: 'Module'}));\nvalues.push('second');\nconsole.log(values.join(','), facade.run());\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values",
                    &[Member::new("values")],
                    &[FIRST_PUSH, SECOND_PUSH],
                ),
                logical_module("facade", &[Member::new("run"), Member::new("facade")]),
                logical_module_with_anon(
                    "observe",
                    &[],
                    &["console.log(values.join(','), facade.run());"],
                ),
            ],
        )
        .with_host_behavior(HostBehavior::default())
        .with_program_premises(ProgramPremises {
            intrinsics_never_written: Some(IntrinsicsNeverWritten {
                families: IntrinsicFamilies::Listed(
                    [IntrinsicFamily::Object, IntrinsicFamily::Symbol]
                        .into_iter()
                        .collect(),
                ),
                note: Some("neutral source contains no Object or Symbol writes".to_string()),
            }),
            ..ProgramPremises::default()
        }),
    );
    assert_preserved_entry_output(&fixture, "first,second 1\n");
}

#[test]
fn frozen_namespace_with_inherited_descriptor_getter_keeps_heap_barrier() {
    let setup = "Object.defineProperty(Object.prototype, 'enumerable', {get() { values.push('during'); return false; }, configurable: true});";
    let cleanup = "delete Object.prototype.enumerable;";
    let source = format!(
        "const values = [];\nvalues.push('first');\nfunction run() {{ return 1; }}\n{setup}\nconst facade = Object.freeze(Object.defineProperty({{__proto__: null, run}}, Symbol.toStringTag, {{value: 'Module'}}));\n{cleanup}\nvalues.push('second');\nconsole.log(values.join(','), facade.run());\n"
    );
    let colocated = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("values"),
                Member::new("run"),
                Member::new("facade"),
            ],
            &[
                FIRST_PUSH,
                setup,
                cleanup,
                SECOND_PUSH,
                "console.log(values.join(','), facade.run());",
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "first,during,second 1\n");
    expect_cycle_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "values",
                    &[Member::new("values")],
                    &[FIRST_PUSH, SECOND_PUSH],
                ),
                logical_module_with_anon(
                    "facade",
                    &[Member::new("run"), Member::new("facade")],
                    &[setup, cleanup],
                ),
            ],
        ),
        &["values", "facade"],
    );
}

#[test]
fn unknown_construction_keeps_sequenced_fallback_barrier() {
    let cycle = expect_cycle_rejection(
        heap_frontier_options(UNKNOWN_HEAP_FRONTIER_SOURCE, IntrinsicFamily::Map),
        &["values", "cache"],
    );
    assert!(
        cycle["cut"]
            .as_array()
            .expect("SCC cut")
            .iter()
            .any(|edge| edge["kind"] == "sequenced"),
        "expected unknown construction to retain its sequenced fallback barrier: {cycle:#}"
    );
}

#[test]
fn empty_weak_map_does_not_create_a_heap_barrier_between_local_array_writes() {
    const SOURCE: &str = "const values = [];\nvalues.push('first');\nconst cache = new WeakMap();\nvalues.push('second');\nconsole.log(values.join(','), cache ? 'ready' : 'missing');\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "values",
                    &[Member::new("values")],
                    &["values.push('first');", "values.push('second');"],
                ),
                logical_module("cache", &[Member::new("cache")]),
                logical_module_with_anon(
                    "observe",
                    &[],
                    &["console.log(values.join(','), cache ? 'ready' : 'missing');"],
                ),
            ],
        )
        .with_host_behavior(HostBehavior::default()),
    );
    assert_preserved_entry_output(&fixture, "first,second ready\n");
}

#[test]
fn one_shot_array_born_after_opaque_call_does_not_inherit_old_frontier() {
    for member in ["[0]", ".length"] {
        let source = format!(
            "const old = [1];\nconst opaque = globalThis.makeSchema();\nconst fresh = [2];\nconst observed = fresh{member};\nconst retained = old{member};\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![
                logical_module("old", &[Member::new("old")]),
                logical_module("opaque", &[Member::new("opaque")]),
                logical_module("fresh", &[Member::new("fresh")]),
                logical_module("observed", &[Member::new("observed")]),
                logical_module("retained", &[Member::new("retained")]),
            ],
        ));
        let graph = fixture.dependency_graph();
        assert_sequenced_edge(&graph, 1, 0);
        assert_no_sequenced_edge(&graph, 3, 1);
        assert_sequenced_edge(&graph, 4, 1);
    }
}

#[test]
fn repeated_callable_allocation_does_not_discharge_old_frontier() {
    for member in ["[0]", ".length"] {
        let source = format!(
            "function make() {{ return [1]; }}\nconst early = make();\nconst opaque = globalThis.makeSchema();\nconst late = make();\nconst read = late{member};\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![
                logical_module("factory", &[Member::new("make")]),
                logical_module("early", &[Member::new("early")]),
                logical_module("opaque", &[Member::new("opaque")]),
                logical_module("late", &[Member::new("late")]),
                logical_module("read", &[Member::new("read")]),
            ],
        ));
        assert_sequenced_edge(&fixture.dependency_graph(), 4, 2);
    }
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

#[test]
fn pure_for_in_reader_precedes_later_array_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nfunction count() { let n = 0; for (const key in values) n++; return n; }\nconst first = count();\nvalues.push(42);\nconsole.log(first);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("count").with_purity(MemberPurity::Pure),
                    Member::new("first"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(first);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 3, 2);
}

#[test]
fn pure_coercion_reader_precedes_later_array_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nfunction text() { return '' + values; }\nconst before = text();\nvalues.push(42);\nconsole.log(before);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("text").with_purity(MemberPurity::Pure),
                    Member::new("before"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(before);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 3, 2);
}

#[test]
fn pure_getter_carrier_reader_precedes_later_array_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nconst box = { get value() { return values.length; } };\nfunction read() { return box.value; }\nconst before = read();\nvalues.push(42);\nconsole.log(before);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("box"),
                    Member::new("read").with_purity(MemberPurity::Pure),
                    Member::new("before"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(before);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 4, 3);
}

#[test]
fn pure_optional_reader_precedes_later_array_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nfunction read() { return values.length; }\nconst before = read?.();\nvalues.push(42);\nconsole.log(before);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("read").with_purity(MemberPurity::Pure),
                    Member::new("before"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(before);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 3, 2);
}

#[test]
fn pure_tagged_template_reader_precedes_later_array_write() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nfunction tag() { return values.length; }\nconst first = tag``;\nvalues.push(42);\nconsole.log(first);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("tag").with_purity(MemberPurity::Pure),
                    Member::new("first"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
            logical_module_with_anon("observe", &[], &["console.log(first);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 3, 2);
}

#[test]
fn array_allocation_keeps_opaque_heap_frontier_live_without_read_syntax() {
    let fixture = run_fixture(FixtureOpts::new(
        "const values = [];\nglobalThis.noop = () => {};\nglobalThis.noop();\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module_with_anon("register", &[], &["globalThis.noop = () => {};"]),
            logical_module_with_anon("invoke", &[], &["globalThis.noop();"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "");
    assert_sequenced_path(&fixture.dependency_graph(), 2, 0);
}

#[test]
fn fresh_array_allocation_can_precede_an_earlier_opaque_call() {
    let fixture = run_fixture(FixtureOpts::new(
        "function opaque() { return globalThis.initial; }\nconst before = opaque();\nconst values = [42];\nconsole.log(values[0], before);\n",
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module("before", &[Member::new("opaque"), Member::new("before")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42 undefined\n");
    assert_no_sequenced_edge(&fixture.dependency_graph(), 2, 1);
}

#[test]
fn eager_native_array_input_does_not_inherit_pre_allocation_heap_barrier() {
    let fixture = run_fixture(FixtureOpts::new(
        "const before = (() => 1)();\nconst keys = Object.keys([10]);\nconsole.log(keys.length + before);\n",
        vec![logical_module("keys", &[Member::new("keys")])],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
    assert_no_sequenced_edge(&fixture.dependency_graph(), 1, 0);
}

#[test]
fn lazy_array_origins_keep_opaque_calls_live_before_lexical_allocation() {
    let fixture = run_fixture(FixtureOpts::new(
        "globalThis.make = make;\nfunction make() { return []; }\nconst first = globalThis.make();\nconst second = globalThis.make();\nconsole.log(first.length, second.length);\n",
        vec![
            logical_module_with_anon("make", &[Member::new("make")], &["globalThis.make = make;"]),
            logical_module("first", &[Member::new("first")]),
            logical_module("second", &[Member::new("second")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 0\n");
    assert_sequenced_edge(&fixture.dependency_graph(), 3, 2);
}

#[test]
fn fresh_allocation_still_preserves_an_earlier_binding_tdz_observation() {
    let source = "function read() { try { return values[0]; } catch (error) { return error.name; } }\nconst before = read();\nconst values = [42];\nconsole.log(before, values[0]);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("reader", &[Member::new("read"), Member::new("before")]),
                logical_module("values", &[Member::new("values")]),
            ],
        ),
        &["reader", "values"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 42\n");
}

#[test]
fn dense_array_primitive_push_does_not_invoke_published_callbacks() {
    let source = "globalThis.observe = () => Later;\nconst values = [];\nvalues.push(1, 2);\nclass Later {}\nconsole.log(values.join(','));\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values",
                &[Member::new("values")],
                &["globalThis.observe = () => Later;", "values.push(1, 2);"],
            ),
            logical_module("later", &[Member::new("Later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1,2\n");
    let graph = fixture.dependency_graph();
    let push = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .expect("push unit");
    assert!(
        push.synchronous_execution
            .as_ref()
            .expect("execution report")
            .complete
    );
    assert!(!push.purity.is_pure());
}

#[test]
fn primitive_push_composes_with_callable_effects_and_tracks_later_lengths() {
    use analysis::reports::schema::{BindingEffectAuthorityReport, LexicalBindingEffectBody};

    let source = "const values = [];\nfunction append() { return values.push(1, 2); }\nconst first = append();\nconst second = append();\nconst later = 42;\nconsole.log(first, second, values.join(','), later);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values",
                &[
                    Member::new("values"),
                    Member::new("append"),
                    Member::new("first"),
                    Member::new("second"),
                ],
            ),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2 4 1,2,1,2 42\n");
    let graph = fixture.dependency_graph();
    for binding in ["first", "second"] {
        let node = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|item| item.binding == binding)
            })
            .expect("push result owner");
        let execution = node
            .synchronous_execution
            .as_ref()
            .expect("execution report");
        assert!(execution.complete, "{execution:#?}");
        let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
            &execution.binding_effect_authority
        else {
            panic!("missing composed push receipt: {execution:#?}");
        };
        assert!(
            receipt.calls.iter().any(|call| matches!(
                &call.target,
                LexicalBindingEffectBody::Native { property: Some(property), .. }
                    if property == "prototype.push"
            )),
            "{receipt:#?}"
        );
    }
}

#[test]
fn pooled_array_allocations_do_not_restore_descriptor_proofs() {
    use analysis::reports::schema::BindingEffectAuthorityReport;

    for (extra, covered) in [("", true), ("const second = make();\n", false)] {
        let source = format!(
            "function make() {{ return []; }}\nconst values = make();\n{extra}function append() {{ return values.push(1); }}\nconst result = append();\nconsole.log(result, values.join(','));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "1 1\n");
        let graph = fixture.dependency_graph();
        let execution = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|item| item.binding == "result")
            })
            .expect("push result owner")
            .synchronous_execution
            .as_ref()
            .expect("execution report");
        assert_eq!(
            matches!(
                execution.binding_effect_authority,
                Some(BindingEffectAuthorityReport::OperationReceipt { .. })
            ),
            covered,
            "{extra}: {execution:#?}"
        );
    }
}

#[test]
fn dense_array_push_retains_mutation_argument_and_escape_guards() {
    for operation in [
        "Array.prototype.push = function () { return Later; };\nvalues.push(1);",
        "const alias = values;\nalias.push = () => Later;\nvalues.push(1);",
        "Object.defineProperty(values, 'push', { get() { return () => Later; } });\nvalues.push(1);",
        "Object.defineProperty(values, 'length', { writable: false });\nvalues.push(1);",
        "Object.preventExtensions(values);\nvalues.push(1);",
        "Object.freeze(values);\nvalues.push(1);",
        "Object.setPrototypeOf(values, { push() { return Later; } });\nvalues.push(1);",
        "Object.defineProperty(Array.prototype, '0', { set(value) { globalThis.observe(); }, configurable: true });\nvalues.push(1);",
        "Object.defineProperty(Object.prototype, '0', { set(value) { globalThis.observe(); }, configurable: true });\nvalues.push(1);",
        "consume(values);\nvalues.push(1);",
        "values.push(() => Later);",
        "const argument = { get value() { return Later; } };\nvalues.push(argument.value);",
    ] {
        let source = format!(
            "globalThis.observe = () => Later;\nconst values = [];\n{operation}\nclass Later {{}}\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "values",
                        &[Member::new("values")],
                        &[operation.lines().last().expect("push statement")],
                    ),
                    logical_module("later", &[Member::new("Later")]),
                ],
            ),
            &[],
            &[],
        );
    }
}

#[test]
fn dense_array_push_invalidates_cached_iteration_without_losing_callbacks() {
    let source = "const values = [];\nfunction read() { return values.map(() => Later); }\nconst before = read();\nvalues.push(1);\nconst after = read();\nclass Later {}\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values",
                    &[
                        Member::new("values"),
                        Member::new("read"),
                        Member::new("before"),
                        Member::new("after"),
                    ],
                    &["values.push(1);"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["values", "later"],
        &["eager_use"],
    );

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "const values = [() => Later];\nvalues.push(1);\nconsume(values);\nclass Later {}\n",
            vec![
                logical_module_with_anon(
                    "values",
                    &[Member::new("values")],
                    &["values.push(1);", "consume(values);"],
                ),
                logical_module("later", &[Member::new("Later")]),
            ],
        ),
        &["values", "later"],
        &["eager_use"],
    );
}
