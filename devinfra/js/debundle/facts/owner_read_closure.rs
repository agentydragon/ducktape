//! Shared owner-level read closure for conservative analyses.
//!
//! Read edges point from a statement to the first declaration site of each
//! binding it reads eagerly or lazily. Co-declaration edges connect repeated
//! declaration sites bidirectionally, so a closure rooted at any site reaches
//! the complete declaration unit.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use roaring::RoaringBitmap;
use swc_ecma_ast::Id;

use super::StatementFacts;
use crate::StatementOrdinal;

/// Owner read closure over one chunk's statement facts.
///
/// The SCC summaries preserve the closure used by graph at-init fallback;
/// the owner adjacency also supports incremental traversal for other analyses
/// without materializing a transitive closure for every possible root.
pub(crate) struct FactsOwnerReadClosure {
    binding_owner: BTreeMap<Id, StatementOrdinal>,
    owner_adjacency: BTreeMap<StatementOrdinal, Vec<StatementOrdinal>>,
    expanded_lazy_reads: Vec<BTreeSet<Id>>,
    expanded_lazy_rebinds: Vec<BTreeSet<Id>>,
    scc_of: BTreeMap<StatementOrdinal, usize>,
}

impl FactsOwnerReadClosure {
    /// Build a second adjacency for synchronous callback fallback. The caller
    /// supplies retained callable-prefix reads where coverage is complete;
    /// other owners keep their whole lazy read set. A lexical read remains an
    /// edge even when its target's body is deferred.
    pub(crate) fn synchronous_adjacency(
        &self,
        facts: &[StatementFacts],
        prefix_reads: &[Option<&BTreeSet<Id>>],
    ) -> Vec<Vec<StatementOrdinal>> {
        let mut adjacency = vec![Vec::new(); facts.len()];
        for fact in facts {
            let owner = fact.ordinal;
            for binding in &fact.declared {
                if let Some(&representative) = self.binding_owner.get(binding)
                    && representative != owner
                {
                    adjacency[owner.0].push(representative);
                    adjacency[representative.0].push(owner);
                }
            }
            let lazy = prefix_reads[owner.0].unwrap_or(&fact.reads.lazy);
            for id in fact.reads.eager.iter().chain(lazy) {
                if let Some(&target) = self.binding_owner.get(id) {
                    adjacency[owner.0].push(target);
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
    /// Each reached owner is expanded at most once as callback roots grow.
    pub(crate) fn extend_synchronous_owners(
        adjacency: &[Vec<StatementOrdinal>],
        roots: impl IntoIterator<Item = StatementOrdinal>,
        expanded: &mut BTreeSet<StatementOrdinal>,
    ) -> Vec<StatementOrdinal> {
        let mut pending = VecDeque::new();
        let mut reached = Vec::new();
        for root in roots {
            if root.0 < adjacency.len() && expanded.insert(root) {
                reached.push(root);
                pending.push_back(root);
            }
        }
        while let Some(owner) = pending.pop_front() {
            for &target in &adjacency[owner.0] {
                if expanded.insert(target) {
                    reached.push(target);
                    pending.push_back(target);
                }
            }
        }
        reached
    }

    pub(crate) fn new(facts: &[StatementFacts]) -> Self {
        let mut declaration_sites = BTreeMap::<Id, Vec<StatementOrdinal>>::new();
        for stmt in facts {
            for binding in &stmt.declared {
                declaration_sites
                    .entry(binding.clone())
                    .or_default()
                    .push(stmt.ordinal);
            }
        }
        let binding_owner: BTreeMap<Id, StatementOrdinal> = declaration_sites
            .iter()
            .map(|(binding, sites)| (binding.clone(), sites[0]))
            .collect();

        let mut read_graph: DiGraphMap<StatementOrdinal, ()> = DiGraphMap::new();
        for stmt in facts {
            let owner = stmt.ordinal;
            read_graph.add_node(owner);
            for binding in &stmt.declared {
                if let Some(&representative) = binding_owner.get(binding)
                    && representative != owner
                {
                    read_graph.add_edge(owner, representative, ());
                    read_graph.add_edge(representative, owner, ());
                }
            }
            for id in stmt.reads.eager.iter().chain(stmt.reads.lazy.iter()) {
                if let Some(&target) = binding_owner.get(id) {
                    read_graph.add_edge(owner, target, ());
                }
            }
        }

        let mut owner_adjacency = BTreeMap::new();
        for owner in read_graph.nodes() {
            let mut neighbors: Vec<_> = read_graph.neighbors(owner).collect();
            neighbors.sort_unstable();
            neighbors.dedup();
            owner_adjacency.insert(owner, neighbors);
        }

        let stmt_by_owner: BTreeMap<StatementOrdinal, &StatementFacts> =
            facts.iter().map(|stmt| (stmt.ordinal, stmt)).collect();
        let sccs = tarjan_scc(&read_graph);
        let mut scc_of = BTreeMap::new();
        for (idx, scc) in sccs.iter().enumerate() {
            for owner in scc {
                scc_of.insert(*owner, idx);
            }
        }

        // The same transitive IDs can occur on many outgoing SCC edges. Merge
        // their compact indices here, then materialize the ordered ID sets
        // once for the existing closure query API.
        let binding_ids: Vec<Id> = binding_owner.keys().cloned().collect();
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
            for owner in scc {
                let Some(stmt) = stmt_by_owner.get(owner) else {
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
            for owner in scc {
                for (_, target, _) in read_graph.edges(*owner) {
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
            binding_owner,
            owner_adjacency,
            expanded_lazy_reads,
            expanded_lazy_rebinds,
            scc_of,
        }
    }

    /// Map a chunk-declared binding to its first declaration site.
    pub(crate) fn owner_for_binding(&self, binding: &Id) -> Option<StatementOrdinal> {
        self.binding_owner.get(binding).copied()
    }

    /// Collect declaration owners for bindings, omitting globals and imports
    /// that have no declaration site in this chunk.
    pub(crate) fn owners_for_bindings<'a>(
        &self,
        bindings: impl IntoIterator<Item = &'a Id>,
    ) -> BTreeSet<StatementOrdinal> {
        bindings
            .into_iter()
            .filter_map(|binding| self.owner_for_binding(binding))
            .collect()
    }

    /// Grow `owners` by the transitive read closure of `roots`.
    ///
    /// The caller retains `expanded` as roots grow, so every owner is expanded
    /// at most once even when the same reachability set is extended repeatedly.
    /// Return the newly expanded owners so callers can check exceptional scope
    /// effects without rescanning the accumulated closure.
    /// `owners` may already contain unrelated direct roots; `expanded` is the
    /// separate traversal state that distinguishes those from expanded nodes.
    pub(crate) fn extend_reachable_owners(
        &self,
        roots: impl IntoIterator<Item = StatementOrdinal>,
        expanded: &mut BTreeSet<StatementOrdinal>,
        owners: &mut BTreeSet<StatementOrdinal>,
    ) -> Vec<StatementOrdinal> {
        let mut pending = VecDeque::new();
        let mut reached = Vec::new();
        for root in roots {
            if self.owner_adjacency.contains_key(&root) && expanded.insert(root) {
                owners.insert(root);
                reached.push(root);
                pending.push_back(root);
            }
        }
        while let Some(owner) = pending.pop_front() {
            let Some(neighbors) = self.owner_adjacency.get(&owner) else {
                continue;
            };
            for &target in neighbors {
                if expanded.insert(target) {
                    owners.insert(target);
                    reached.push(target);
                    pending.push_back(target);
                }
            }
        }
        reached
    }

    /// Return the transitive lazy reads and rebinds for one owner, retaining
    /// the original graph-builder semantics for unresolved-call promotion.
    pub(crate) fn lazy_effects_for_owner(
        &self,
        owner: StatementOrdinal,
    ) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        static EMPTY: std::sync::OnceLock<BTreeSet<Id>> = std::sync::OnceLock::new();
        let empty = EMPTY.get_or_init(BTreeSet::new);
        match self.scc_of.get(&owner) {
            Some(&idx) => (
                &self.expanded_lazy_reads[idx],
                &self.expanded_lazy_rebinds[idx],
            ),
            None => (empty, empty),
        }
    }
}
