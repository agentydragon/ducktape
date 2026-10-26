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

/// SourceUnit read closure over one chunk's statement facts.
///
/// The SCC summaries preserve the closure used by graph at-init fallback;
/// the synchronous source unit adjacency also supports incremental traversal for callback analyses
/// without materializing a transitive closure for every possible root.
pub(crate) struct SourceUnitReadClosure {
    binding_source_unit: BTreeMap<Id, SourceUnitOrdinal>,
    expanded_lazy_reads: Vec<BTreeSet<Id>>,
    expanded_lazy_rebinds: Vec<BTreeSet<Id>>,
    scc_of: BTreeMap<SourceUnitOrdinal, usize>,
}

impl SourceUnitReadClosure {
    /// Build an adjacency for synchronous callback fallback. The caller
    /// supplies retained callable-prefix reads where coverage is complete;
    /// other source units keep their whole lazy read set. A lexical read remains an
    /// edge even when its target's body is deferred.
    pub(crate) fn synchronous_adjacency(
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
    pub(crate) fn extend_synchronous_source_units(
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

    pub(crate) fn new(facts: &[StatementFacts]) -> Self {
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
            for id in stmt.reads.eager.iter().chain(stmt.reads.lazy.iter()) {
                if let Some(&target) = binding_source_unit.get(id) {
                    read_graph.add_edge(source_unit, target, ());
                }
            }
        }

        let stmt_by_source_unit: BTreeMap<SourceUnitOrdinal, &StatementFacts> =
            facts.iter().map(|stmt| (stmt.ordinal, stmt)).collect();
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
        let mut read_bits = vec![RoaringBitmap::new(); sccs.len()];
        let mut rebind_bits = vec![RoaringBitmap::new(); sccs.len()];
        for (scc_idx, scc) in sccs.iter().enumerate() {
            let mut reads = RoaringBitmap::new();
            let mut rebinds = RoaringBitmap::new();
            for source_unit in scc {
                let Some(stmt) = stmt_by_source_unit.get(source_unit) else {
                    continue;
                };
                for id in &stmt.reads.lazy {
                    if let Some(&index) = binding_indices.get(id) {
                        reads.insert(index);
                    }
                }
                for id in &stmt.rebinds.lazy {
                    if let Some(&index) = binding_indices.get(id) {
                        rebinds.insert(index);
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
                    reads |= &read_bits[target_scc];
                    rebinds |= &rebind_bits[target_scc];
                }
            }
            read_bits[scc_idx] = reads;
            rebind_bits[scc_idx] = rebinds;
        }
        let materialize = |bits: RoaringBitmap| {
            bits.iter()
                .map(|index| binding_ids[index as usize].clone())
                .collect::<BTreeSet<_>>()
        };
        let expanded_lazy_reads = read_bits.into_iter().map(materialize).collect();
        let expanded_lazy_rebinds = rebind_bits.into_iter().map(materialize).collect();

        Self {
            binding_source_unit,
            expanded_lazy_reads,
            expanded_lazy_rebinds,
            scc_of,
        }
    }

    /// Map a chunk-declared binding to its first declaration site.
    pub(crate) fn source_unit_for_binding(&self, binding: &Id) -> Option<SourceUnitOrdinal> {
        self.binding_source_unit.get(binding).copied()
    }

    /// Collect declaration source units for bindings, omitting globals and imports
    /// that have no declaration site in this chunk.
    pub(crate) fn source_units_for_bindings<'a>(
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
    pub(crate) fn lazy_effects_for_source_unit(
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
