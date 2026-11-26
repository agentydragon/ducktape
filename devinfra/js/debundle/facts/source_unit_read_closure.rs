//! Shared source-unit-level read closure for conservative analyses.
//!
//! Read edges point from a statement to the first declaration site of each
//! binding it reads eagerly or lazily. Co-declaration edges connect repeated
//! declaration sites bidirectionally, so a closure rooted at any site reaches
//! the complete declaration unit.

use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use roaring::RoaringBitmap;
use swc_ecma_ast::Id;

use super::StatementFacts;

#[derive(Clone, Default)]
struct LazyEffectBits {
    reads: RoaringBitmap,
    rebinds: RoaringBitmap,
}

struct ClosedLazyEffects {
    summaries: Vec<LazyEffectBits>,
    #[cfg(test)]
    edge_joins: usize,
}

fn close_scc_lazy_effects(
    mut local_effects: Vec<LazyEffectBits>,
    successors: Vec<Vec<usize>>,
) -> ClosedLazyEffects {
    debug_assert_eq!(local_effects.len(), successors.len());
    // SCC condensation is acyclic. A component is ready exactly when all
    // successors have completed; each completed summary crosses each incoming
    // edge once. No parent is recomputed for an intermediate successor value.
    let mut predecessors = vec![Vec::new(); successors.len()];
    for (source, targets) in successors.iter().enumerate() {
        for &target in targets {
            predecessors[target].push(source);
        }
    }
    let mut remaining: Vec<_> = successors.iter().map(Vec::len).collect();
    let mut ready: VecDeque<_> = successors
        .iter()
        .enumerate()
        .filter_map(|(scc, targets)| targets.is_empty().then_some(scc))
        .collect();
    let mut completed = 0;
    #[cfg(test)]
    let mut edge_joins = 0;
    while let Some(successor) = ready.pop_front() {
        completed += 1;
        let summary = local_effects[successor].clone();
        for &predecessor in &predecessors[successor] {
            #[cfg(test)]
            {
                edge_joins += 1;
            }
            local_effects[predecessor].reads |= &summary.reads;
            local_effects[predecessor].rebinds |= &summary.rebinds;
            remaining[predecessor] -= 1;
            if remaining[predecessor] == 0 {
                ready.push_back(predecessor);
            }
        }
    }
    assert_eq!(
        completed,
        local_effects.len(),
        "SCC condensation is acyclic"
    );
    ClosedLazyEffects {
        summaries: local_effects,
        #[cfg(test)]
        edge_joins,
    }
}

/// SourceUnit read closure over one chunk's statement facts.
///
/// The SCC summaries preserve the closure used by graph at-init fallback;
/// the synchronous source unit adjacency also supports incremental traversal for callback analyses
/// without materializing a transitive closure for every possible root.
pub struct SourceUnitReadClosure {
    binding_source_unit: BTreeMap<Id, SourceUnitOrdinal>,
    expanded_lazy_reads: Vec<BTreeSet<Id>>,
    expanded_lazy_rebinds: Vec<BTreeSet<Id>>,
    scc_of: BTreeMap<SourceUnitOrdinal, usize>,
    #[cfg(test)]
    edge_joins: usize,
}

impl SourceUnitReadClosure {
    /// Build an adjacency for synchronous callback fallback. The caller
    /// supplies retained callable-prefix reads where coverage is complete;
    /// other source units keep their whole lazy read set. A lexical read remains an
    /// edge even when its target's body is deferred.
    pub fn synchronous_adjacency(
        &self,
        facts: &[StatementFacts],
        prefix_reads: &[Option<&BTreeSet<Id>>],
    ) -> Vec<Vec<SourceUnitOrdinal>> {
        let mut adjacency = vec![Vec::new(); facts.len()];
        for fact in facts {
            let source_unit = fact.ordinal;
            for binding in &fact.declared {
                if let Some(&representative) = self.binding_source_unit.get(binding)
                    && representative != source_unit
                {
                    adjacency[source_unit.0].push(representative);
                    adjacency[representative.0].push(source_unit);
                }
            }
            let lazy = prefix_reads[source_unit.0].unwrap_or(&fact.reads.lazy);
            for id in fact.reads.eager.iter().chain(lazy) {
                if let Some(&target) = self.binding_source_unit.get(id) {
                    adjacency[source_unit.0].push(target);
                }
            }
        }
        for neighbors in &mut adjacency {
            neighbors.sort_unstable();
            neighbors.dedup();
        }
        adjacency
    }

    /// Grow a persistent fallback cache using only synchronous read edges.
    /// Each reached source unit is expanded at most once as callback roots grow.
    pub fn extend_synchronous_source_units(
        adjacency: &[Vec<SourceUnitOrdinal>],
        roots: impl IntoIterator<Item = SourceUnitOrdinal>,
        expanded: &mut BTreeSet<SourceUnitOrdinal>,
    ) -> Vec<SourceUnitOrdinal> {
        let mut pending = VecDeque::new();
        let mut reached = Vec::new();
        for root in roots {
            if root.0 < adjacency.len() && expanded.insert(root) {
                reached.push(root);
                pending.push_back(root);
            }
        }
        while let Some(source_unit) = pending.pop_front() {
            for &target in &adjacency[source_unit.0] {
                if expanded.insert(target) {
                    reached.push(target);
                    pending.push_back(target);
                }
            }
        }
        reached
    }

    pub fn new(facts: &[StatementFacts]) -> Self {
        Self::build(facts, false)
    }

    /// Graph-level unresolved-call fallback uses the same covered callable
    /// prefixes as the synchronous interpreter. Unknown owners retain their
    /// whole lazy effects. Both traversal and effects must use this view:
    /// retaining only the latter still follows a post-await read to its
    /// declaration and can promote that declaration's lazy bodies.
    pub fn new_synchronous(facts: &[StatementFacts]) -> Self {
        Self::build(facts, true)
    }

    fn build(facts: &[StatementFacts], synchronous: bool) -> Self {
        let mut declaration_sites = BTreeMap::<Id, Vec<SourceUnitOrdinal>>::new();
        for stmt in facts {
            for binding in &stmt.declared {
                declaration_sites
                    .entry(binding.clone())
                    .or_default()
                    .push(stmt.ordinal);
            }
        }
        let binding_source_unit: BTreeMap<Id, SourceUnitOrdinal> = declaration_sites
            .iter()
            .map(|(binding, sites)| (binding.clone(), sites[0]))
            .collect();

        let mut read_graph: DiGraphMap<SourceUnitOrdinal, ()> = DiGraphMap::new();
        for stmt in facts {
            let source_unit = stmt.ordinal;
            read_graph.add_node(source_unit);
            for binding in &stmt.declared {
                if let Some(&representative) = binding_source_unit.get(binding)
                    && representative != source_unit
                {
                    read_graph.add_edge(source_unit, representative, ());
                    read_graph.add_edge(representative, source_unit, ());
                }
            }
            let lazy_reads = if synchronous {
                stmt.synchronous_prefix_effects
                    .as_ref()
                    .map(|(reads, _)| reads)
                    .unwrap_or(&stmt.reads.lazy)
            } else {
                &stmt.reads.lazy
            };
            for id in stmt.reads.eager.iter().chain(lazy_reads.iter()) {
                if let Some(&target) = binding_source_unit.get(id) {
                    read_graph.add_edge(source_unit, target, ());
                }
            }
        }

        let stmt_by_source_unit: BTreeMap<_, _> =
            facts.iter().map(|stmt| (stmt.ordinal, stmt)).collect();
        Self::from_graph(binding_source_unit, read_graph, |source_unit| {
            let stmt = stmt_by_source_unit[&source_unit];
            if synchronous {
                stmt.synchronous_prefix_effects
                    .as_ref()
                    .map(|(reads, rebinds)| (reads, rebinds))
                    .unwrap_or((&stmt.reads.lazy, &stmt.rebinds.lazy))
            } else {
                (&stmt.reads.lazy, &stmt.rebinds.lazy)
            }
        })
    }

    /// Close a sealed source graph with its coverage-checked local effects.
    /// Each component owns one completed summary shared by all of its owners.
    pub fn with_effects(
        &self,
        adjacency: &[Vec<SourceUnitOrdinal>],
        effects: &[(&BTreeSet<Id>, &BTreeSet<Id>)],
    ) -> Self {
        assert_eq!(adjacency.len(), effects.len());
        let mut graph = DiGraphMap::new();
        for (source, targets) in adjacency.iter().enumerate() {
            let source = SourceUnitOrdinal(source);
            graph.add_node(source);
            for &target in targets {
                graph.add_edge(source, target, ());
            }
        }
        Self::from_graph(self.binding_source_unit.clone(), graph, |source| {
            effects[source.0]
        })
    }

    fn from_graph<'a>(
        binding_source_unit: BTreeMap<Id, SourceUnitOrdinal>,
        read_graph: DiGraphMap<SourceUnitOrdinal, ()>,
        effects_for: impl Fn(SourceUnitOrdinal) -> (&'a BTreeSet<Id>, &'a BTreeSet<Id>),
    ) -> Self {
        let sccs = tarjan_scc(&read_graph);
        let mut scc_of = BTreeMap::new();
        for (idx, scc) in sccs.iter().enumerate() {
            for source_unit in scc {
                scc_of.insert(*source_unit, idx);
            }
        }

        // The same transitive IDs can occur on many outgoing SCC edges. Merge
        // their compact indices here, then materialize the ordered ID sets
        // once for the existing closure query API.
        let binding_ids: Vec<Id> = binding_source_unit.keys().cloned().collect();
        let binding_indices: BTreeMap<Id, u32> = binding_ids
            .iter()
            .enumerate()
            .map(|(index, id)| {
                (
                    id.clone(),
                    u32::try_from(index).expect("chunk binding count exceeds bitmap capacity"),
                )
            })
            .collect();
        let mut local_effects = vec![LazyEffectBits::default(); sccs.len()];
        let mut successors = vec![BTreeSet::new(); sccs.len()];
        for (scc_idx, scc) in sccs.iter().enumerate() {
            for source_unit in scc {
                let (reads, rebinds) = effects_for(*source_unit);
                for id in reads {
                    if let Some(&index) = binding_indices.get(id) {
                        local_effects[scc_idx].reads.insert(index);
                    }
                }
                for id in rebinds {
                    if let Some(&index) = binding_indices.get(id) {
                        local_effects[scc_idx].rebinds.insert(index);
                    }
                }
            }
            for source_unit in scc {
                for (_, target, _) in read_graph.edges(*source_unit) {
                    let Some(&target_scc) = scc_of.get(&target) else {
                        continue;
                    };
                    if target_scc == scc_idx {
                        continue;
                    }
                    successors[scc_idx].insert(target_scc);
                }
            }
        }
        let closed_effects = close_scc_lazy_effects(
            local_effects,
            successors
                .into_iter()
                .map(|targets| targets.into_iter().collect())
                .collect(),
        );
        let materialize = |bits: RoaringBitmap| {
            bits.iter()
                .map(|index| binding_ids[index as usize].clone())
                .collect::<BTreeSet<_>>()
        };
        let mut expanded_lazy_reads = Vec::with_capacity(closed_effects.summaries.len());
        let mut expanded_lazy_rebinds = Vec::with_capacity(closed_effects.summaries.len());
        for effects in closed_effects.summaries {
            expanded_lazy_reads.push(materialize(effects.reads));
            expanded_lazy_rebinds.push(materialize(effects.rebinds));
        }

        Self {
            binding_source_unit,
            expanded_lazy_reads,
            expanded_lazy_rebinds,
            scc_of,
            #[cfg(test)]
            edge_joins: closed_effects.edge_joins,
        }
    }

    /// Map a chunk-declared binding to its first declaration site.
    pub(crate) fn source_unit_for_binding(&self, binding: &Id) -> Option<SourceUnitOrdinal> {
        self.binding_source_unit.get(binding).copied()
    }

    /// Collect declaration source units for bindings, omitting globals and imports
    /// that have no declaration site in this chunk.
    pub fn source_units_for_bindings<'a>(
        &self,
        bindings: impl IntoIterator<Item = &'a Id>,
    ) -> BTreeSet<SourceUnitOrdinal> {
        bindings
            .into_iter()
            .filter_map(|binding| self.source_unit_for_binding(binding))
            .collect()
    }

    /// Return the transitive lazy reads and rebinds for one source unit, retaining
    /// the original graph-builder semantics for unresolved-call promotion.
    pub fn lazy_effects_for_source_unit(
        &self,
        source_unit: SourceUnitOrdinal,
    ) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        static EMPTY: std::sync::OnceLock<BTreeSet<Id>> = std::sync::OnceLock::new();
        let empty = EMPTY.get_or_init(BTreeSet::new);
        match self.scc_of.get(&source_unit) {
            Some(&idx) => (
                &self.expanded_lazy_reads[idx],
                &self.expanded_lazy_rebinds[idx],
            ),
            None => (empty, empty),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{LazyEffectBits, SourceUnitReadClosure, close_scc_lazy_effects};
    use petgraph::graphmap::DiGraphMap;
    use roaring::RoaringBitmap;
    use source_identity::SourceUnitOrdinal;
    use std::collections::{BTreeMap, BTreeSet, VecDeque};
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

    #[test]
    fn scc_closure_propagates_transitive_reads_and_rebinds() {
        let mut local = vec![LazyEffectBits::default(); 4];
        local[0].reads.insert(0);
        local[2].rebinds.insert(2);
        local[3].reads.insert(3);

        let effects =
            close_scc_lazy_effects(local, vec![vec![1], vec![2], vec![3], vec![]]).summaries;

        assert_eq!(effects[0].reads, RoaringBitmap::from_iter([0, 3]));
        assert_eq!(effects[0].rebinds, RoaringBitmap::from_iter([2]));
        assert_eq!(effects[1].reads, RoaringBitmap::from_iter([3]));
        assert_eq!(effects[1].rebinds, RoaringBitmap::from_iter([2]));
        assert_eq!(effects[2].reads, RoaringBitmap::from_iter([3]));
        assert_eq!(effects[2].rebinds, RoaringBitmap::from_iter([2]));
        assert_eq!(effects[3].reads, RoaringBitmap::from_iter([3]));
        assert!(effects[3].rebinds.is_empty());
    }

    #[test]
    fn diamond_components_share_the_completed_successor_effects() {
        let mut local = vec![LazyEffectBits::default(); 5];
        for (index, effects) in local.iter_mut().enumerate() {
            effects.reads.insert(index as u32);
        }
        local[1].rebinds.insert(6);
        let effects =
            close_scc_lazy_effects(local, vec![vec![1], vec![], vec![1], vec![0, 2], vec![]])
                .summaries;
        assert_eq!(effects[3].reads, RoaringBitmap::from_iter([0, 1, 2, 3]));
        assert_eq!(effects[3].rebinds, RoaringBitmap::from_iter([6]));
        assert_eq!(effects[0].reads, RoaringBitmap::from_iter([0, 1]));
        assert_eq!(effects[2].reads, RoaringBitmap::from_iter([1, 2]));
        assert_eq!(effects[4].reads, RoaringBitmap::from_iter([4]));
        assert!(effects[4].rebinds.is_empty());
    }

    fn compile(adjacency: &[Vec<usize>]) -> SourceUnitReadClosure {
        let bindings: Vec<Id> = (0..adjacency.len())
            .map(|index| (format!("binding{index}").into(), SyntaxContext::empty()))
            .collect();
        let declarations = bindings
            .iter()
            .enumerate()
            .map(|(index, id)| (id.clone(), SourceUnitOrdinal(index)))
            .collect::<BTreeMap<_, _>>();
        let local: Vec<_> = bindings
            .iter()
            .enumerate()
            .map(|(index, id)| {
                (
                    BTreeSet::from([id.clone()]),
                    if index % 2 == 0 {
                        BTreeSet::from([id.clone()])
                    } else {
                        BTreeSet::new()
                    },
                )
            })
            .collect();
        let mut graph = DiGraphMap::new();
        for (source, targets) in adjacency.iter().enumerate() {
            graph.add_node(SourceUnitOrdinal(source));
            for &target in targets {
                graph.add_edge(SourceUnitOrdinal(source), SourceUnitOrdinal(target), ());
            }
        }
        SourceUnitReadClosure::from_graph(declarations, graph, |source| {
            (&local[source.0].0, &local[source.0].1)
        })
    }

    #[test]
    fn compiled_callback_effects_match_independent_reachability_on_cyclic_graphs() {
        for size in 1..=6 {
            for seed in 0..32 {
                let adjacency: Vec<Vec<_>> = (0..size)
                    .map(|source| {
                        (0..size)
                            .filter(|target| (source * 7 + target * 3 + seed) % 5 < 2)
                            .collect()
                    })
                    .collect();
                let closure = compile(&adjacency);
                for root in 0..size {
                    let mut reached = BTreeSet::from([root]);
                    let mut pending = VecDeque::from([root]);
                    while let Some(source) = pending.pop_front() {
                        for &target in &adjacency[source] {
                            if reached.insert(target) {
                                pending.push_back(target);
                            }
                        }
                    }
                    let expected_reads: BTreeSet<Id> = reached
                        .iter()
                        .map(|index| (format!("binding{index}").into(), SyntaxContext::empty()))
                        .collect();
                    let expected_rebinds: BTreeSet<Id> = reached
                        .iter()
                        .filter(|index| *index % 2 == 0)
                        .map(|index| (format!("binding{index}").into(), SyntaxContext::empty()))
                        .collect();
                    let (reads, rebinds) =
                        closure.lazy_effects_for_source_unit(SourceUnitOrdinal(root));
                    assert_eq!(reads, &expected_reads);
                    assert_eq!(rebinds, &expected_rebinds);
                }
            }
        }
    }

    #[test]
    fn shared_callback_tails_are_closed_once_across_repeated_queries() {
        for width in [4, 16, 64] {
            // Many roots share one long tail. The tail terminates in a cycle,
            // whose local effects are joined inside its single component.
            let mut adjacency = vec![Vec::new(); width * 2];
            for targets in &mut adjacency[..width] {
                targets.push(width);
            }
            for (source, targets) in adjacency.iter_mut().enumerate().skip(width) {
                targets.push(if source + 1 == width * 2 {
                    width * 2 - 2
                } else {
                    source + 1
                });
            }
            let closure = compile(&adjacency);
            assert_eq!(closure.edge_joins, width * 2 - 2);
            for _ in 0..32 {
                for root in 0..width {
                    let (reads, _) = closure.lazy_effects_for_source_unit(SourceUnitOrdinal(root));
                    assert_eq!(reads.len(), width + 1);
                }
            }
            assert_eq!(closure.edge_joins, width * 2 - 2);
        }
    }
}
