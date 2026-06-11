//! Proptest differential suite for [`CondensationOrder`]: random
//! digraphs driven through proptest-generated interleaved sequences of
//! edge insertions / removals / contractions / invalidations with
//! speculative overlay queries, checked **after every operation**
//! against a petgraph `tarjan_scc` brute-force recompute (the shared
//! reference implementations in `condensation_order::test_support`).
//!
//! Complements the pinned-seed xorshift suites in
//! `condensation_order.rs` — those stay as fast fixed-seed
//! regressions; this suite explores fresh cases per run with
//! proptest's shrinking. The case count is bounded for CI (see
//! [`ci_config`]); for a longer local run override it via
//! `bbr test //devinfra/js/debundle:gate_test
//! --test_env=PROPTEST_CASES=2000`.

use std::collections::{BTreeMap, BTreeSet};

use proptest::prelude::*;
use proptest::test_runner::TestCaseError;

use super::CondensationOrder;
use super::condensation_order::test_support::{
    TestAlias, assert_multi_matches_brute, brute_would_join,
};
use crate::rollback_graph::RollbackDiGraph;

/// Node universe size. Small enough that the brute-force recompute
/// after every operation stays cheap; large enough for nontrivial
/// SCC / condensation shapes.
const NODES: usize = 8;

/// One committed mutation against the base graph + maintained order.
#[derive(Debug, Clone, Copy)]
enum Op {
    InsertEdge(usize, usize),
    RemoveEdge(usize, usize),
    Contract { winner: usize, loser: usize },
    Invalidate,
}

/// Speculative overlay entry: `remove` zeroes the base edge's
/// multiplicity (no-op when the edge is absent at query time),
/// otherwise the entry adds one edge.
#[derive(Debug, Clone, Copy)]
struct OverlayEntry {
    from: usize,
    to: usize,
    remove: bool,
}

/// One step: a committed mutation plus a speculative
/// `would_join_multi_scc` query (pair + overlay) differential-checked
/// on top of the mutated state.
#[derive(Debug, Clone)]
struct Step {
    op: Op,
    query: (usize, usize),
    overlay: Vec<OverlayEntry>,
}

fn arb_node() -> impl Strategy<Value = usize> {
    0..NODES
}

fn arb_op() -> impl Strategy<Value = Op> {
    prop_oneof![
        4 => (arb_node(), arb_node()).prop_map(|(a, b)| Op::InsertEdge(a, b)),
        3 => (arb_node(), arb_node()).prop_map(|(a, b)| Op::RemoveEdge(a, b)),
        2 => (arb_node(), arb_node()).prop_map(|(winner, loser)| Op::Contract { winner, loser }),
        1 => Just(Op::Invalidate),
    ]
}

fn arb_overlay_entries() -> impl Strategy<Value = Vec<OverlayEntry>> {
    proptest::collection::vec(
        (arb_node(), arb_node(), any::<bool>()).prop_map(|(from, to, remove)| OverlayEntry {
            from,
            to,
            remove,
        }),
        0..3,
    )
}

fn arb_step() -> impl Strategy<Value = Step> {
    (arb_op(), (arb_node(), arb_node()), arb_overlay_entries())
        .prop_map(|(op, query, overlay)| Step { op, query, overlay })
}

/// Resolve generated overlay entries against the *current* base
/// graph: `remove` entries zero out an existing edge's multiplicity,
/// additions contribute `+1` — the `QuotientOverlay` delta shape.
fn build_overlay(
    base: &RollbackDiGraph<usize>,
    entries: &[OverlayEntry],
) -> BTreeMap<(usize, usize), isize> {
    let mut overlay = BTreeMap::new();
    for entry in entries {
        if entry.from == entry.to {
            continue;
        }
        if entry.remove {
            let count = base.edge_count(entry.from, entry.to);
            if count > 0 {
                overlay.insert((entry.from, entry.to), -(count as isize));
            }
        } else {
            *overlay.entry((entry.from, entry.to)).or_insert(0) += 1;
        }
    }
    overlay
}

/// Bounded case count for CI; a `PROPTEST_CASES` env override still
/// wins for longer local runs (`ProptestConfig::default()` reads it).
fn ci_config(cases: u32) -> ProptestConfig {
    let mut config = ProptestConfig::default();
    if std::env::var_os("PROPTEST_CASES").is_none() {
        config.cases = cases;
    }
    config
}

proptest! {
    #![proptest_config(ci_config(64))]

    /// After every committed operation: the internal invariants hold
    /// (`validate` — rank/inverse agreement, the topological rank
    /// order over the condensation, module-count bookkeeping),
    /// per-node multi-SCC membership matches a fresh tarjan
    /// recompute, and a speculative overlay query matches the
    /// brute-force identified-graph reference.
    #[test]
    fn mutation_sequences_match_brute_force(
        steps in proptest::collection::vec(arb_step(), 1..50),
    ) {
        let universe: BTreeSet<usize> = (0..NODES).collect();
        let mut base = RollbackDiGraph::new();
        let mut alias = TestAlias::default();
        let mut order = CondensationOrder::new();
        for (step_index, step) in steps.iter().enumerate() {
            match step.op {
                Op::InsertEdge(a, b) => {
                    if a != b {
                        base.increment_edge(a, b);
                        order.insert_edge(&base, a, b);
                    }
                }
                Op::RemoveEdge(a, b) => {
                    if a != b && base.edge_count(a, b) > 0 {
                        base.decrement_edge(a, b);
                        order.remove_edge(&base, a, b);
                    }
                }
                Op::Contract { winner, loser } => {
                    if winner != loser {
                        order.apply_contract(&base, winner, loser);
                        alias.union(winner, loser);
                    }
                }
                Op::Invalidate => order.invalidate(),
            }
            let context = format!("step {step_index}: {:?}", step.op);
            assert_multi_matches_brute(&mut order, &base, &alias, &universe, &context);
            if let Err(violation) = order.validate(&base) {
                return Err(TestCaseError::fail(format!("{context}: {violation}")));
            }
            let overlay = build_overlay(&base, &step.overlay);
            let (u, v) = step.query;
            prop_assert_eq!(
                order.would_join_multi_scc(&base, &overlay, u, v),
                brute_would_join(&base, &alias, &overlay, u, v),
                "{}: would_join({}, {}) overlay={:?}",
                context, u, v, overlay,
            );
        }
    }

    /// Cold start: `would_join_multi_scc` on a fresh order (the first
    /// query triggers the lazy rebuild) matches the brute-force
    /// reference for random graphs + overlays.
    #[test]
    fn cold_start_would_join_matches_brute_force(
        edges in proptest::collection::vec((arb_node(), arb_node()), 0..30),
        entries in arb_overlay_entries(),
        u in arb_node(),
        v in arb_node(),
    ) {
        let mut base = RollbackDiGraph::new();
        for &(a, b) in &edges {
            if a != b {
                base.increment_edge(a, b);
            }
        }
        let overlay = build_overlay(&base, &entries);
        let alias = TestAlias::default();
        let mut order = CondensationOrder::new();
        prop_assert_eq!(
            order.would_join_multi_scc(&base, &overlay, u, v),
            brute_would_join(&base, &alias, &overlay, u, v),
            "would_join({}, {}) overlay={:?}",
            u, v, overlay,
        );
    }
}
