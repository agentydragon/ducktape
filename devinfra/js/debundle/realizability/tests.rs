use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet};

use super::*;
use analysis::ids::ModuleId;
use analysis_vocabulary::inputs::AnalysisInputs;
use analyze::analyze_chunk;
use graph::DependencyGraph;
use graph::build_dependency_graph_with;
use graph::module_assignment::ModuleAssignment;
use report_fixtures::{active_source_unit, dependency_edge, residual_source_unit, singleton_graph};
use source_identity::SourceUnitId;

fn parse_and_build(source: &str) -> DependencyGraph {
    let module = raw_js_test_support::parse(source);
    let facts = analyze_chunk(&module, &AnalysisInputs::default(), None).facts;
    build_dependency_graph_with(&facts, Default::default(), &[])
        .unwrap()
        .0
}

#[test]
fn typed_assignment_projection_separates_imports_constraints_and_rebinds() {
    let graph = graph::DependencyGraphInput::from_report(singleton_graph(
        vec![
            residual_source_unit("lazy_caller", 0, &["lazy_caller"], 1),
            residual_source_unit("target", 1, &["target"], 1),
            residual_source_unit("sequenced_caller", 2, &["sequenced_caller"], 1),
            residual_source_unit("eager_caller", 3, &["eager_caller"], 1),
            residual_source_unit("writer", 4, &["writer"], 1),
        ],
        vec![
            dependency_edge(
                "edge:lazy",
                "lazy_caller",
                "target",
                DepKind::LazyUse,
                false,
            ),
            dependency_edge(
                "edge:eager",
                "eager_caller",
                "target",
                DepKind::EagerUse,
                true,
            ),
            dependency_edge(
                "edge:sequenced:first",
                "sequenced_caller",
                "target",
                DepKind::Sequenced,
                true,
            ),
            dependency_edge(
                "edge:sequenced:second",
                "sequenced_caller",
                "target",
                DepKind::Sequenced,
                true,
            ),
            dependency_edge(
                "edge:rebind",
                "writer",
                "target",
                DepKind::EagerRebind,
                false,
            ),
        ],
    ))
    .unwrap()
    .graph()
    .clone();
    let assignment = ModuleAssignment::from_assignments(
        vec![
            ModuleId(1),
            ModuleId(2),
            ModuleId(3),
            ModuleId(4),
            ModuleId(5),
        ],
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );

    let constraints = project_assignment_constraints(&graph, &assignment);

    assert_eq!(
        constraints.canonical.i_successors,
        BTreeMap::from([
            (ModuleId(1), BTreeSet::from([ModuleId(2)])),
            (ModuleId(3), BTreeSet::from([ModuleId(2)])),
            (ModuleId(4), BTreeSet::from([ModuleId(2)])),
        ]),
        "lazy imports remain in the canonical I-graph, while rebinds do not"
    );
    assert_eq!(
        constraints
            .canonical
            .edges
            .keys()
            .copied()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([(ModuleId(3), ModuleId(2)), (ModuleId(4), ModuleId(2)),]),
        "only initialization-constraining edges enter the C graph"
    );
    assert_eq!(
        constraints
            .canonical
            .edges_for(ModuleId(3), ModuleId(2))
            .len(),
        1,
        "parallel sequenced source edges contribute one C-edge witness"
    );
    assert_eq!(constraints.cross_rebinds.len(), 1);
    assert_eq!(constraints.cross_rebinds[0].from, ModuleId(5));
    assert_eq!(constraints.cross_rebinds[0].to, ModuleId(2));
    assert_eq!(
        graph
            .edge(constraints.cross_rebinds[0].dependency_edge)
            .from,
        SourceUnitId(4),
        "the separate rebind witness retains its source edge identity"
    );
}

#[test]
fn supplied_priority_verification_does_not_repair_a_failed_witness() {
    let graph = parse_and_build(
        "const hub = () => [a, b];\n\
         const a = { dependency: hub, lazy: () => b };\n\
         const b = { dependency: hub };",
    );
    let mut assignment = assignment_with(&graph, &[]);
    for unit in 0..3 {
        assignment.set(SourceUnitId(unit), ModuleId(unit + 1));
    }
    let nodes = (0..4).map(ModuleId).collect();
    let path = |module: ModuleId| spec::ModulePath::parse(&format!("m{}", module.0), "").unwrap();
    let priority = |ids: &[usize]| ids.iter().map(|&id| path(ModuleId(id))).collect::<Vec<_>>();
    let (accepted, _) = crate::validation::validate_assignment_with_priority(
        &graph,
        &assignment,
        &path,
        &nodes,
        &priority(&[2, 3, 1]),
    )
    .unwrap();
    assert!(accepted.verdict.is_realizable());
    let (rejected, schedule) = crate::validation::validate_assignment_with_priority(
        &graph,
        &assignment,
        &path,
        &nodes,
        &priority(&[3, 2, 1]),
    )
    .unwrap();
    assert!(!rejected.verdict.is_realizable());
    assert_eq!(
        schedule.imports(ModuleId(0)),
        &[ModuleId(3), ModuleId(2), ModuleId(1)]
    );
    for malformed in [
        vec![],
        vec![2, 3],
        vec![2, 3, 3],
        vec![0, 2, 3],
        vec![2, 3, 9],
    ] {
        assert!(matches!(
            crate::validation::validate_assignment_with_priority(
                &graph,
                &assignment,
                &path,
                &nodes,
                &priority(&malformed),
            ),
            Err(ImportScheduleError::PriorityPaths { .. })
        ));
    }
}

/// The exact import-order problem includes directed Hamiltonian path even with
/// an acyclic C star. Check the reduction against real source analysis, not a
/// separately invented graph model. An accepted all-entry baseline does not
/// make checking an arbitrary later batch assignment easier in general.
#[test]
fn source_hub_reduction_matches_directed_hamiltonian_path() {
    let vertices = [ModuleId(2), ModuleId(3), ModuleId(4)];
    let hub = ModuleId(1);
    let possible: Vec<_> = (0..3)
        .flat_map(|from| {
            (0..3)
                .filter(move |&to| from != to)
                .map(move |to| (from, to))
        })
        .collect();
    let permutations = [
        [0, 1, 2],
        [0, 2, 1],
        [1, 0, 2],
        [1, 2, 0],
        [2, 0, 1],
        [2, 1, 0],
    ];
    for mask in 0..(1 << possible.len()) {
        let h: BTreeSet<_> = possible
            .iter()
            .enumerate()
            .filter_map(|(bit, &edge)| (mask & (1 << bit) != 0).then_some(edge))
            .collect();
        let mut source = String::from("const z = () => [a0, a1, a2];\n");
        for from in 0..3 {
            let targets = h
                .iter()
                .filter(|&&(node, _)| node == from)
                .map(|&(_, to)| format!("a{to}"))
                .collect::<Vec<_>>()
                .join(",");
            source.push_str(&format!(
                "const a{from} = {{ dependency: z, lazy: () => [{targets}] }};\n"
            ));
        }
        let graph = parse_and_build(&source);
        let atoms = graph::compute_atomic_groups(&graph);
        assert_eq!(atoms.len(), 4, "H mask {mask}: {source}");
        assert!(
            atoms.iter().all(|atom| atom.members.len() == 1),
            "the distinct-owner map must not split a structural atom: {atoms:?}"
        );
        let mut assignment = assignment_with(&graph, &[]);
        assert!(check_realizability(&graph, &assignment).is_realizable());
        for source_unit in 0..4 {
            assignment.set(SourceUnitId(source_unit), ModuleId(source_unit + 1));
        }
        let (canonical, rebinds) = canonical_assignment_constraints(&graph, &assignment);
        assert!(rebinds.is_empty());
        let c: BTreeSet<_> = vertices.iter().map(|&vertex| (vertex, hub)).collect();
        assert_eq!(canonical.pairs().collect::<BTreeSet<_>>(), c);
        let expected_i: BTreeSet<_> = c
            .iter()
            .copied()
            .chain(vertices.iter().map(|&vertex| (hub, vertex)))
            .chain(h.iter().map(|&(from, to)| (vertices[from], vertices[to])))
            .collect();
        let actual_i: BTreeSet<_> = canonical
            .i_successors
            .iter()
            .flat_map(|(&from, row)| row.iter().map(move |&to| (from, to)))
            .collect();
        assert_eq!(actual_i, expected_i, "H mask {mask}: {source}");
        let has_path = permutations
            .iter()
            .any(|path| path.windows(2).all(|edge| h.contains(&(edge[0], edge[1]))));
        assert_eq!(
            check_realizability(&graph, &assignment).is_realizable(),
            has_path,
            "H mask {mask}: {source}"
        );
        // The standalone loader model agrees with the gate's scheduled verdict.
        let simulator = EsmEvaluationSimulator::build(&canonical.i_successors, &c, ModuleId(0));
        let modules = (0..5).map(ModuleId).collect();
        assert_eq!(
            simulator.tdz_pairs(&modules, &c).next().is_none(),
            has_path,
            "H mask {mask}: {source}"
        );
    }
}

#[test]
fn large_module_import_schedule_retains_every_target_in_shared_order() {
    use esm_schedule::EsmImportOrder;
    use esm_schedule::EsmImportSchedule;

    let entry_module_id = ModuleId(0);
    let importer = ModuleId(1);
    let targets: BTreeSet<ModuleId> = (2..=97).map(ModuleId).collect();
    let nodes: BTreeSet<ModuleId> = targets
        .iter()
        .copied()
        .chain([entry_module_id, importer])
        .collect();
    let i_successors = BTreeMap::from([
        (importer, targets.clone()),
        (ModuleId(95), BTreeSet::from([ModuleId(4)])),
        (ModuleId(4), BTreeSet::from([ModuleId(75)])),
    ]);
    // Linker positions intentionally differ from ModuleId order. The other
    // targets have no position and must remain in stable ID order.
    let constraining_pairs = BTreeSet::from([
        (importer, ModuleId(95)),
        (ModuleId(95), ModuleId(4)),
        (ModuleId(4), ModuleId(75)),
    ]);
    let import_order = EsmImportOrder::build(&constraining_pairs, &i_successors, &nodes);
    let schedule = EsmImportSchedule::construct(
        entry_module_id,
        &constraining_pairs,
        &i_successors,
        &nodes,
        &import_order,
    )
    .unwrap();
    let mut emitted_imports: Vec<_> = targets.iter().copied().map(|target| (target, ())).collect();
    import_order.sort_module_imports(&mut emitted_imports);
    let expected: Vec<_> = emitted_imports
        .into_iter()
        .map(|(target, ())| target)
        .collect();
    assert_eq!(schedule.imports(importer), expected);
    assert!(schedule.tdz_pairs(&constraining_pairs).next().is_none());
    schedule
        .sort_imports(
            importer,
            &mut targets
                .iter()
                .rev()
                .copied()
                .map(|target| (target, ()))
                .collect::<Vec<_>>(),
        )
        .unwrap();
}

/// Two top-level constants in different modules, with one reading
/// the other at-init across the module boundary acyclically. No
/// cycle, no rebind — verdict is empty.
#[test]
fn acyclic_cross_module_at_init_read_is_realizable() {
    let source = "const a = 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    // SourceUnit 0: const a = 1 → module 1.
    // SourceUnit 1: const b = a + 1 → module 2.
    // Edge source_unit_1 → source_unit_0 (eager_use of `a`).
    let assignment = assignment_with(&dependency_graph, &[(0, 1), (1, 2)]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        verdict.is_realizable(),
        "verdict should be empty: {verdict:#?}"
    );
}

/// Same setup but flipped to create a constraining cycle: both
/// statements live in different modules and mutually at-init read
/// the other. Quotient has a 2-cycle of constraining edges →
/// unrealizable.
#[test]
fn constraining_cycle_across_two_modules_is_unrealizable() {
    // Two top-level constants whose initializers eager-read each
    // other. Real JS would TDZ at runtime, but the analyzer just
    // records the structural graph: two `eager_use` edges in
    // opposite directions. Placing them in different modules
    // forms a constraining-edge SCC of the quotient — exactly
    // what clause 3 rejects.
    let source = "const a = b + 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    let assignment = assignment_with(&dependency_graph, &[(1, 1)]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        !verdict.is_realizable(),
        "verdict should report an SCC: {verdict:#?}"
    );
    let modules: BTreeSet<ModuleId> = verdict
        .unrealizable_sccs
        .iter()
        .flat_map(|scc| scc.core.modules.iter().copied())
        .collect();
    assert!(modules.contains(&ModuleId(0)));
    assert!(modules.contains(&ModuleId(1)));
    assert!(
        verdict
            .unrealizable_sccs
            .iter()
            .all(|scc| !scc.core.constraining_dependency_edges.is_empty()),
        "every SCC must carry source_unit-edge evidence"
    );
}

/// Modules 1 and 2 at-init read each other (Pass 1). Module 3 at-init reads
/// module 1, which lazily reads module 3, so the I-SCC {1, 2, 3} strictly
/// contains the Pass-1 SCC. With a cyclic constraining subgraph there is no
/// linker order, so Pass 2 does not run and only the Pass-1 SCC is reported
/// (a simulator run under `ModuleId` order would always reject the superset,
/// since its mutual pair cannot both hold). Breaking the mutual pair gives
/// the simulator a linker order, which rescues {1, 3}.
#[test]
fn pass_two_is_skipped_while_pass_one_has_a_cycle() {
    let nodes = |count: usize| {
        (0..count)
            .map(|i| {
                let name = format!("unit:{i}");
                if i == 0 {
                    residual_source_unit(&name, i + 1, &["Seed"], 1)
                } else {
                    active_source_unit(&name, i + 1, &["Value"], 1, "ui/m")
                }
            })
            .collect::<Vec<_>>()
    };
    let edge = |id: &str, from: &str, to: &str, kind: DepKind, constrains: bool| {
        dependency_edge(id, from, to, kind, constrains)
    };
    let shared = vec![
        edge("e:12", "unit:1", "unit:2", DepKind::EagerUse, true),
        edge("e:31", "unit:3", "unit:1", DepKind::EagerUse, true),
        edge("e:13", "unit:1", "unit:3", DepKind::LazyUse, false),
    ];
    let mut cyclic = shared.clone();
    cyclic.push(edge("e:21", "unit:2", "unit:1", DepKind::EagerUse, true));
    let verdict_of = |edges: Vec<_>| {
        let graph = graph::DependencyGraphInput::from_report(singleton_graph(nodes(4), edges))
            .unwrap()
            .graph()
            .clone();
        let assignment = ModuleAssignment::from_assignments(
            (0..4).map(ModuleId).collect(),
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        check_realizability(&graph, &assignment)
    };

    let rejections: BTreeSet<(Vec<ModuleId>, SccRejection)> = verdict_of(cyclic)
        .unrealizable_sccs
        .into_iter()
        .map(|scc| (scc.core.modules.into_iter().collect(), scc.rejection))
        .collect();
    assert_eq!(
        rejections,
        BTreeSet::from([(
            vec![ModuleId(1), ModuleId(2)],
            SccRejection::MutualConstrainingCycle
        )]),
    );

    let acyclic = verdict_of(shared);
    assert!(acyclic.is_realizable(), "{acyclic:#?}");
}

#[test]
fn pure_lazy_cycle_is_realizable() {
    let source = "function a() { return b(); } function b() { return a(); }";
    let dependency_graph = parse_and_build(source);
    let assignment = assignment_with(&dependency_graph, &[(1, 1)]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        verdict.is_realizable(),
        "lazy-only cycle should be realizable: {verdict:#?}"
    );
}

/// Namespace-aggregator split: a module-level `const ids = {...sub1, ...sub2}`
/// gets sub1 and sub2 extracted into separate modules, both INDEPENDENT of
/// residual (pure literal initializers). The split is realizable: ESM evaluates
/// sub1, sub2, then ids, then residual.
#[test]
fn namespace_aggregator_with_pure_subs_is_realizable() {
    // source_unit_0: const sub1 = { foo: 1 }
    // source_unit_1: const sub2 = { bar: 2 }
    // source_unit_2: const ids = {...sub1, ...sub2}
    // source_unit_3: console.log(ids)
    let source = "const sub1 = { foo: 1 }; const sub2 = { bar: 2 }; const ids = {...sub1, ...sub2}; console.log(ids);";
    let dependency_graph = parse_and_build(source);
    // sub1 → mod_sub1, sub2 → mod_sub2, ids → mod_ids.
    let assignment = assignment_with(&dependency_graph, &[(0, 1), (1, 2), (2, 3)]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        verdict.is_realizable(),
        "pure namespace-aggregator split must be realizable; verdict: {verdict:#?}",
    );
}

/// Namespace-aggregator TDZ hole: a cycle closed only by a promoted edge is
/// unrealizable.
///
/// The cycle goes through a *promoted* edge — the sub-module's at-init
/// `readSeed()` call has its body's read of `seed` (in residual) promoted
/// to a sub→residual eager edge. The lenient projection view
/// (`EndpointView::Lenient`) drops it under
/// `EdgeRole::is_cross_module_promotion` because the call target
/// `readSeed` lives in `mod_helpers`, not `mod_sub1`. With the
/// drop, the gate sees no cycle. Without the drop, the cycle
/// `residual→mod_ids→mod_sub1→residual` is closed.
///
/// ESM runtime DFS from residual:
///   residual → mod_ids → mod_sub1 → mod_helpers (eval helpers)
///                                 → residual (on stack, skip).
///   Post-order: helpers, then mod_sub1.
///   When `mod_sub1`'s body evaluates `readSeed()`, the call reads
///   `seed` from residual — residual is mid-DFS, `seed` is TDZ-locked.
///   ⇒ `ReferenceError: Cannot access 'seed' before initialization`.
///
/// The gate-side view (`EndpointView::Gate`) keeps the promoted
/// edge so the cycle is detected; the test pins that behaviour.
#[test]
fn promoted_edge_in_aggregator_cycle_is_unrealizable() {
    // source_unit_0: const seed = "S"                  (residual)
    // source_unit_1: const readSeed = () => seed       (mod_helpers)
    // source_unit_2: const sub1 = { foo: readSeed() }  (mod_sub1) — at-init call into mod_helpers
    // source_unit_3: const ids = sub1.foo + "x"        (mod_ids)
    // source_unit_4: const consumed = ids              (residual)
    let source = "const seed = \"S\"; const readSeed = () => seed; const sub1 = { foo: readSeed() }; const ids = sub1.foo + \"x\"; const consumed = ids; console.log(consumed);";
    let dependency_graph = parse_and_build(source);
    let assignment = assignment_with(&dependency_graph, &[(1, 1), (2, 2), (3, 3)]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        !verdict.is_realizable(),
        "promoted-edge aggregator cycle must be flagged by the gate \
         (mod_sub1's readSeed() at-init call reads `seed` in residual; \
         residual reads `ids` in mod_ids; mod_ids reads `sub1` in \
         mod_sub1 — closes a cycle through the promoted edge); \
         verdict: {verdict:#?}",
    );
}

/// All source units in the same module → no cross-destination edges of
/// any kind → empty verdict.
#[test]
fn single_module_is_always_realizable() {
    let source = "const a = 1; const b = a + 1; const c = a * b;";
    let dependency_graph = parse_and_build(source);
    let assignment = assignment_with(&dependency_graph, &[]);
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(verdict.is_realizable());
}

/// An assignment with every source unit in module 0 (the entry), then each
/// `(source unit, module)` pair applied in order.
fn assignment_with(
    dependency_graph: &DependencyGraph,
    assignments: &[(usize, usize)],
) -> ModuleAssignment {
    let mut assignment = ModuleAssignment::new(
        dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    for &(source_unit, module) in assignments {
        assignment.set(SourceUnitId(source_unit), ModuleId(module));
    }
    assignment
}

/// Entry imports every module and evaluates last, so a constraining edge into
/// entry is a TDZ even without a return edge in the source I-graph.
#[test]
fn entry_dependency_is_rejected() {
    let graph = parse_and_build("const a = 1; const b = a + 1;");
    let mut assignment = assignment_with(&graph, &[(1, 1)]);
    assert!(!check_realizability(&graph, &assignment).is_realizable());
    // Moving the dependency into the same emitted module repairs the violation.
    assignment.set(SourceUnitId(0), ModuleId(1));
    assert!(check_realizability(&graph, &assignment).is_realizable());
}

/// Modules 5 and 6 at-init read each other (Pass 1), so the verdict skips
/// Pass 2. Independently, 2 and 4 both eagerly read 1, while 1 lazily imports
/// both consumers. That star has no valid ESM traversal at ANY root. Collapsing
/// {5, 6} cannot rescue it, so the report retains a conditional Pass-2 diagnosis.
#[test]
fn validation_reports_pass_two_after_collapsing_pass_one_as_conditional() {
    let nodes = (0..7)
        .map(|i| {
            let name = format!("unit:{i}");
            if i == 0 {
                residual_source_unit(&name, i + 1, &["Seed"], 1)
            } else {
                active_source_unit(&name, i + 1, &["Value"], 1, "ui/m")
            }
        })
        .collect();
    let edges = vec![
        dependency_edge("e:21", "unit:2", "unit:1", DepKind::EagerUse, true),
        dependency_edge("e:41", "unit:4", "unit:1", DepKind::EagerUse, true),
        dependency_edge("e:12", "unit:1", "unit:2", DepKind::LazyUse, false),
        dependency_edge("e:14", "unit:1", "unit:4", DepKind::LazyUse, false),
        dependency_edge("e:56", "unit:5", "unit:6", DepKind::EagerUse, true),
        dependency_edge("e:65", "unit:6", "unit:5", DepKind::EagerUse, true),
    ];
    let graph = graph::DependencyGraphInput::from_report(singleton_graph(nodes, edges))
        .unwrap()
        .graph()
        .clone();
    let assignment = ModuleAssignment::from_assignments(
        (0..7).map(ModuleId).collect(),
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    let verdict = check_realizability(&graph, &assignment);
    assert_eq!(
        verdict
            .unrealizable_sccs
            .iter()
            .map(|scc| (scc.core.modules.clone(), scc.rejection))
            .collect::<Vec<_>>(),
        [(
            BTreeSet::from([ModuleId(5), ModuleId(6)]),
            SccRejection::MutualConstrainingCycle
        )],
    );
    let report = crate::validation::validate_assignment(&graph, &assignment, &|module| {
        spec::ModulePath::parse(&format!("m{}", module.0), "").unwrap()
    });
    let cycles: Vec<(Vec<&str>, crate::validation::BlockingRejection)> = report
        .cycles
        .iter()
        .map(|cycle| {
            (
                cycle.modules.iter().map(|path| path.as_str()).collect(),
                cycle.rejection,
            )
        })
        .collect();
    assert_eq!(
        cycles,
        [
            (
                vec!["m5", "m6"],
                crate::validation::BlockingRejection::MutualConstrainingCycle
            ),
            (
                vec!["m1", "m2", "m4"],
                crate::validation::BlockingRejection::TdzAfterCollapsingPassOne
            ),
        ],
    );
}
