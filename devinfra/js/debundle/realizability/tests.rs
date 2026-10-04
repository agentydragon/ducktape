use std::collections::BTreeSet;

use super::incremental_quotient::OverlayGraphView;
use super::*;
use crate::counted_digraph::CountedDiGraph;
use analysis::OwnerId;
use analysis::facts::analyze_chunk;
use analysis::graph::{EdgeRole, build_owner_graph_with};
use analysis::ids::{LogicalModuleIndex, ModuleId};
use analysis::partition::Partition;
use analysis::{AnalysisHints, OwnerGraph};

fn module_id(index: usize) -> ModuleId {
    ModuleId(LogicalModuleIndex(index))
}

fn parse_and_build(source: &str) -> OwnerGraph {
    let module = raw_js_test_support::parse(source);
    let facts = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts;
    build_owner_graph_with(&facts, Default::default()).unwrap()
}

/// Two top-level constants in different modules, with one reading
/// the other at-init across the module boundary acyclically. No
/// cycle, no rebind — verdict is empty.
#[test]
fn acyclic_cross_module_at_init_read_is_realizable() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    // Owner 0: const a = 1 → module 1.
    // Owner 1: const b = a + 1 → module 2.
    // Edge owner_1 → owner_0 (eager_use of `a`).
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    let verdict = check_realizability(&owner_graph, &partition);
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
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        !verdict.is_realizable(),
        "verdict should report an SCC: {verdict:#?}"
    );
    let modules: BTreeSet<ModuleId> = verdict.modules_in_unrealizable_sccs();
    assert!(modules.contains(&module_id(0)));
    assert!(modules.contains(&module_id(1)));
    assert!(
        verdict
            .unrealizable_sccs
            .iter()
            .all(|scc| !scc.core.constraining_owner_edges.is_empty()),
        "every SCC must carry owner-edge evidence"
    );
}

/// The touching-filtered reference predicate: an SCC diagnosis is
/// kept only when the queried module participates in it. A module
/// outside every diagnosis sees a realizable verdict even though the
/// full verdict is unrealizable.
#[test]
fn touching_filter_keeps_only_diagnoses_involving_the_queried_module() {
    // Mutual eager cycle between mod 1 and mod 2; owner 2 (`const c`)
    // is an unrelated clean module 3.
    let source = "const a = b + 1; const b = a + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    partition.set(OwnerId(2), module_id(3));
    assert!(!check_realizability(&owner_graph, &partition).is_realizable());
    let touching_cycle = check_realizability_touching(&owner_graph, &partition, module_id(1));
    assert!(
        !touching_cycle.is_realizable(),
        "module 1 is in the SCC; the diagnosis must survive the filter: {touching_cycle:#?}",
    );
    let touching_clean = check_realizability_touching(&owner_graph, &partition, module_id(3));
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
    // owner_0: let a = 1 (residual). owner_1: a = 2 (mod 1 — a
    // cross-module top-level rebinding write). owner_2: const z
    // (mod 2, unrelated).
    let source = "let a = 1; a = 2; const z = 3;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    partition.set(OwnerId(2), module_id(2));
    let full = check_realizability(&owner_graph, &partition);
    assert!(
        !full.cross_rebinds.is_empty(),
        "fixture must produce a cross-module rebind: {full:#?}",
    );
    let touching_writer = check_realizability_touching(&owner_graph, &partition, module_id(1));
    assert!(
        !touching_writer.cross_rebinds.is_empty(),
        "module 1 is the rebind's writer side: {touching_writer:#?}",
    );
    let touching_clean = check_realizability_touching(&owner_graph, &partition, module_id(2));
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
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "lazy-only cycle should be realizable: {verdict:#?}"
    );
}

/// Residual is the source of a constraining edge into the SCC,
/// but the SCC also has a constraining-target-residual edge.
/// Lemma 2 fails: residual is the DFS root and evaluates last in
/// post-order; the SCC member reading residual's binding TDZs.
#[test]
fn constraining_edge_into_residual_inside_scc_is_unrealizable() {
    // owner_0: class Backend { ... } (residual, TDZ-locked target)
    // owner_1: let currentLogger; (mod_logger)
    // owner_2: function setLogger(impl) { currentLogger = impl; ... } (mod_logger)
    // owner_3: setLogger(new Backend()); (mod_logger, at-init reads Backend)
    // owner_4: console.log(currentLogger.tag); (residual, lazy read of currentLogger from mod_logger via re-export)
    let source = "class Backend { constructor() { this.tag = \"B\"; } } let currentLogger; function setLogger(impl) { currentLogger = impl; globalThis.__tag = impl.tag; } setLogger(new Backend()); console.log(currentLogger);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    // Backend (owner 0) stays in residual.
    partition.set(OwnerId(1), module_id(1)); // currentLogger → mod_logger
    partition.set(OwnerId(2), module_id(1)); // setLogger → mod_logger
    partition.set(OwnerId(3), module_id(1)); // setLogger(new Backend()) → mod_logger
    // owner 4 (console.log) stays in residual.
    let verdict = check_realizability(&owner_graph, &partition);
    // mod_logger → residual EagerUse (constraining target = residual)
    // residual → mod_logger LazyUse (re-export / console.log)
    // SCC = {residual, mod_logger}. Constraining edge target = residual.
    // Residual is DFS root; mod_logger body runs first, reads Backend → TDZ.
    assert!(
        !verdict.is_realizable(),
        "constraining edge target=residual must TDZ; verdict: {verdict:#?}",
    );
}

/// Namespace-aggregator split: a module-level `const ids = {...sub1, ...sub2}`
/// gets sub1 and sub2 extracted into separate modules, both INDEPENDENT of
/// residual (pure literal initializers). The split is realizable: ESM evaluates
/// sub1, sub2, then ids, then residual.
#[test]
fn namespace_aggregator_with_pure_subs_is_realizable() {
    // owner_0: const sub1 = { foo: 1 }
    // owner_1: const sub2 = { bar: 2 }
    // owner_2: const ids = {...sub1, ...sub2}
    // owner_3: console.log(ids)
    let source = "const sub1 = { foo: 1 }; const sub2 = { bar: 2 }; const ids = {...sub1, ...sub2}; console.log(ids);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1)); // sub1 → mod_sub1
    partition.set(OwnerId(1), module_id(2)); // sub2 → mod_sub2
    partition.set(OwnerId(2), module_id(3)); // ids  → mod_ids
    let verdict = check_realizability(&owner_graph, &partition);
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
    // owner_0: const seed = "S"                  (residual)
    // owner_1: const readSeed = () => seed       (mod_helpers)
    // owner_2: const sub1 = { foo: readSeed() }  (mod_sub1) — at-init call into mod_helpers
    // owner_3: const ids = sub1.foo + "x"        (mod_ids)
    // owner_4: const consumed = ids              (residual)
    let source = "const seed = \"S\"; const readSeed = () => seed; const sub1 = { foo: readSeed() }; const ids = sub1.foo + \"x\"; const consumed = ids; console.log(consumed);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1)); // readSeed → mod_helpers
    partition.set(OwnerId(2), module_id(2)); // sub1 → mod_sub1
    partition.set(OwnerId(3), module_id(3)); // ids → mod_ids
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        !verdict.is_realizable(),
        "promoted-edge aggregator cycle must be flagged by the gate \
         (mod_sub1's readSeed() at-init call reads `seed` in residual; \
         residual reads `ids` in mod_ids; mod_ids reads `sub1` in \
         mod_sub1 — closes a cycle through the promoted edge); \
         verdict: {verdict:#?}",
    );
}

/// All owners in the same module → no cross-destination edges of
/// any kind → empty verdict.
#[test]
fn single_module_is_always_realizable() {
    let source = "const a = 1; const b = a + 1; const c = a * b;";
    let owner_graph = parse_and_build(source);
    let partition = Partition::new(&owner_graph, module_id(0));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(verdict.is_realizable());
}

/// Verdict touching `to` after committing the move on a copy of
/// `index`: the incrementally maintained reference for the overlay
/// query, which must agree with it without mutating the index.
fn verdict_touching_after_applying_move(
    index: &RealizabilityIndex,
    owner_graph: &OwnerGraph,
    owners: &[OwnerId],
    to: ModuleId,
) -> RealizabilityVerdict {
    let mut moved = index.clone();
    moved.apply(
        owner_graph,
        PartitionDelta::MoveOwners {
            owners: owners.to_vec(),
            to,
        },
    );
    moved.verdict_touching(to)
}

#[test]
fn move_overlay_matches_applied_verdict_touching() {
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());
    let before = normalize_verdict(index.verdict());

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
    let applied =
        verdict_touching_after_applying_move(&index, &owner_graph, &[OwnerId(1)], module_id(1));

    assert_eq!(normalize_verdict(overlay), normalize_verdict(applied));
    assert_eq!(
        normalize_verdict(index.verdict()),
        before,
        "overlay query must not mutate the working partition",
    );
    assert_eq!(index.partition().of(OwnerId(1)), baseline.of(OwnerId(1)));
}

#[test]
fn move_overlay_reports_cross_rebinds_like_applied_verdict() {
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
    let applied =
        verdict_touching_after_applying_move(&index, &owner_graph, &[OwnerId(1)], module_id(1));

    assert_eq!(
        normalize_verdict(overlay.clone()),
        normalize_verdict(applied)
    );
    assert!(overlay.unrealizable_sccs.is_empty());
    assert_eq!(overlay.cross_rebinds.len(), 1);
}

#[test]
fn move_overlay_masks_removed_current_edges() {
    let source = "const a = b + 1; const b = c + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let mut baseline = Partition::new(&owner_graph, module_id(0));
    baseline.set(OwnerId(0), module_id(1));
    baseline.set(OwnerId(1), module_id(2));
    baseline.set(OwnerId(2), module_id(3));
    let mut explicit = baseline.clone();
    explicit.set(OwnerId(1), module_id(4));
    let index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(4));
    let applied =
        verdict_touching_after_applying_move(&index, &owner_graph, &[OwnerId(1)], module_id(4));
    let pure = filter_verdict_touching(&check_realizability(&owner_graph, &explicit), module_id(4));

    assert_eq!(
        normalize_verdict(overlay.clone()),
        normalize_verdict(applied)
    );
    assert_eq!(normalize_verdict(overlay), normalize_verdict(pure));
}

/// Each committed move leaves the index's verdict equal to the pure
/// from-scratch verdict on the same partition. The last move rejoins
/// the `a`/`b` cycle's two modules, removing edges internal to a
/// multi-module SCC.
#[test]
fn incremental_index_matches_pure_verdict_through_sequential_moves() {
    let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
    let owner_graph = parse_and_build(source);

    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut explicit = baseline.clone();
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );

    for (owner, to, realizable) in [(1, 1, false), (2, 2, false), (1, 0, true)] {
        index.apply(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(owner)],
                to: module_id(to),
            },
        );
        explicit.set(OwnerId(owner), module_id(to));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
            "after moving owner {owner} to module {to}",
        );
        assert_eq!(index.verdict().is_realizable(), realizable);
    }
}

#[test]
fn verdict_touching_matches_full_verdict_filtered_to_module() {
    let source = "const a = b + 1; const b = a + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
    index.apply(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    index.apply(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(2)],
            to: module_id(2),
        },
    );

    let full = index.verdict();
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(1))),
        normalize_verdict(filter_verdict_touching(&full, module_id(1))),
    );
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(2))),
        normalize_verdict(filter_verdict_touching(&full, module_id(2))),
        "unrelated module should not inherit the a/b SCC",
    );
}

#[test]
fn empty_delta_overlay_scc_containing_is_the_base_scc() {
    let mut graph = CountedDiGraph::new();
    for (from, to) in [(1, 2), (2, 3), (3, 1), (3, 4), (4, 5)] {
        graph.increment_edge(module_id(from), module_id(to));
    }
    let no_delta = BTreeMap::new();
    let view = OverlayGraphView::new(&graph, &no_delta);

    assert_eq!(
        view.scc_containing(module_id(2)),
        BTreeSet::from([module_id(1), module_id(2), module_id(3)]),
    );
    assert_eq!(
        view.scc_containing(module_id(4)),
        BTreeSet::from([module_id(4)]),
        "4 is reachable from the cycle but cannot reach it",
    );
}

#[test]
fn incremental_index_reports_cross_rebinds_without_scc_edges() {
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut explicit = baseline.clone();
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    index.apply(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    explicit.set(OwnerId(1), module_id(1));

    let verdict = index.verdict();
    assert_eq!(
        normalize_verdict(verdict.clone()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );
    assert!(
        verdict.unrealizable_sccs.is_empty(),
        "rebinds are direct violations, not SCC edges: {verdict:#?}",
    );
    assert_eq!(verdict.cross_rebinds.len(), 1);
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(1))),
        normalize_verdict(verdict),
    );
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
                .constraining_owner_edges
                .into_iter()
                .map(|edge| edge.0)
                .collect();
            (modules, edges)
        })
        .collect();
    let rebinds = verdict
        .cross_rebinds
        .into_iter()
        .map(|rebind| (rebind.from, rebind.to, rebind.owner_edge.0))
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
        EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, quotient.residual);
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
/// an arbitrary sequence of `MoveOwners` deltas through the
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
        let owner_graph = parse_and_build(fixture.source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
        assert_cached_simulator_matches_rebuild(&index, fixture.label, "initial");
        for (owner_indices, dest) in &fixture.deltas {
            let owners: Vec<OwnerId> = owner_indices.iter().copied().map(OwnerId).collect();
            index.apply(
                &owner_graph,
                PartitionDelta::MoveOwners {
                    owners,
                    to: module_id(*dest),
                },
            );
            assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-apply");
            // verdict() pulls through the cached simulator; assert
            // it stays consistent with `check_realizability`.
            let projected = index.partition().clone();
            assert_eq!(
                normalize_verdict(index.verdict()),
                normalize_verdict(check_realizability(&owner_graph, &projected)),
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
    owner_graph: &OwnerGraph,
    owners: &[OwnerId],
    to: ModuleId,
) -> LadderDecision {
    let decision = index.ladder_decision_after_moving_owners_touching(owner_graph, owners, to);
    let verdict = index.verdict_after_moving_owners_touching(owner_graph, owners, to);
    assert_eq!(
        decision.accepts(),
        verdict.is_realizable(),
        "ladder {decision:?} diverges from the overlay verdict for move \
         {owners:?} → {to:?}: {verdict:#?}",
    );
    assert_eq!(
        decision.accepts(),
        index.would_remain_realizable_after_moving_owners_touching(owner_graph, owners, to),
    );
    decision
}

#[test]
fn ladder_tier0_delta_free_move_accepts_on_clean_state() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    // Owner 1 already lives in module 0 — the move is delta-free.
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(0));
    assert_eq!(decision, LadderDecision::DeltaFreeAccept);
}

#[test]
fn ladder_tier0_delta_free_move_rejects_on_dirty_pre_state() {
    // Mutual constraining cycle committed between modules 1 and 2; a
    // delta-free move touching module 1 must reject (post == pre, and
    // the pre-state touching verdict is dirty).
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    let index = RealizabilityIndex::from_partition(&owner_graph, partition);
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(0)], module_id(1));
    assert_eq!(decision, LadderDecision::DeltaFreeReject);
}

#[test]
fn ladder_tier1_rejects_constraining_cycle_move() {
    // Moving `b` out of residual closes the mutual eager 2-cycle —
    // a Pass-1 reject the constraining condensation decides.
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::ConstrainingCycleReject);
}

#[test]
fn ladder_tier1_rejects_cross_rebind_move() {
    // Moving the writer out of residual turns the intra-module rebind
    // into a clause-2 cross-module rebinding write.
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::CrossRebindReject);
}

#[test]
fn ladder_tier2_accepts_acyclic_cross_module_move() {
    // Move the dependency out of entry, not its dependent. The resulting
    // entry-to-module edge is valid; the reverse is an entry TDZ. The
    // I-condensation proves Pass 2 vacuous without a simulator build.
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(0)], module_id(1));
    assert_eq!(decision, LadderDecision::NoMultiModuleISccAccept);
}

#[test]
fn ladder_tier2_accepts_pure_lazy_cycle_move() {
    // The move closes a pure-lazy I-cycle: multi-module I-SCC with no
    // constraining pair inside — Lemma 2 says it never TDZs.
    let source = "function a() { return b(); } function b() { return a(); }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
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
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(2), module_id(1));
    let index = RealizabilityIndex::from_partition(&owner_graph, partition);
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(2));
    assert_eq!(decision, LadderDecision::SimulatorAccept);
}

#[test]
fn ladder_rejects_entry_tdz_before_simulation() {
    // Asymmetric I-SCC with the constraining edge pointing INTO
    // residual (the `constraining_edge_into_residual_inside_scc`
    // shape, reached via a move): residual is the DFS root and
    // evaluates last, so the moved statement's eager read of `seed`
    // TDZs. Pass 1 is clean (one constraining direction) and the
    // entry-last check rejects before tier 2 can skip the simulator.
    let source = "const seed = 1; const x = seed + 1; function readX() { return x; }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::EntryDependencyReject);
}

/// Condensation-order maintenance: the ladder stays equal to the
/// overlay verdict across committed moves (incremental edge
/// insert/remove), including one that removes edges internal to a
/// multi-module SCC (stale + lazy rebuild).
#[test]
fn ladder_matches_verdict_across_applied_moves() {
    let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
    let owner_graph = parse_and_build(source);
    let mut index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let sweep = |index: &RealizabilityIndex| {
        for owner in 0..owner_graph.num_nodes() {
            for module in 0..4 {
                assert_ladder_matches_verdict(
                    index,
                    &owner_graph,
                    &[OwnerId(owner)],
                    module_id(module),
                );
            }
        }
    };
    sweep(&index);
    for (owner, to) in [(1, 1), (2, 2), (1, 0)] {
        index.apply(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(owner)],
                to: module_id(to),
            },
        );
        sweep(&index);
    }
}

// ---------------------------------------------------------------------
// Move overlay vs committed path vs pure reference, over owner graphs
// that carry promoted-at-init edges.
// ---------------------------------------------------------------------

/// One speculative move must get the same verdict from all three
/// routes to the gate: the overlay (the production speculative path,
/// both the evidence-producing verdict and the tier ladder), the
/// committed path (`apply` on a copy, then `verdict_touching`), and the
/// pure from-scratch reference on the post-move partition.
fn assert_move_agrees_across_paths(
    index: &RealizabilityIndex,
    owner_graph: &OwnerGraph,
    owners: &[OwnerId],
    to: ModuleId,
) {
    let base: Vec<usize> = index.partition().iter().map(|(_, m)| m.0.0).collect();
    let overlay = index.verdict_after_moving_owners_touching(owner_graph, owners, to);
    let ladder = index.ladder_decision_after_moving_owners_touching(owner_graph, owners, to);
    let mut post = index.partition().clone();
    for &owner in owners {
        post.set(owner, to);
    }
    let pure = check_realizability_touching(owner_graph, &post, to);
    let mut moved = index.clone();
    moved.apply(
        owner_graph,
        PartitionDelta::MoveOwners {
            owners: owners.to_vec(),
            to,
        },
    );
    let committed = moved.verdict_touching(to);
    assert_eq!(
        ladder.accepts(),
        pure.is_realizable(),
        "ladder {ladder:?} vs pure {pure:#?}: move {owners:?} -> {to:?} from {base:?}",
    );
    let pure = normalize_verdict(pure);
    assert_eq!(
        normalize_verdict(committed),
        pure,
        "committed path vs pure: move {owners:?} -> {to:?} from {base:?}",
    );
    assert_eq!(
        normalize_verdict(overlay),
        pure,
        "overlay vs pure: move {owners:?} -> {to:?} from {base:?}",
    );
}

fn partition_with(owner_graph: &OwnerGraph, assignments: &[(usize, usize)]) -> Partition {
    let mut partition = Partition::new(owner_graph, module_id(0));
    for &(owner, module) in assignments {
        partition.set(OwnerId(owner), module_id(module));
    }
    partition
}

/// Fallback-promoted edges (`callee_owner == from`, emitted for an
/// at-init call the analysis cannot resolve) drop out of the gate view
/// when the caller is in residual. Moving such an edge's target into a
/// module that reads residual must not add a residual -> module
/// constraining edge: the gate accepts the move, the overlay must too.
#[test]
fn move_overlay_adds_no_edge_for_residual_fallback_edge_when_target_moves() {
    // Owners: 0 a, 1 read_a, 2 g, 3 r, 4 m. `r = g()` is unresolvable
    // (alias), so r -> a is a fallback-promoted edge from residual;
    // m (module 1) reads r (residual).
    let owner_graph = parse_and_build(
        "const a = 1; function read_a() { return a; } const g = read_a; const r = g(); const m = r + 1;",
    );
    let index =
        RealizabilityIndex::from_partition(&owner_graph, partition_with(&owner_graph, &[(4, 1)]));
    assert_move_agrees_across_paths(
        &index,
        &owner_graph,
        &[OwnerId(0), OwnerId(1)],
        module_id(1),
    );
}

/// Same rule, other direction: moving the *caller* of a residual
/// fallback edge out of residual must not remove a residual -> target
/// quotient edge the gate never had, since the removal would cancel a
/// real edge on the same module pair (here the lazy `api -> a` read)
/// and hide the TDZ cycle the move closes.
#[test]
fn move_overlay_removes_no_edge_for_residual_fallback_edge_when_caller_moves() {
    // Owners: 0 a, 1 api, 2 r, 3 m. `r = api.read()` is unresolvable
    // (member call), so r -> a is a fallback-promoted edge from
    // residual; api (residual) reads a (module 1) lazily.
    let owner_graph = parse_and_build(
        "const a = 1; const api = { read: () => a }; const r = api.read(); const m = r + 1;",
    );
    let index =
        RealizabilityIndex::from_partition(&owner_graph, partition_with(&owner_graph, &[(0, 1)]));
    assert_move_agrees_across_paths(
        &index,
        &owner_graph,
        &[OwnerId(2), OwnerId(3)],
        module_id(1),
    );
}

/// Every single- and pair-owner move from every assignment of the
/// owners to three modules (module 0 is residual; the targets include
/// a fresh module) must agree across the overlay, the committed path
/// and the pure reference. The first two sources carry
/// fallback-promoted edges (`callee_owner == from`); the third a
/// promoted edge whose callee is neither endpoint.
#[test]
fn move_overlay_matches_committed_and_pure_on_promoted_edge_graphs() {
    let sources = [
        (
            "const a = 1; function read_a() { return a; } const g = read_a; const r = g(); const m = r + 1;",
            true,
        ),
        (
            "const a = 1; const api = { read: () => a }; const r = api.read(); const m = r + 1;",
            true,
        ),
        (
            "const a = 1; const read_a = () => a; const s = { v: read_a() }; const t = s.v + 1; const u = t;",
            false,
        ),
    ];
    for (source, callee_is_caller) in sources {
        let owner_graph = parse_and_build(source);
        assert!(
            owner_graph.iter_edges().any(|edge| matches!(
                edge.reason.role(),
                EdgeRole::PromotedAtInit { callee_owner }
                    if (callee_owner == edge.from) == callee_is_caller
            )),
            "{source}: fixture lost its promoted edge",
        );
        let owner_count = owner_graph.num_nodes();
        let moves: Vec<Vec<OwnerId>> = (0..owner_count)
            .flat_map(|first| {
                std::iter::once(vec![OwnerId(first)]).chain(
                    ((first + 1)..owner_count)
                        .map(move |second| vec![OwnerId(first), OwnerId(second)]),
                )
            })
            .collect();
        for assignment in 0..3usize.pow(owner_count as u32) {
            let base: Vec<(usize, usize)> = (0..owner_count)
                .map(|owner| (owner, assignment / 3usize.pow(owner as u32) % 3))
                .collect();
            let index = RealizabilityIndex::from_partition(
                &owner_graph,
                partition_with(&owner_graph, &base),
            );
            for owners in &moves {
                for to in 0..4 {
                    assert_move_agrees_across_paths(&index, &owner_graph, owners, module_id(to));
                }
            }
        }
    }
}

#[test]
fn entry_dependency_is_rejected_by_reference_overlay_and_committed_gates() {
    let graph = parse_and_build("const a = 1; const b = a + 1;");
    let mut partition = Partition::new(&graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&graph, partition.clone());
    let decision = assert_ladder_matches_verdict(&index, &graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::EntryDependencyReject);
    partition.set(OwnerId(1), module_id(1));
    assert!(!check_realizability(&graph, &partition).is_realizable());
    // Rebuild the committed cache from the same concrete assignment.
    index = RealizabilityIndex::from_partition(&graph, partition);
    assert_eq!(
        assert_ladder_matches_verdict(&index, &graph, &[OwnerId(1)], module_id(1)),
        LadderDecision::DeltaFreeReject
    );
    // Moving the dependency into the same emitted module repairs the violation.
    assert!(assert_ladder_matches_verdict(&index, &graph, &[OwnerId(0)], module_id(1)).accepts());
}
