use std::collections::{BTreeMap, BTreeSet};

use petgraph::Directed;
use petgraph::algo::tarjan_scc;
use petgraph::visit::{
    GraphBase, GraphProp, GraphRef, IntoNeighbors, IntoNeighborsDirected, IntoNodeIdentifiers,
    NodeIndexable,
};

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

    /// Number of `(from, to)` adjacency pairs with nonzero count.
    /// Counts each parallel-edge multiplicity as 1 (matches the SCC /
    /// reachability view, which is set-based). Used by
    /// `realizability::gate_perf_counters` to record base-graph shape
    /// when `DEBUNDLE_TIMING=1`.
    pub(crate) fn distinct_edge_count(&self) -> usize {
        self.edge_counts.len()
    }

    /// Number of distinct nodes participating in at least one edge.
    /// Matches `PetgraphView`'s `node_bound`. Used by
    /// `realizability::gate_perf_counters` to record base-graph shape
    /// when `DEBUNDLE_TIMING=1`.
    pub(crate) fn node_count(&self) -> usize {
        let mut nodes: BTreeSet<N> = BTreeSet::new();
        nodes.extend(self.out_edges.keys().copied());
        nodes.extend(self.in_edges.keys().copied());
        nodes.len()
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

    /// The strict SCC containing `node`, computed as the intersection
    /// of forward and reverse reachability from `node`. Localized:
    /// cost is bounded by `node`'s reachable cones, not the whole
    /// graph — the previous shape ran a full Tarjan and materialized
    /// every SCC per query. A node with no incident edges yields
    /// `{node}`.
    pub(crate) fn scc_containing(&self, node: N) -> BTreeSet<N> {
        let forward = self.reachable_from(node, |graph, n| graph.successors(n));
        let mut scc: BTreeSet<N> = self
            .reachable_from(node, |graph, n| graph.predecessors(n))
            .intersection(&forward)
            .copied()
            .collect();
        scc.insert(node);
        scc
    }

    /// Nodes reachable from `start` (excluding `start` unless it lies
    /// on a cycle through itself) via the `neighbors` direction.
    fn reachable_from<'a, I>(
        &'a self,
        start: N,
        neighbors: impl Fn(&'a Self, N) -> I,
    ) -> BTreeSet<N>
    where
        I: Iterator<Item = N> + 'a,
    {
        let mut seen: BTreeSet<N> = BTreeSet::new();
        let mut stack: Vec<N> = neighbors(self, start).collect();
        while let Some(n) = stack.pop() {
            if seen.insert(n) {
                stack.extend(neighbors(self, n));
            }
        }
        seen
    }

    pub(crate) fn all_sccs(&self) -> Vec<BTreeSet<N>> {
        let view = PetgraphView::new(self);
        tarjan_scc(&view)
            .into_iter()
            .map(|scc| scc.into_iter().collect())
            .collect()
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

/// Petgraph adapter that materializes a dense node index for
/// [`RollbackDiGraph`]. `RollbackDiGraph` only tracks nodes that
/// participate in at least one edge, matching petgraph `GraphMap`
/// semantics — the view's node bound is the count of such nodes.
struct PetgraphView<'a, N> {
    graph: &'a RollbackDiGraph<N>,
    nodes: Vec<N>,
    index_of: BTreeMap<N, usize>,
}

impl<'a, N> PetgraphView<'a, N>
where
    N: Copy + Ord,
{
    fn new(graph: &'a RollbackDiGraph<N>) -> Self {
        let mut node_set: BTreeSet<N> = BTreeSet::new();
        for &(from, to) in graph.edge_counts.keys() {
            node_set.insert(from);
            node_set.insert(to);
        }
        let nodes: Vec<N> = node_set.into_iter().collect();
        let index_of: BTreeMap<N, usize> = nodes
            .iter()
            .copied()
            .enumerate()
            .map(|(i, n)| (n, i))
            .collect();
        Self {
            graph,
            nodes,
            index_of,
        }
    }
}

impl<N> GraphBase for &PetgraphView<'_, N>
where
    N: Copy + Ord,
{
    type NodeId = N;
    type EdgeId = (N, N);
}

impl<N> GraphRef for &PetgraphView<'_, N> where N: Copy + Ord {}

impl<N> GraphProp for &PetgraphView<'_, N>
where
    N: Copy + Ord,
{
    type EdgeType = Directed;
}

impl<N> NodeIndexable for &PetgraphView<'_, N>
where
    N: Copy + Ord,
{
    fn node_bound(&self) -> usize {
        self.nodes.len()
    }

    fn to_index(&self, node: N) -> usize {
        *self
            .index_of
            .get(&node)
            .expect("node not present in RollbackDiGraph view")
    }

    fn from_index(&self, index: usize) -> N {
        self.nodes[index]
    }
}

/// Iterator over the optionally-present adjacency set for a node.
/// Returning a concrete type lets `IntoNeighbors` etc. avoid boxing.
struct NeighborIter<'a, N> {
    inner: Option<std::collections::btree_set::Iter<'a, N>>,
}

impl<'a, N: Copy> Iterator for NeighborIter<'a, N> {
    type Item = N;

    fn next(&mut self) -> Option<N> {
        self.inner.as_mut()?.next().copied()
    }
}

fn neighbor_iter<'a, N: Ord>(
    adjacency: &'a BTreeMap<N, BTreeSet<N>>,
    node: &N,
) -> NeighborIter<'a, N> {
    NeighborIter {
        inner: adjacency.get(node).map(|set| set.iter()),
    }
}

impl<'a, N> IntoNeighbors for &'a PetgraphView<'_, N>
where
    N: Copy + Ord + 'a,
{
    type Neighbors = NeighborIter<'a, N>;

    fn neighbors(self, node: N) -> Self::Neighbors {
        neighbor_iter(&self.graph.out_edges, &node)
    }
}

impl<'a, N> IntoNeighborsDirected for &'a PetgraphView<'_, N>
where
    N: Copy + Ord + 'a,
{
    type NeighborsDirected = NeighborIter<'a, N>;

    fn neighbors_directed(
        self,
        node: N,
        direction: petgraph::Direction,
    ) -> Self::NeighborsDirected {
        let adjacency = match direction {
            petgraph::Direction::Outgoing => &self.graph.out_edges,
            petgraph::Direction::Incoming => &self.graph.in_edges,
        };
        neighbor_iter(adjacency, &node)
    }
}

impl<'a, N> IntoNodeIdentifiers for &'a PetgraphView<'_, N>
where
    N: Copy + Ord + 'a,
{
    type NodeIdentifiers = std::iter::Copied<std::slice::Iter<'a, N>>;

    fn node_identifiers(self) -> Self::NodeIdentifiers {
        self.nodes.iter().copied()
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use petgraph::algo::tarjan_scc;
    use petgraph::graphmap::DiGraphMap;

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
        assert_eq!(graph.distinct_edge_count(), 2);
    }

    #[test]
    fn scc_containing_is_forward_reverse_reachability_intersection() {
        let mut graph = RollbackDiGraph::new();
        graph.increment_edge(1, 2);
        graph.increment_edge(2, 3);
        graph.increment_edge(3, 1);
        graph.increment_edge(3, 4);

        assert_eq!(
            graph.scc_containing(2),
            BTreeSet::from([1, 2, 3]),
            "1, 2, 3 are mutually reachable",
        );
        assert_eq!(
            graph.scc_containing(4),
            BTreeSet::from([4]),
            "4 is reachable from the cycle but cannot reach it",
        );
    }

    #[test]
    fn tarjan_output_matches_petgraph_for_small_graph() {
        let edges = [(1, 2), (2, 1), (2, 3), (3, 4), (4, 3), (5, 6)];
        let mut graph = RollbackDiGraph::new();
        let mut petgraph: DiGraphMap<i32, (), std::collections::hash_map::RandomState> =
            DiGraphMap::new();
        for (from, to) in edges {
            graph.increment_edge(from, to);
            petgraph.add_edge(from, to, ());
        }

        let mut ours: BTreeSet<BTreeSet<i32>> = graph.all_sccs().into_iter().collect();
        // `RollbackDiGraph` only knows nodes that are edge endpoints,
        // matching this petgraph construction.
        let pet: BTreeSet<BTreeSet<i32>> = tarjan_scc(&petgraph)
            .into_iter()
            .map(|scc| scc.into_iter().collect())
            .collect();

        assert_eq!(ours, pet);

        let mark = graph.mark();
        graph.increment_edge(6, 5);
        ours = graph.all_sccs().into_iter().collect();
        assert!(ours.contains(&BTreeSet::from([5, 6])));
        graph.rollback_to(mark);
        ours = graph.all_sccs().into_iter().collect();
        assert_eq!(ours, pet);
    }
}
