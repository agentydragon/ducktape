use std::collections::{BTreeMap, BTreeSet};
use std::hash::Hash;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

/// Journal position for [`RollbackDiGraph`]. Rolling back to a mark
/// restores every edge count changed after the mark was created.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) struct GraphMark(usize);

#[derive(Debug, Clone)]
struct EdgeJournalEntry<N> {
    from: N,
    to: N,
    old_count: usize,
}

/// Counted directed graph with LIFO rollback and small graph queries.
///
/// The graph stores one adjacency edge for each `(from, to)` pair
/// whose count is nonzero. Parallel edge reasons are represented by
/// incrementing the count. This is intentionally generic and unaware
/// of debundle owner/module semantics; callers layer evidence and
/// domain-specific labels on top.
#[derive(Debug, Clone)]
pub struct RollbackDiGraph<N> {
    edge_counts: BTreeMap<(N, N), usize>,
    out_edges: BTreeMap<N, BTreeSet<N>>,
    in_edges: BTreeMap<N, BTreeSet<N>>,
    journal: Vec<EdgeJournalEntry<N>>,
}

impl<N> Default for RollbackDiGraph<N>
where
    N: Copy + Ord,
{
    fn default() -> Self {
        Self::new()
    }
}

impl<N> RollbackDiGraph<N>
where
    N: Copy + Ord,
{
    pub(crate) fn new() -> Self {
        Self {
            edge_counts: BTreeMap::new(),
            out_edges: BTreeMap::new(),
            in_edges: BTreeMap::new(),
            journal: Vec::new(),
        }
    }

    pub(crate) fn mark(&self) -> GraphMark {
        GraphMark(self.journal.len())
    }

    pub(crate) fn rollback_to(&mut self, mark: GraphMark) {
        while self.journal.len() > mark.0 {
            let entry = self
                .journal
                .pop()
                .expect("journal length checked before pop");
            self.restore_edge_count(entry.from, entry.to, entry.old_count);
        }
    }

    /// Discard the rollback journal for everything applied so far.
    /// Call when the current graph state is committed — i.e. no
    /// rollback past this point will ever be requested. Without this,
    /// permanently-applied mutations accumulate journal entries for
    /// the lifetime of the graph.
    ///
    /// Invalidates every outstanding [`GraphMark`]: marks taken
    /// before `commit` must not be passed to `rollback_to` afterwards
    /// (the caller contract in `RealizabilityIndex` guarantees this —
    /// speculative push/undo pairs are balanced before a commit).
    pub(crate) fn commit(&mut self) {
        self.journal.clear();
    }

    pub(crate) fn edge_count(&self, from: N, to: N) -> usize {
        self.edge_counts.get(&(from, to)).copied().unwrap_or(0)
    }

    #[cfg(test)]
    fn contains_edge(&self, from: N, to: N) -> bool {
        self.edge_count(from, to) > 0
    }

    pub(crate) fn increment_edge(&mut self, from: N, to: N) {
        let old_count = self.edge_count(from, to);
        self.journal.push(EdgeJournalEntry {
            from,
            to,
            old_count,
        });
        self.restore_edge_count(from, to, old_count + 1);
    }

    pub(crate) fn decrement_edge(&mut self, from: N, to: N) {
        let old_count = self.edge_count(from, to);
        assert!(
            old_count > 0,
            "RollbackDiGraph::decrement_edge called for absent edge",
        );
        self.journal.push(EdgeJournalEntry {
            from,
            to,
            old_count,
        });
        self.restore_edge_count(from, to, old_count - 1);
    }

    pub(crate) fn successors(&self, node: N) -> impl Iterator<Item = N> + '_ {
        self.out_edges
            .get(&node)
            .map(|edges| edges.iter().copied())
            .into_iter()
            .flatten()
    }

    /// Every `(from, to)` pair whose edge count is nonzero, in sorted
    /// order. Used by callers that need to materialize the full
    /// adjacency outside the graph's internal representation (e.g. the
    /// realizability simulator that mirrors emit-time DFS).
    pub(crate) fn edge_pairs(&self) -> impl Iterator<Item = (N, N)> + '_ {
        self.edge_counts.keys().copied()
    }

    pub(crate) fn predecessors(&self, node: N) -> impl Iterator<Item = N> + '_ {
        self.in_edges
            .get(&node)
            .map(|edges| edges.iter().copied())
            .into_iter()
            .flatten()
    }

    fn restore_edge_count(&mut self, from: N, to: N, count: usize) {
        let old_count = self.edge_count(from, to);
        if old_count == count {
            return;
        }
        if old_count == 0 && count > 0 {
            self.out_edges.entry(from).or_default().insert(to);
            self.in_edges.entry(to).or_default().insert(from);
        } else if old_count > 0 && count == 0 {
            remove_adjacent(&mut self.out_edges, from, to);
            remove_adjacent(&mut self.in_edges, to, from);
        }

        if count == 0 {
            self.edge_counts.remove(&(from, to));
        } else {
            self.edge_counts.insert((from, to), count);
        }
    }
}

impl<N> RollbackDiGraph<N>
where
    N: Copy + Ord + Hash,
{
    pub(crate) fn all_sccs(&self) -> Vec<BTreeSet<N>> {
        tarjan_scc(&DiGraphMap::<N, ()>::from_edges(self.edge_pairs()))
            .into_iter()
            .map(|scc| scc.into_iter().collect())
            .collect()
    }
}

fn remove_adjacent<N>(adjacency: &mut BTreeMap<N, BTreeSet<N>>, from: N, to: N)
where
    N: Copy + Ord,
{
    let Some(edges) = adjacency.get_mut(&from) else {
        return;
    };
    edges.remove(&to);
    if edges.is_empty() {
        adjacency.remove(&from);
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::RollbackDiGraph;

    #[test]
    fn counted_parallel_edges_keep_adjacency_until_last_edge_is_removed() {
        let mut graph = RollbackDiGraph::new();
        graph.increment_edge(1, 2);
        graph.increment_edge(1, 2);
        assert_eq!(graph.edge_count(1, 2), 2);
        assert!(graph.contains_edge(1, 2));

        graph.decrement_edge(1, 2);
        assert_eq!(graph.edge_count(1, 2), 1);
        assert!(graph.contains_edge(1, 2));

        graph.decrement_edge(1, 2);
        assert_eq!(graph.edge_count(1, 2), 0);
        assert!(!graph.contains_edge(1, 2));
        assert!(graph.successors(1).next().is_none());
        assert!(graph.predecessors(2).next().is_none());
    }

    #[test]
    fn rollback_restores_edge_counts_and_adjacency() {
        let mut graph = RollbackDiGraph::new();
        graph.increment_edge("a", "b");
        let mark = graph.mark();
        graph.increment_edge("a", "b");
        graph.increment_edge("b", "a");
        graph.decrement_edge("a", "b");

        assert_eq!(graph.edge_count("a", "b"), 1);
        assert_eq!(graph.edge_count("b", "a"), 1);
        assert!(graph.contains_edge("b", "a"));

        graph.rollback_to(mark);
        assert_eq!(graph.edge_count("a", "b"), 1);
        assert_eq!(graph.edge_count("b", "a"), 0);
        assert!(graph.contains_edge("a", "b"));
        assert!(!graph.contains_edge("b", "a"));
    }

    #[test]
    fn commit_truncates_journal_and_keeps_state_rollbackable_from_new_baseline() {
        let mut graph = RollbackDiGraph::new();
        graph.increment_edge("a", "b");
        graph.increment_edge("b", "c");
        graph.commit();
        // Committed edges survive; a rollback to a post-commit mark
        // only unwinds post-commit work.
        let mark = graph.mark();
        graph.increment_edge("c", "a");
        graph.rollback_to(mark);
        assert_eq!(graph.edge_count("a", "b"), 1);
        assert_eq!(graph.edge_count("b", "c"), 1);
        assert_eq!(graph.edge_count("c", "a"), 0);
        // Rolling back to the post-commit baseline is a no-op.
        graph.rollback_to(graph.mark());
        assert_eq!(graph.edge_pairs().count(), 2);
    }

    #[test]
    fn all_sccs_follow_rollback() {
        let mut graph = RollbackDiGraph::new();
        for (from, to) in [(1, 2), (2, 1), (2, 3), (3, 4), (4, 3), (5, 6)] {
            graph.increment_edge(from, to);
        }
        let sccs = |graph: &RollbackDiGraph<i32>| -> BTreeSet<BTreeSet<i32>> {
            graph.all_sccs().into_iter().collect()
        };
        let baseline = BTreeSet::from([
            BTreeSet::from([1, 2]),
            BTreeSet::from([3, 4]),
            BTreeSet::from([5]),
            BTreeSet::from([6]),
        ]);
        assert_eq!(sccs(&graph), baseline);

        let mark = graph.mark();
        graph.increment_edge(6, 5);
        assert!(sccs(&graph).contains(&BTreeSet::from([5, 6])));
        graph.rollback_to(mark);
        assert_eq!(sccs(&graph), baseline);
    }
}
