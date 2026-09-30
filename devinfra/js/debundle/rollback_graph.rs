use std::collections::{BTreeMap, BTreeSet};
use std::hash::Hash;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

/// Counted directed graph with small graph queries.
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
        }
    }

    pub(crate) fn edge_count(&self, from: N, to: N) -> usize {
        self.edge_counts.get(&(from, to)).copied().unwrap_or(0)
    }

    #[cfg(test)]
    fn contains_edge(&self, from: N, to: N) -> bool {
        self.edge_count(from, to) > 0
    }

    pub(crate) fn increment_edge(&mut self, from: N, to: N) {
        self.set_edge_count(from, to, self.edge_count(from, to) + 1);
    }

    pub(crate) fn decrement_edge(&mut self, from: N, to: N) {
        let old_count = self.edge_count(from, to);
        assert!(
            old_count > 0,
            "RollbackDiGraph::decrement_edge called for absent edge",
        );
        self.set_edge_count(from, to, old_count - 1);
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

    fn set_edge_count(&mut self, from: N, to: N, count: usize) {
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
    fn all_sccs_reflect_added_edges() {
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

        graph.increment_edge(6, 5);
        assert!(sccs(&graph).contains(&BTreeSet::from([5, 6])));
    }
}
