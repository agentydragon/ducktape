//! Candidate collection and ranking: which constraining pairs a premise could inert.

use super::{
    Analysis, Candidate, ConstrainingEdge, PairVerdict, RankedCandidate, SccState, cut_effect,
    module_graph,
};
use crate::propose_commutations::report::{
    CoLocationNeed, ConflictingEdge, RejectedCandidate, RejectionReason,
};
use ::gate::{BlockingSccEntry, CycleEdge};
use analysis::ModuleId;
use analysis_model::DepKind;
use anyhow::{Context, Result};
use graph::{DependencyEdgeId, PremiseVerdict};
use graph::{EndpointView, assignment_endpoints};
use petgraph::algo::is_cyclic_directed;
use source_identity::SourceUnitId;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::ops::ControlFlow;

impl Analysis<'_> {
    /// Constraining unit edges between members of `modules`, by module pair in
    /// the gate view (see [`SccState::constraining`]).
    fn scc_constraining_edges(
        &self,
        modules: &BTreeSet<ModuleId>,
    ) -> BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> {
        let mut out: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = BTreeMap::new();
        for edge in self.graph.iter_edges() {
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let Some((from, to)) = assignment_endpoints(edge, &self.assignment, EndpointView::Gate)
            else {
                continue;
            };
            if modules.contains(&from) && modules.contains(&to) {
                out.entry((from, to)).or_default().push(edge.id);
            }
        }
        out
    }

    /// Classify every constraining `sequenced` edge inside every SCC: a pair
    /// the gate would accept becomes a candidate (selectors come later). The
    /// SCC's `cut` is the gate's feedback arc set, chosen without regard to
    /// what a premise can inert, so the pair that breaks the SCC may sit on
    /// another arc; the cut only marks which candidates it covers and which
    /// at-init edges need co-location instead.
    pub(crate) fn collect_candidates(
        &mut self,
        sccs: &[BlockingSccEntry],
    ) -> Result<(Vec<CoLocationNeed>, Vec<RejectedCandidate>)> {
        let mut needs_co_location = Vec::new();
        let mut rejected: Vec<RejectedCandidate> = Vec::new();
        let mut rejected_by_pair: HashMap<(usize, usize), usize> = HashMap::new();
        for scc in sccs {
            let scc_module_ids = scc
                .modules
                .iter()
                .map(|path| {
                    self.report
                        .quotient
                        .nodes
                        .iter()
                        .position(|entry| &entry.path == path)
                        .map(ModuleId)
                        .with_context(|| format!("SCC module {path} is not in the module table"))
                })
                .collect::<Result<BTreeSet<_>>>()?;
            let constraining = self.scc_constraining_edges(&scc_module_ids);
            let mut cut_indices_by_edge: BTreeMap<DependencyEdgeId, Vec<usize>> = BTreeMap::new();
            for (cut_index, cut) in scc.cut.iter().enumerate() {
                if cut.kind != DepKind::Sequenced {
                    needs_co_location.push(CoLocationNeed {
                        scc_id: scc.id,
                        cut_index,
                        edge: cut.clone(),
                    });
                    continue;
                }
                let matching = self.matching_graph_edges(cut);
                if matching.is_empty() {
                    rejected.push(RejectedCandidate {
                        scc_id: scc.id,
                        also_in_sccs: Vec::new(),
                        cut_edges: vec![cut_index],
                        a: None,
                        b: None,
                        reason: RejectionReason::CutEdgeNotInGraph,
                    });
                    continue;
                }
                for edge_id in matching {
                    cut_indices_by_edge
                        .entry(edge_id)
                        .or_default()
                        .push(cut_index);
                }
            }
            // Unordered source-unit pair -> its sequenced edges, each with the
            // module pair it constrains.
            let mut by_pair: BTreeMap<(usize, usize), Vec<ConstrainingEdge>> = BTreeMap::new();
            for (&module_pair, edges) in &constraining {
                for &edge_id in edges {
                    let edge = self.graph.edge(edge_id);
                    if edge.reason.kind() != DepKind::Sequenced {
                        continue;
                    }
                    let (a, b) = (edge.from.0, edge.to.0);
                    by_pair
                        .entry((a.min(b), a.max(b)))
                        .or_default()
                        .push(ConstrainingEdge {
                            id: edge_id,
                            module_pair,
                        });
                }
            }
            let mut rejected_pairs = HashMap::new();
            for ((a, b), edges) in by_pair {
                let mut cut_edges: Vec<usize> = edges
                    .iter()
                    .flat_map(|edge| {
                        cut_indices_by_edge
                            .get(&edge.id)
                            .into_iter()
                            .flatten()
                            .copied()
                    })
                    .collect();
                cut_edges.sort_unstable();
                cut_edges.dedup();
                match self.classify_pair(a, b) {
                    PairVerdict::Inertable(inerted) => self.candidates.push(Candidate {
                        scc_id: scc.id,
                        cut_edges,
                        module_pair: edges[0].module_pair,
                        a,
                        b,
                        inerted,
                    }),
                    PairVerdict::Rejected(reason) => {
                        if let Some(blocking) = reason.blocking_reason() {
                            rejected_pairs.insert((a, b), blocking);
                        }
                        // A nested SCC repeats its enclosing SCC's pairs.
                        match rejected_by_pair.get(&(a, b)) {
                            Some(&index) => {
                                let row: &mut RejectedCandidate = &mut rejected[index];
                                row.also_in_sccs.push(scc.id);
                            }
                            None => {
                                rejected_by_pair.insert((a, b), rejected.len());
                                rejected.push(RejectedCandidate {
                                    scc_id: scc.id,
                                    also_in_sccs: Vec::new(),
                                    cut_edges,
                                    a: Some(self.node(a).id.clone()),
                                    b: Some(self.node(b).id.clone()),
                                    reason,
                                });
                            }
                        }
                    }
                }
            }
            let edge_module_pairs = constraining
                .iter()
                .flat_map(|(&pair, edges)| edges.iter().map(move |&id| (id, pair)))
                .collect();
            let graph = module_graph(constraining.keys());
            let cyclic = is_cyclic_directed(&graph);
            self.sccs.insert(
                scc.id,
                SccState {
                    modules: scc.modules.clone(),
                    constraining,
                    edge_module_pairs,
                    graph,
                    cyclic,
                    rejected_pairs,
                    inerted: HashSet::new(),
                    unproven_endpoints: HashSet::new(),
                },
            );
        }
        Ok((needs_co_location, rejected))
    }

    /// Graph edges the cut edge stands for: `sequenced`, same statement
    /// ordinal, endpoints in the cut's module pair.
    fn matching_graph_edges(&self, cut: &CycleEdge) -> Vec<DependencyEdgeId> {
        self.sequenced_by_ordinal
            .get(&cut.statement_ordinal)
            .into_iter()
            .flatten()
            .copied()
            .filter(|id| {
                let edge = self.graph.edge(*id);
                let (source, target) = (edge.from.0, edge.to.0);
                self.node_modules[source] == &cut.from && self.node_modules[target] == &cut.to
            })
            .collect()
    }

    /// What the gate would do with a premise over `(a, b)`, read from the
    /// verdict retained on every edge between them.
    fn classify_pair(&self, a: usize, b: usize) -> PairVerdict {
        let mut conflicts = Vec::new();
        let mut inerted = Vec::new();
        let mut summarized = false;
        for &id in self.premise_edges.get(&(a, b)).into_iter().flatten() {
            let edge = &self.report.edges[id.0];
            match &edge.premise_verdict {
                None => {
                    return PairVerdict::Rejected(RejectionReason::VerdictNotRecorded {
                        edge: edge.id.clone(),
                    });
                }
                Some(PremiseVerdict::Inertable) => inerted.push(id),
                Some(PremiseVerdict::Summarized) => summarized = true,
                Some(PremiseVerdict::Known { conflict }) => conflicts.push(ConflictingEdge {
                    edge: edge.id.clone(),
                    from: edge.source.clone(),
                    to: edge.target.clone(),
                    conflict: conflict.clone(),
                }),
            }
        }
        if !conflicts.is_empty() {
            return PairVerdict::Rejected(RejectionReason::KnownConflict { conflicts });
        }
        if inerted.is_empty() {
            debug_assert!(summarized, "a sequenced cut edge joins the pair");
            return PairVerdict::Rejected(RejectionReason::UnusedSummarized);
        }
        PairVerdict::Inertable(inerted)
    }
    fn cut_effect(&self, scc: &SccState, candidate: &Candidate) -> (bool, usize) {
        let (raw_cut, original_left) = cut_effect(scc, candidate);
        let mut projected = raw_cut.then(|| {
            let inerted: HashSet<_> = candidate.inerted.iter().copied().collect();
            module_graph(scc.constraining.iter().filter_map(|(pair, edges)| {
                edges.iter().any(|id| !inerted.contains(id)).then_some(pair)
            }))
        });
        let mut pairs = self.existing_pairs.clone();
        pairs.push((SourceUnitId(candidate.a), SourceUnitId(candidate.b)));
        let mut added_on_pair = 0;
        self.temporal_order.preserve(&pairs, |edge| {
            if edge.inerted_by.is_some() {
                return ControlFlow::Continue(());
            }
            let pair = (self.assignment.of(edge.from), self.assignment.of(edge.to));
            if pair == candidate.module_pair {
                added_on_pair += 1;
            }
            if let Some(graph) = &mut projected
                && pair.0 != pair.1
                && scc.graph.contains_node(pair.0)
                && scc.graph.contains_node(pair.1)
            {
                graph.add_edge(pair.0, pair.1, ());
            }
            ControlFlow::Continue(())
        });
        (
            projected
                .as_ref()
                .is_some_and(|graph| !is_cyclic_directed(graph)),
            original_left + added_on_pair,
        )
    }

    /// Every candidate once, with its effect in the SCC it ranks best in,
    /// best first: a single-pair cut, then one that empties its module-pair
    /// arc, then more inerted edges, then one the gate's cut names.
    pub(crate) fn rank_candidates(&mut self) -> Vec<RankedCandidate> {
        let mut by_pair: HashMap<(usize, usize), Vec<RankedCandidate>> = HashMap::new();
        for candidate in std::mem::take(&mut self.candidates) {
            let (single_pair_cut, remaining_module_pair_edges) =
                self.cut_effect(&self.sccs[&candidate.scc_id], &candidate);
            by_pair
                .entry((candidate.a, candidate.b))
                .or_default()
                .push(RankedCandidate {
                    candidate,
                    also_in_sccs: Vec::new(),
                    single_pair_cut,
                    remaining_module_pair_edges,
                });
        }
        let mut ranked: Vec<RankedCandidate> = by_pair
            .into_values()
            .map(|mut placements| {
                placements.sort_by_key(RankedCandidate::rank_key);
                let mut placements = placements.into_iter();
                let mut best = placements.next().expect("a pair has a placement");
                best.also_in_sccs = placements
                    .map(|placement| placement.candidate.scc_id)
                    .collect();
                best.also_in_sccs.sort_unstable();
                best
            })
            .collect();
        ranked.sort_by_key(RankedCandidate::rank_key);
        ranked
    }
}
