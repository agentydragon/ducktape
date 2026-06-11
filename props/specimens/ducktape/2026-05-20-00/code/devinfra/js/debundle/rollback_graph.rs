use std::collections::{BTreeMap, BTreeSet};

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
pub(crate) struct RollbackDiGraph<N> {
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

    pub(crate) fn successors(&self, node: N) -> Vec<N> {
        self.out_edges
            .get(&node)
            .map(|edges| edges.iter().copied().collect())
            .unwrap_or_default()
    }

    pub(crate) fn predecessors(&self, node: N) -> Vec<N> {
        self.in_edges
            .get(&node)
            .map(|edges| edges.iter().copied().collect())
            .unwrap_or_default()
    }

    pub(crate) fn scc_containing(&self, node: N) -> BTreeSet<N> {
        let forward = self.reachable_from(node, Direction::Forward);
        let reverse = self.reachable_from(node, Direction::Reverse);
        forward.intersection(&reverse).copied().collect()
    }

    pub(crate) fn all_sccs(&self) -> Vec<BTreeSet<N>> {
        struct Tarjan<'a, N> {
            graph: &'a RollbackDiGraph<N>,
            next_index: usize,
            index_by_node: BTreeMap<N, usize>,
            lowlink_by_node: BTreeMap<N, usize>,
            stack: Vec<N>,
            on_stack: BTreeSet<N>,
            sccs: Vec<BTreeSet<N>>,
        }

        impl<'a, N> Tarjan<'a, N>
        where
            N: Copy + Ord,
        {
            fn visit(&mut self, node: N) {
                let index = self.next_index;
                self.next_index += 1;
                self.index_by_node.insert(node, index);
                self.lowlink_by_node.insert(node, index);
                self.stack.push(node);
                self.on_stack.insert(node);

                for successor in self.graph.successors(node) {
                    if !self.index_by_node.contains_key(&successor) {
                        self.visit(successor);
                        let node_low = self.lowlink_by_node[&node];
                        let successor_low = self.lowlink_by_node[&successor];
                        self.lowlink_by_node
                            .insert(node, node_low.min(successor_low));
                    } else if self.on_stack.contains(&successor) {
                        let node_low = self.lowlink_by_node[&node];
                        let successor_index = self.index_by_node[&successor];
                        self.lowlink_by_node
                            .insert(node, node_low.min(successor_index));
                    }
                }

                if self.lowlink_by_node[&node] == self.index_by_node[&node] {
                    let mut scc = BTreeSet::new();
                    loop {
                        let member = self
                            .stack
                            .pop()
                            .expect("Tarjan stack must contain current component");
                        self.on_stack.remove(&member);
                        scc.insert(member);
                        if member == node {
                            break;
                        }
                    }
                    self.sccs.push(scc);
                }
            }
        }

        let mut state = Tarjan {
            graph: self,
            next_index: 0,
            index_by_node: BTreeMap::new(),
            lowlink_by_node: BTreeMap::new(),
            stack: Vec::new(),
            on_stack: BTreeSet::new(),
            sccs: Vec::new(),
        };

        for node in self.nodes() {
            if !state.index_by_node.contains_key(&node) {
                state.visit(node);
            }
        }
        state.sccs
    }

    fn reachable_from(&self, start: N, direction: Direction) -> BTreeSet<N> {
        let mut seen = BTreeSet::new();
        let mut stack = vec![start];
        while let Some(node) = stack.pop() {
            if !seen.insert(node) {
                continue;
            }
            let neighbors = match direction {
                Direction::Forward => self.successors(node),
                Direction::Reverse => self.predecessors(node),
            };
            for neighbor in neighbors.into_iter().rev() {
                if !seen.contains(&neighbor) {
                    stack.push(neighbor);
                }
            }
        }
        seen
    }

    fn nodes(&self) -> Vec<N> {
        let mut nodes = BTreeSet::new();
        for &(from, to) in self.edge_counts.keys() {
            nodes.insert(from);
            nodes.insert(to);
        }
        nodes.into_iter().collect()
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

#[derive(Debug, Clone, Copy)]
enum Direction {
    Forward,
    Reverse,
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
        assert!(graph.successors(1).is_empty());
        assert!(graph.predecessors(2).is_empty());
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
