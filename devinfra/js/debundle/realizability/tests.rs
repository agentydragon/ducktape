use std::collections::{BTreeMap, BTreeSet};

use super::incremental_quotient::{
    ConstrainingBucket, OverlayGraphView, constraining_bucket_is_empty_after_overlay,
};
use super::*;
use crate::counted_digraph::CountedDiGraph;
use analysis::SourceUnitId;
use analysis::facts::analyze_chunk;
use analysis::graph::{EdgeRole, build_dependency_graph_with};
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use analysis::{AnalysisInputs, DepKind, DependencyGraph};
use petgraph::visit::IntoNeighbors;
use report_fixtures::{
    active_source_unit, dependency_edge, promoted_dependency_edge, residual_source_unit,
    singleton_graph,
};

fn parse_and_build(source: &str) -> DependencyGraph {
    let module = raw_js_test_support::parse(source);
    let facts = analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None).facts;
    build_dependency_graph_with(&facts, Default::default()).unwrap()
}

/// The hypothetical full gate must reproduce the pure canonical projection
/// and complete verdict, including diagnoses in I-SCCs remote from the moved
/// source units. Every graph here is a typed dependency-graph report fixture; no source
/// parser or alternate SCC/simulator implementation participates.
#[test]
fn full_overlay_verdict_matches_pure_gate_across_typed_moves() {
    let names = [
        "unit:0", "unit:1", "unit:2", "unit:3", "unit:4", "unit:5", "unit:6", "unit:7",
    ];
    let kinds = [
        DepKind::EagerUse,
        DepKind::LazyUse,
        DepKind::HoistedUse,
        DepKind::Sequenced,
        DepKind::LocalEffect,
        DepKind::CoDeclaration,
        DepKind::CoLocation,
        DepKind::EagerRebind,
        DepKind::LazyRebind,
        DepKind::DeferredRebind,
    ];
    for seed in 0..32usize {
        let nodes = names
            .iter()
            .enumerate()
            .map(|(i, name)| {
                if i % 3 == 0 {
                    residual_source_unit(name, i + 1, &["Value"], 5)
                } else {
                    active_source_unit(name, i + 1, &["Value"], 5, "ui/m")
                }
            })
            .collect();
        let mut edges = Vec::new();
        for i in 0..28usize {
            let from = (i * 11 + seed * 3) % names.len();
            let to = (i * 7 + seed + 1) % names.len();
            let kind = kinds[(i + seed * 5) % kinds.len()];
            let constrains = matches!(
                kind,
                DepKind::EagerUse
                    | DepKind::Sequenced
                    | DepKind::LocalEffect
                    | DepKind::CoDeclaration
                    | DepKind::CoLocation
            );
            let id = format!("edge:{seed}:{i}");
            edges.push(if i % 5 == 0 {
                promoted_dependency_edge(
                    &id,
                    names[from],
                    names[to],
                    kind,
                    constrains,
                    names[(from + 2) % names.len()],
                )
            } else {
                dependency_edge(&id, names[from], names[to], kind, constrains)
            });
        }
        // Multiple sequenced source unit edges on one module pair must select the
        // first DependencyEdgeId while retaining all non-sequenced evidence.
        edges.push(dependency_edge(
            "edge:sequenced:first",
            names[0],
            names[1],
            DepKind::Sequenced,
            true,
        ));
        edges.push(dependency_edge(
            "edge:sequenced:second",
            names[0],
            names[1],
            DepKind::Sequenced,
            true,
        ));
        let graph = DependencyGraph::from_report(&singleton_graph(nodes, edges)).unwrap();
        let assignments = (0..names.len())
            .map(|i| ModuleId((i * 3 + seed) % 5))
            .collect();
        let assignment = ModuleAssignment::from_assignments(
            assignments,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        let mut index = RealizabilityIndex::from_assignment(&graph, assignment);
        for step in 0..12usize {
            let first = (step * 5 + seed) % names.len();
            let source_units = if step % 3 == 0 {
                vec![SourceUnitId(first), SourceUnitId((first + 3) % names.len())]
            } else {
                vec![SourceUnitId(first)]
            };
            let to = ModuleId((step + seed * 2) % 5);
            let overlay =
                index
                    .quotient
                    .overlay_for_move(&graph, index.assignment(), &source_units, to);
            let mut post = index.assignment().clone();
            for &source_unit in &source_units {
                post.set(source_unit, to);
            }
            assert_eq!(
                index.quotient.canonical_with_overlay(&overlay),
                chunk_constraining_module_edges(&graph, &post),
                "canonical projection diverged: seed={seed} step={step}",
            );
            assert_eq!(
                index.verdict_after_moving_source_units(&graph, &source_units, to),
                check_realizability(&graph, &post),
                "full verdict diverged: seed={seed} step={step}",
            );
            if step % 2 == 0 {
                index.apply(
                    &graph,
                    ModuleAssignmentDelta::MoveSourceUnits { source_units, to },
                );
            }
        }
    }
}

#[test]
fn large_module_import_neighbors_match_the_shared_emitter_order() {
    use super::esm_simulator::EsmIGraph;
    use crate::esm_import_order::EsmImportOrder;

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
    let graph = EsmIGraph {
        i_successors: &i_successors,
        entry_module_id,
        nodes: &nodes,
        import_order: &import_order,
    };
    let mut emitted_imports: Vec<_> = targets.iter().copied().map(|target| (target, ())).collect();
    import_order.sort_module_imports(&mut emitted_imports);
    let expected: Vec<_> = emitted_imports
        .into_iter()
        .rev()
        .map(|(target, ())| target)
        .collect();
    assert_eq!((&graph).neighbors(importer).collect::<Vec<_>>(), expected);
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
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(0), ModuleId(1));
    assignment.set(SourceUnitId(1), ModuleId(2));
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
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(1), ModuleId(1));
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(
        !verdict.is_realizable(),
        "verdict should report an SCC: {verdict:#?}"
    );
    let modules: BTreeSet<ModuleId> = verdict.modules_in_unrealizable_sccs();
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

/// The touching-filtered reference predicate: an SCC diagnosis is
/// kept only when the queried module participates in it. A module
/// outside every diagnosis sees a realizable verdict even though the
/// full verdict is unrealizable.
#[test]
fn touching_filter_keeps_only_diagnoses_involving_the_queried_module() {
    // Mutual eager cycle between mod 1 and mod 2; source unit 2 (`const c`)
    // is an unrelated clean module 3.
    let source = "const a = b + 1; const b = a + 1; const c = 1;";
    let dependency_graph = parse_and_build(source);
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(0), ModuleId(1));
    assignment.set(SourceUnitId(1), ModuleId(2));
    assignment.set(SourceUnitId(2), ModuleId(3));
    assert!(!check_realizability(&dependency_graph, &assignment).is_realizable());
    let touching_cycle = check_realizability_touching(&dependency_graph, &assignment, ModuleId(1));
    assert!(
        !touching_cycle.is_realizable(),
        "module 1 is in the SCC; the diagnosis must survive the filter: {touching_cycle:#?}",
    );
    let touching_clean = check_realizability_touching(&dependency_graph, &assignment, ModuleId(3));
    assert!(
        touching_clean.is_realizable(),
        "module 3 touches no diagnosis; pre-existing violations \
         elsewhere must not surface: {touching_clean:#?}",
    );
}

/// Clause-2 side of the touching filter: a cross-module rebind is
/// kept iff the queried module is one of its endpoints.
#[test]
fn touching_filter_keeps_only_cross_rebinds_at_the_queried_module() {
    // source_unit_0: let a = 1 (residual). source_unit_1: a = 2 (mod 1 — a
    // cross-module top-level rebinding write). source_unit_2: const z
    // (mod 2, unrelated).
    let source = "let a = 1; a = 2; const z = 3;";
    let dependency_graph = parse_and_build(source);
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(1), ModuleId(1));
    assignment.set(SourceUnitId(2), ModuleId(2));
    let full = check_realizability(&dependency_graph, &assignment);
    assert!(
        !full.cross_rebinds.is_empty(),
        "fixture must produce a cross-module rebind: {full:#?}",
    );
    let touching_writer = check_realizability_touching(&dependency_graph, &assignment, ModuleId(1));
    assert!(
        !touching_writer.cross_rebinds.is_empty(),
        "module 1 is the rebind's writer side: {touching_writer:#?}",
    );
    let touching_clean = check_realizability_touching(&dependency_graph, &assignment, ModuleId(2));
    assert!(
        touching_clean.is_realizable(),
        "module 2 is on neither rebind endpoint: {touching_clean:#?}",
    );
}

/// A pure lazy-read cycle (mutual references inside function
/// bodies) is realizable: ESM evaluates the lazy side first, no
/// TDZ. Verdict must be empty even when the modules form a cycle
/// in the *full* quotient.
#[test]
fn pure_lazy_cycle_is_realizable() {
    let source = "function a() { return b(); } function b() { return a(); }";
    let dependency_graph = parse_and_build(source);
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(1), ModuleId(1));
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
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(0), ModuleId(1)); // sub1 → mod_sub1
    assignment.set(SourceUnitId(1), ModuleId(2)); // sub2 → mod_sub2
    assignment.set(SourceUnitId(2), ModuleId(3)); // ids  → mod_ids
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
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(1), ModuleId(1)); // readSeed → mod_helpers
    assignment.set(SourceUnitId(2), ModuleId(2)); // sub1 → mod_sub1
    assignment.set(SourceUnitId(3), ModuleId(3)); // ids → mod_ids
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
    let assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    let verdict = check_realizability(&dependency_graph, &assignment);
    assert!(verdict.is_realizable());
}

/// One speculative single-source unit move per case: the overlay verdict must equal
/// the committed and pure verdicts, have the stated shape, and leave the
/// working index untouched.
#[test]
fn move_overlay_matches_applied_verdict_on_cycle_rebind_and_chain_moves() {
    // (label, source, base assignments, moved source unit, target module,
    //  unrealizable SCCs, cross rebinds)
    let cases = [
        // Moving `b` out of residual closes the mutual eager `a`/`b` cycle.
        (
            "cycle",
            "const a = b + 1; const b = a + 1;",
            &[][..],
            1,
            1,
            1,
            0,
        ),
        // The writer leaves the binding it rebinds: a direct violation, not
        // an SCC edge.
        (
            "rebind",
            "let a = 0; function b() { a = 1; }",
            &[][..],
            1,
            1,
            0,
            1,
        ),
        // `b` leaves the initialized chain `a -> b -> c` for a fresh module, so its
        // current edges are masked.
        (
            "chain",
            "const c = 1; const b = c + 1; const a = b + 1;",
            &[(0, 3), (1, 2), (2, 1)][..],
            1,
            4,
            0,
            0,
        ),
    ];
    for (label, source, base, source_unit, to, sccs, rebinds) in cases {
        let dependency_graph = parse_and_build(source);
        let index = RealizabilityIndex::from_assignment(
            &dependency_graph,
            assignment_with(&dependency_graph, base),
        );
        let before = normalize_verdict(index.verdict());
        let home = index.assignment().of(SourceUnitId(source_unit));
        let (source_units, to) = ([SourceUnitId(source_unit)], ModuleId(to));

        assert_move_agrees_across_paths(&index, &dependency_graph, &source_units, to);

        let overlay =
            index.verdict_after_moving_source_units_touching(&dependency_graph, &source_units, to);
        assert_eq!(
            (overlay.unrealizable_sccs.len(), overlay.cross_rebinds.len()),
            (sccs, rebinds),
            "{label}: {overlay:#?}",
        );
        assert_eq!(
            normalize_verdict(index.verdict()),
            before,
            "{label}: overlay query must not mutate the working assignment",
        );
        assert_eq!(
            index.assignment().of(SourceUnitId(source_unit)),
            home,
            "{label}"
        );
    }
}

/// After each committed move the index's verdict equals the pure
/// from-scratch verdict on the same assignment, has the stated shape, and its
/// touching verdict for the target module is the full verdict filtered to it.
#[test]
fn incremental_index_matches_pure_verdict_after_each_committed_move() {
    // Per row: (label, source, moves), each move being (source unit, target module,
    // realizable, unrealizable SCCs, cross rebinds).
    let rows = [
        // The last move rejoins the `a`/`b` cycle's two modules, removing
        // edges internal to a multi-module SCC.
        (
            "sequential moves",
            "const a = b + 1; const b = a + 1; function c() { return a; }",
            vec![(1, 1, false, 1, 0), (2, 2, false, 1, 0), (1, 0, true, 0, 0)],
        ),
        // Rebinds are direct violations, not SCC edges.
        (
            "cross rebind",
            "let a = 0; function b() { a = 1; }",
            vec![(1, 1, false, 0, 1)],
        ),
    ];
    for (label, source, moves) in rows {
        let dependency_graph = parse_and_build(source);
        let baseline = ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        let mut explicit = baseline.clone();
        let mut index = RealizabilityIndex::from_assignment(&dependency_graph, baseline);
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&dependency_graph, &explicit)),
            "{label}: before any move",
        );
        for (source_unit, to, realizable, sccs, rebinds) in moves {
            index.apply(
                &dependency_graph,
                ModuleAssignmentDelta::MoveSourceUnits {
                    source_units: vec![SourceUnitId(source_unit)],
                    to: ModuleId(to),
                },
            );
            explicit.set(SourceUnitId(source_unit), ModuleId(to));
            let verdict = index.verdict();
            assert_eq!(
                normalize_verdict(verdict.clone()),
                normalize_verdict(check_realizability(&dependency_graph, &explicit)),
                "{label}: after moving source unit {source_unit} to module {to}",
            );
            assert_eq!(
                (
                    verdict.is_realizable(),
                    verdict.unrealizable_sccs.len(),
                    verdict.cross_rebinds.len(),
                ),
                (realizable, sccs, rebinds),
                "{label}: after moving source unit {source_unit} to module {to}: {verdict:#?}",
            );
            assert_eq!(
                normalize_verdict(index.verdict_touching(ModuleId(to))),
                normalize_verdict(filter_verdict_touching(&verdict, ModuleId(to))),
                "{label}: touching verdict after moving source unit {source_unit} to module {to}",
            );
        }
    }
}

#[test]
fn empty_delta_overlay_scc_containing_is_the_base_scc() {
    let mut graph = CountedDiGraph::new();
    for (from, to) in [(1, 2), (2, 3), (3, 1), (3, 4), (4, 5)] {
        graph.increment_edge(ModuleId(from), ModuleId(to));
    }
    let no_delta = BTreeMap::new();
    let view = OverlayGraphView::new(&graph, &no_delta);

    assert_eq!(
        view.scc_containing(ModuleId(2)),
        BTreeSet::from([ModuleId(1), ModuleId(2), ModuleId(3)]),
    );
    assert_eq!(
        view.scc_containing(ModuleId(4)),
        BTreeSet::from([ModuleId(4)]),
        "4 is reachable from the cycle but cannot reach it",
    );
}

#[test]
fn overlay_scc_matches_effective_graph_with_counted_additions_and_removals() {
    let mut base = CountedDiGraph::new();
    for (from, to) in [
        (1, 2),
        (1, 2), // One of two parallel edges remains after the overlay.
        (2, 1),
        (3, 4),
        (4, 5),
        (6, 7),
    ] {
        base.increment_edge(ModuleId(from), ModuleId(to));
    }
    let delta = BTreeMap::from([
        ((ModuleId(1), ModuleId(2)), -1),
        ((ModuleId(2), ModuleId(1)), -1),
        ((ModuleId(3), ModuleId(4)), 1), // Existing adjacency with another edge.
        ((ModuleId(5), ModuleId(3)), 1), // Closes a new three-node cycle.
        ((ModuleId(6), ModuleId(7)), -1),
    ]);
    let view = OverlayGraphView::new(&base, &delta);

    // Independently materialize the effective counted graph. This checks the
    // overlay's zero-count filtering and positive additions without relying on
    // the optimized reachability implementation.
    let mut pairs: BTreeSet<_> = base.edge_pairs().collect();
    pairs.extend(delta.keys().copied());
    let mut effective = CountedDiGraph::new();
    for (from, to) in pairs {
        let count = base.edge_count(from, to) as isize
            + delta.get(&(from, to)).copied().unwrap_or_default();
        for _ in 0..count.max(0) {
            effective.increment_edge(from, to);
        }
    }

    for index in 1..=7 {
        let node = ModuleId(index);
        let expected = effective
            .all_sccs()
            .into_iter()
            .find(|scc| scc.contains(&node))
            .unwrap_or_else(|| BTreeSet::from([node]));
        assert_eq!(view.scc_containing(node), expected, "module {index}");
    }
}

#[test]
fn overlay_bucket_emptiness_matches_materialized_added_and_removed_edges() {
    let mut base = ConstrainingBucket::default();
    base.insert_edge(analysis::graph::DependencyEdgeId(1), false);
    base.insert_edge(analysis::graph::DependencyEdgeId(2), true);
    let mut removed = ConstrainingBucket::default();
    removed.insert_edge(analysis::graph::DependencyEdgeId(1), false);
    let mut added = ConstrainingBucket::default();
    added.insert_edge(analysis::graph::DependencyEdgeId(3), true);

    let mut effective = base.clone();
    effective.remove_from(&removed);
    effective.extend_from(&added);
    assert_eq!(
        constraining_bucket_is_empty_after_overlay(Some(&base), Some(&removed), Some(&added)),
        effective.is_empty(),
    );

    removed.insert_edge(analysis::graph::DependencyEdgeId(2), true);
    let mut no_edges = base.clone();
    no_edges.remove_from(&removed);
    assert_eq!(
        constraining_bucket_is_empty_after_overlay(Some(&base), Some(&removed), None),
        no_edges.is_empty(),
    );

    assert!(!constraining_bucket_is_empty_after_overlay(
        None,
        None,
        Some(&added),
    ));
}

type NormalizedVerdict = (
    BTreeSet<(Vec<ModuleId>, Vec<usize>)>,
    BTreeSet<(ModuleId, ModuleId, usize)>,
);

fn normalize_verdict(verdict: RealizabilityVerdict) -> NormalizedVerdict {
    let sccs = verdict
        .unrealizable_sccs
        .into_iter()
        .map(|scc| {
            let modules: Vec<ModuleId> = scc.core.modules.into_iter().collect();
            let edges: Vec<usize> = scc
                .core
                .constraining_dependency_edges
                .into_iter()
                .map(|edge| edge.0)
                .collect();
            (modules, edges)
        })
        .collect();
    let rebinds = verdict
        .cross_rebinds
        .into_iter()
        .map(|rebind| (rebind.from, rebind.to, rebind.dependency_edge.0))
        .collect();
    (sccs, rebinds)
}

fn filter_verdict_touching(
    verdict: &RealizabilityVerdict,
    module: ModuleId,
) -> RealizabilityVerdict {
    RealizabilityVerdict {
        unrealizable_sccs: verdict
            .unrealizable_sccs
            .iter()
            .filter(|scc| scc.core.modules.contains(&module))
            .cloned()
            .collect(),
        cross_rebinds: verdict
            .cross_rebinds
            .iter()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .cloned()
            .collect(),
    }
}

/// Reach inside the `RealizabilityIndex` to assert that the
/// `IncrementalQuotient`'s cached base simulator (when populated)
/// matches a from-scratch `EsmEvaluationSimulator::build` against
/// the live `i_graph` + `constraining_buckets`. Lives in the
/// realizability.rs `mod tests` so it can name the private types
/// (`IncrementalQuotient`, `EsmEvaluationSimulator`).
fn assert_cached_simulator_matches_rebuild(index: &RealizabilityIndex, label: &str, phase: &str) {
    let quotient = &index.quotient;
    // Materialize the same inputs `EsmEvaluationSimulator::build`
    // would have walked from scratch, bypassing the cache so a
    // bug in the cached-input path can't mask a divergence here.
    let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    for (from, to) in quotient.i_graph.edge_pairs() {
        i_successors.entry(from).or_default().insert(to);
    }
    let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
        quotient.constraining_buckets.keys().copied().collect();
    let rebuilt =
        EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, quotient.entry_module_id);
    // Force the cache to populate (verdict() takes the base path).
    let cached = quotient.base_simulator().clone();
    assert_eq!(
        cached, rebuilt,
        "{label}: cached base simulator diverges from rebuild ({phase})",
    );

    // Property: the cached `(i_successors, constraining_pairs)`
    // inputs must match the from-scratch walk too. This pins the
    // overlay path's clone-and-patch correctness — overlay queries
    // mutate these cached snapshots, and a mismatched base would
    // taint every overlay query.
    let (cached_inputs_succs, cached_inputs_pairs) = quotient.effective_simulator_inputs(None);
    let mut fresh_succs: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    for (from, to) in quotient.i_graph.edge_pairs() {
        fresh_succs.entry(from).or_default().insert(to);
    }
    let fresh_pairs: BTreeSet<(ModuleId, ModuleId)> =
        quotient.constraining_buckets.keys().copied().collect();
    assert_eq!(
        cached_inputs_succs, fresh_succs,
        "{label}: cached base i_successors diverges from rebuild ({phase})",
    );
    assert_eq!(
        cached_inputs_pairs, fresh_pairs,
        "{label}: cached base constraining pairs diverges from rebuild ({phase})",
    );
}

/// Property test pinning the incremental simulator cache to its
/// from-scratch correctness reference. For each fixture, applies
/// an arbitrary sequence of `MoveSourceUnits` deltas through the
/// `RealizabilityIndex`, asserting after every apply that the
/// `IncrementalQuotient`'s cached `EsmEvaluationSimulator`
/// byte-equals what `EsmEvaluationSimulator::build(...)` would
/// produce against the current `i_graph` / `constraining_buckets`.
/// Also asserts the cached `(i_successors, constraining_pairs)`
/// snapshots match.
///
/// `add_current_edge` / `remove_current_edge` each drop the cache, so
/// a stale simulator never outlives an edge mutation.
#[test]
fn incremental_simulator_matches_rebuild_after_each_delta() {
    struct Fixture {
        label: &'static str,
        source: &'static str,
        deltas: Vec<(Vec<usize>, usize)>,
    }
    let fixtures = vec![
        // Two-cycle plus a lazy bystander.
        Fixture {
            label: "two_eager_plus_lazy",
            source: "const a = b + 1; const b = a + 1; function c() { return a; }",
            deltas: vec![(vec![1], 1), (vec![2], 2), (vec![1, 2], 3)],
        },
        // Asymmetric I-cycle with a residual mediator.
        Fixture {
            label: "asymmetric_with_mediator",
            source: "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; \
                     function lazy_reader() { return cross_value; } \
                     function mediator_helper() { return dep_value + lazy_reader(); } \
                     const mediator_init = mediator_helper(); console.log(mediator_init);",
            deltas: vec![(vec![0, 2], 1), (vec![1], 2), (vec![3, 4], 3)],
        },
        // Cross-destination rebind — exercises the rebind-only
        // overlay code path (no simulator change).
        Fixture {
            label: "rebind_then_unmove",
            source: "let a = 0; function b() { a = 1; }",
            deltas: vec![(vec![1], 1), (vec![1], 0)],
        },
        // Single-module fixture (no cross-module edges → simulator
        // input set stays empty across all deltas).
        Fixture {
            label: "single_module",
            source: "const a = 1; const b = a + 1; const c = a * b;",
            deltas: vec![(vec![1], 1), (vec![2], 1), (vec![1, 2], 0)],
        },
    ];
    for fixture in fixtures {
        let dependency_graph = parse_and_build(fixture.source);
        let baseline = ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        );
        let mut index = RealizabilityIndex::from_assignment(&dependency_graph, baseline);
        assert_cached_simulator_matches_rebuild(&index, fixture.label, "initial");
        for (source_unit_indices, dest) in &fixture.deltas {
            let source_units: Vec<SourceUnitId> = source_unit_indices
                .iter()
                .copied()
                .map(SourceUnitId)
                .collect();
            index.apply(
                &dependency_graph,
                ModuleAssignmentDelta::MoveSourceUnits {
                    source_units,
                    to: ModuleId(*dest),
                },
            );
            assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-apply");
            // verdict() pulls through the cached simulator; assert
            // it stays consistent with `check_realizability`.
            let projected = index.assignment().clone();
            assert_eq!(
                normalize_verdict(index.verdict()),
                normalize_verdict(check_realizability(&dependency_graph, &projected)),
                "{}: verdict diverged from check_realizability after apply",
                fixture.label,
            );
        }
    }
}

// ---------------------------------------------------------------------
// Gate-ladder tests: the tier-laddered boolean must equal the
// evidence-producing overlay verdict on every move query, with each
// fixture pinning the tier expected to decide it.
// ---------------------------------------------------------------------

/// Assert the ladder, the boolean wrapper, and the overlay verdict
/// agree for one move query; return the decision for tier pinning.
fn assert_ladder_matches_verdict(
    index: &RealizabilityIndex,
    dependency_graph: &DependencyGraph,
    source_units: &[SourceUnitId],
    to: ModuleId,
) -> LadderDecision {
    let decision = index.ladder_decision_after_moving_source_units_touching(
        dependency_graph,
        source_units,
        to,
    );
    let verdict =
        index.verdict_after_moving_source_units_touching(dependency_graph, source_units, to);
    assert_eq!(
        decision.accepts(),
        verdict.is_realizable(),
        "ladder {decision:?} diverges from the overlay verdict for move \
         {source_units:?} → {to:?}: {verdict:#?}",
    );
    assert_eq!(
        decision.accepts(),
        index.would_remain_realizable_after_moving_source_units_touching(
            dependency_graph,
            source_units,
            to
        ),
    );
    decision
}

#[test]
fn ladder_tier0_delta_free_move_accepts_on_clean_state() {
    let source = "const a = 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    // SourceUnit 1 already lives in module 0 — the move is delta-free.
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(0));
    assert_eq!(decision, LadderDecision::DeltaFreeAccept);
}

#[test]
fn ladder_tier0_delta_free_move_rejects_on_dirty_pre_state() {
    // Mutual constraining cycle committed between modules 1 and 2; a
    // delta-free move touching module 1 must reject (post == pre, and
    // the pre-state touching verdict is dirty).
    let source = "const a = b + 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(0), ModuleId(1));
    assignment.set(SourceUnitId(1), ModuleId(2));
    let index = RealizabilityIndex::from_assignment(&dependency_graph, assignment);
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(0)], ModuleId(1));
    assert_eq!(decision, LadderDecision::DeltaFreeReject);
}

#[test]
fn ladder_tier1_rejects_constraining_cycle_move() {
    // Moving `b` out of residual closes the mutual eager 2-cycle —
    // a Pass-1 reject the constraining condensation decides.
    let source = "const a = b + 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(1));
    assert_eq!(decision, LadderDecision::ConstrainingCycleReject);
}

#[test]
fn ladder_tier1_rejects_cross_rebind_move() {
    // Moving the writer out of residual turns the intra-module rebind
    // into a clause-2 cross-module rebinding write.
    let source = "let a = 0; function b() { a = 1; }";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(1));
    assert_eq!(decision, LadderDecision::CrossRebindReject);
}

#[test]
fn ladder_tier2_accepts_acyclic_cross_module_move() {
    // Move the dependency out of entry, not its dependent. The resulting
    // entry-to-module edge is valid; the reverse is an entry TDZ. The
    // I-condensation proves Pass 2 vacuous without a simulator build.
    let source = "const a = 1; const b = a + 1;";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(0)], ModuleId(1));
    assert_eq!(decision, LadderDecision::NoMultiModuleISccAccept);
}

#[test]
fn ladder_tier2_accepts_pure_lazy_cycle_move() {
    // The move closes a pure-lazy I-cycle: multi-module I-SCC with no
    // constraining pair inside — Lemma 2 says it never TDZs.
    let source = "function a() { return b(); } function b() { return a(); }";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(1));
    assert_eq!(decision, LadderDecision::NoConstrainingPairAccept);
}

#[test]
fn ladder_tier3_accepts_lemma_two_rescued_move() {
    // The asymmetric cycle of `e2e/lemma_two_rescued_asymmetric_cycle_test`
    // reached via a speculative move: dep_value + lazy_reader sit in mod 1; moving
    // cross_value to mod 2 closes the asymmetric I-SCC {1, 2} with a
    // constraining pair, so the ladder must run the simulator — which
    // rescues (Lemma 2).
    let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } console.log(dep_value, cross_value, lazy_reader());";
    let dependency_graph = parse_and_build(source);
    let mut assignment = ModuleAssignment::new(
        &dependency_graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    assignment.set(SourceUnitId(0), ModuleId(1));
    assignment.set(SourceUnitId(2), ModuleId(1));
    let index = RealizabilityIndex::from_assignment(&dependency_graph, assignment);
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(2));
    assert_eq!(decision, LadderDecision::SimulatorAccept);
}

#[test]
fn ladder_rejects_entry_tdz_before_simulation() {
    // Asymmetric I-SCC with the constraining edge pointing INTO
    // residual (reached via a move): residual is the DFS root and
    // evaluates last, so the moved statement's eager read of `seed`
    // TDZs. Pass 1 is clean (one constraining direction) and the
    // entry-last check rejects before tier 2 can skip the simulator.
    let source = "const seed = 1; const x = seed + 1; function readX() { return x; }";
    let dependency_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let decision =
        assert_ladder_matches_verdict(&index, &dependency_graph, &[SourceUnitId(1)], ModuleId(1));
    assert_eq!(decision, LadderDecision::EntryDependencyReject);
}

/// Condensation-order maintenance: the ladder stays equal to the
/// overlay verdict across committed moves (incremental edge
/// insert/remove), including one that removes edges internal to a
/// multi-module SCC (stale + lazy rebuild).
#[test]
fn ladder_matches_verdict_across_applied_moves() {
    let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
    let dependency_graph = parse_and_build(source);
    let mut index = RealizabilityIndex::from_assignment(
        &dependency_graph,
        ModuleAssignment::new(
            &dependency_graph,
            analysis::EntryDestination::from_module_id(ModuleId(0)),
        ),
    );
    let sweep = |index: &RealizabilityIndex| {
        for source_unit in 0..dependency_graph.num_nodes() {
            for module in 0..4 {
                assert_ladder_matches_verdict(
                    index,
                    &dependency_graph,
                    &[SourceUnitId(source_unit)],
                    ModuleId(module),
                );
            }
        }
    };
    sweep(&index);
    for (source_unit, to) in [(1, 1), (2, 2), (1, 0)] {
        index.apply(
            &dependency_graph,
            ModuleAssignmentDelta::MoveSourceUnits {
                source_units: vec![SourceUnitId(source_unit)],
                to: ModuleId(to),
            },
        );
        sweep(&index);
    }
}

// ---------------------------------------------------------------------
// Move overlay vs committed path vs pure reference, over dependency graphs
// that carry promoted-at-init edges.
// ---------------------------------------------------------------------

/// One speculative move must get the same verdict from all three
/// routes to the gate: the overlay (the production speculative path,
/// both the evidence-producing verdict and the tier ladder), the
/// committed path (`apply` on a copy, then `verdict_touching`), and the
/// pure from-scratch reference on the post-move assignment.
fn assert_move_agrees_across_paths(
    index: &RealizabilityIndex,
    dependency_graph: &DependencyGraph,
    source_units: &[SourceUnitId],
    to: ModuleId,
) {
    let base: Vec<usize> = index.assignment().iter().map(|(_, m)| m.0).collect();
    let overlay =
        index.verdict_after_moving_source_units_touching(dependency_graph, source_units, to);
    let ladder = index.ladder_decision_after_moving_source_units_touching(
        dependency_graph,
        source_units,
        to,
    );
    let mut post = index.assignment().clone();
    for &source_unit in source_units {
        post.set(source_unit, to);
    }
    let pure = check_realizability_touching(dependency_graph, &post, to);
    let mut moved = index.clone();
    moved.apply(
        dependency_graph,
        ModuleAssignmentDelta::MoveSourceUnits {
            source_units: source_units.to_vec(),
            to,
        },
    );
    let committed = moved.verdict_touching(to);
    assert_eq!(
        ladder.accepts(),
        pure.is_realizable(),
        "ladder {ladder:?} vs pure {pure:#?}: move {source_units:?} -> {to:?} from {base:?}",
    );
    let pure = normalize_verdict(pure);
    assert_eq!(
        normalize_verdict(committed),
        pure,
        "committed path vs pure: move {source_units:?} -> {to:?} from {base:?}",
    );
    assert_eq!(
        normalize_verdict(overlay),
        pure,
        "overlay vs pure: move {source_units:?} -> {to:?} from {base:?}",
    );
}

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

/// Every single- and pair-source unit move from every assignment of the
/// source units to three modules (module 0 is residual; the targets include
/// a fresh module) must agree across the overlay, the committed path
/// and the pure reference. The first two sources carry
/// The first and third sources have resolved promoted edges; the
/// second retains a fallback-promoted edge (`callee_source_unit == from`).
#[test]
fn move_overlay_matches_committed_and_pure_on_promoted_edge_graphs() {
    let sources = [
        // `r = g()` resolves through the local alias to `read_a`, so its
        // promoted edge carries that function source unit. The overlay must still
        // agree with the committed and reference gates after every move.
        (
            "const a = 1; function read_a() { return a; } const g = read_a; const r = g(); const m = r + 1;",
            false,
        ),
        // `r = api.read()` resolves the object's static function property.
        // Its promoted `r -> a` edge retains the lexical source unit of the arrow
        // stored on `api`. Moving `r` must preserve agreement between the
        // overlay, committed graph, and reference gate.
        (
            "const a = 1; const api = { read: () => a }; const r = api.read(); const m = r + 1;",
            false,
        ),
        (
            "const a = 1; const read_a = () => a; const s = { v: read_a() }; const t = s.v + 1; const u = t;",
            false,
        ),
    ];
    for (source, callee_is_caller) in sources {
        let dependency_graph = parse_and_build(source);
        assert!(
            dependency_graph.iter_edges().any(|edge| matches!(
                edge.reason.role(),
                EdgeRole::PromotedAtInit { callee_source_unit }
                    if (callee_source_unit == edge.from) == callee_is_caller
            )),
            "{source}: fixture lost its promoted edge",
        );
        let source_unit_count = dependency_graph.num_nodes();
        let moves: Vec<Vec<SourceUnitId>> = (0..source_unit_count)
            .flat_map(|first| {
                std::iter::once(vec![SourceUnitId(first)]).chain(
                    ((first + 1)..source_unit_count)
                        .map(move |second| vec![SourceUnitId(first), SourceUnitId(second)]),
                )
            })
            .collect();
        for assignment in 0..3usize.pow(source_unit_count as u32) {
            let base: Vec<(usize, usize)> = (0..source_unit_count)
                .map(|source_unit| (source_unit, assignment / 3usize.pow(source_unit as u32) % 3))
                .collect();
            let index = RealizabilityIndex::from_assignment(
                &dependency_graph,
                assignment_with(&dependency_graph, &base),
            );
            for source_units in &moves {
                for to in 0..4 {
                    assert_move_agrees_across_paths(
                        &index,
                        &dependency_graph,
                        source_units,
                        ModuleId(to),
                    );
                }
            }
        }
    }
}

#[test]
fn entry_dependency_is_rejected_by_reference_overlay_and_committed_gates() {
    let graph = parse_and_build("const a = 1; const b = a + 1;");
    let mut assignment = ModuleAssignment::new(
        &graph,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    let mut index = RealizabilityIndex::from_assignment(&graph, assignment.clone());
    let decision = assert_ladder_matches_verdict(&index, &graph, &[SourceUnitId(1)], ModuleId(1));
    assert_eq!(decision, LadderDecision::EntryDependencyReject);
    assignment.set(SourceUnitId(1), ModuleId(1));
    assert!(!check_realizability(&graph, &assignment).is_realizable());
    // Rebuild the committed cache from the same concrete assignment.
    index = RealizabilityIndex::from_assignment(&graph, assignment);
    assert_eq!(
        assert_ladder_matches_verdict(&index, &graph, &[SourceUnitId(1)], ModuleId(1)),
        LadderDecision::DeltaFreeReject
    );
    // Moving the dependency into the same emitted module repairs the violation.
    assert!(
        assert_ladder_matches_verdict(&index, &graph, &[SourceUnitId(0)], ModuleId(1)).accepts()
    );
}
