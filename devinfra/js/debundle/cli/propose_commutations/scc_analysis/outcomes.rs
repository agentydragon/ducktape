//! What still closes a cycle once every proposal is inerted.

use super::{Analysis, SccState, module_graph};
use crate::propose_commutations::report::{
    BlockingEdgeCount, BlockingReason, ResidualComponent, SccOutcome, SccVerdict,
};
use analysis::ModuleId;
use analysis_model::DepKind;
use anyhow::{Result, bail};
use graph::{DependencyEdgeId, PremiseVerdict};
use petgraph::algo::{is_cyclic_directed, tarjan_scc};
use source_identity::SourceUnitId;
use spec::ModulePath;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ops::ControlFlow;

impl Analysis<'_> {
    /// Inert every proposal of each SCC together and report what still
    /// closes a cycle: the constraining edges inside each remaining
    /// strongly connected component, counted by kind and by why no proposal
    /// inerts them.
    pub(crate) fn scc_outcomes(&self) -> Result<Vec<SccOutcome>> {
        let mut outcomes = Vec::new();
        for (&scc_id, scc) in &self.sccs {
            let remaining: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = scc
                .constraining
                .iter()
                .map(|(&pair, edges)| {
                    let left: Vec<DependencyEdgeId> = edges
                        .iter()
                        .copied()
                        .filter(|id| !scc.inerted.contains(id))
                        .collect();
                    (pair, left)
                })
                .filter(|(_, left)| !left.is_empty())
                .collect();
            let mut projected = module_graph(remaining.keys());
            let mut preserved: BTreeMap<(ModuleId, ModuleId), usize> = BTreeMap::new();
            let complete = if scc.inerted.is_empty() {
                true
            } else if is_cyclic_directed(&projected) {
                // A cycle of retained original edges already proves failure.
                false
            } else {
                let proposed: BTreeSet<_> = scc
                    .inerted
                    .iter()
                    .map(|id| {
                        let edge = self.graph.edge(*id);
                        let (a, b) = (edge.from.0, edge.to.0);
                        (SourceUnitId(a.min(b)), SourceUnitId(a.max(b)))
                    })
                    .collect();
                let mut pairs = self.existing_pairs.clone();
                pairs.extend(proposed);
                self.temporal_order.preserve(&pairs, |edge| {
                    if edge.inerted_by.is_some() {
                        return ControlFlow::Continue(());
                    }
                    let pair = (self.assignment.of(edge.from), self.assignment.of(edge.to));
                    if pair.0 == pair.1
                        || !scc.graph.contains_node(pair.0)
                        || !scc.graph.contains_node(pair.1)
                    {
                        return ControlFlow::Continue(());
                    }
                    *preserved.entry(pair).or_default() += 1;
                    let new_arc = !projected.contains_edge(pair.0, pair.1);
                    projected.add_edge(pair.0, pair.1, ());
                    if new_arc && is_cyclic_directed(&projected) {
                        // Enough evidence to reject; avoid expanding every
                        // transitive pair of a long opaque ordering chain.
                        ControlFlow::Break(())
                    } else {
                        ControlFlow::Continue(())
                    }
                })
            };
            let mut residual_components: Vec<(Vec<ModuleId>, ResidualComponent)> =
                tarjan_scc(&projected)
                    .into_iter()
                    .filter(|component| component.len() > 1)
                    .map(|component| {
                        let mut modules: Vec<ModulePath> = component
                            .iter()
                            .map(|module| self.report.quotient.nodes[module.0].path.clone())
                            .collect();
                        modules.sort();
                        (component, ResidualComponent { modules })
                    })
                    .collect();
            residual_components.sort_by(|(_, left), (_, right)| left.modules.cmp(&right.modules));
            let component_of: HashMap<ModuleId, usize> = residual_components
                .iter()
                .enumerate()
                .flat_map(|(index, (component, _))| {
                    component.iter().copied().map(move |module| (module, index))
                })
                .collect();
            let verdict = if residual_components.is_empty() {
                SccVerdict::PairsBreak
            } else {
                let mut counts: BTreeMap<(DepKind, BlockingReason), usize> = BTreeMap::new();
                for ((from, to), edges) in &remaining {
                    let component = component_of.get(from);
                    if component.is_none() || component != component_of.get(to) {
                        continue;
                    }
                    for &id in edges {
                        let kind = self.graph.edge(id).reason.kind();
                        *counts
                            .entry((kind, self.blocking_reason(scc, id)?))
                            .or_default() += 1;
                    }
                }
                for ((from, to), count) in &preserved {
                    if component_of.contains_key(from)
                        && component_of.get(from) == component_of.get(to)
                    {
                        *counts
                            .entry((DepKind::Sequenced, BlockingReason::PreservedOrder))
                            .or_default() += count;
                    }
                }
                let mut blocking_edges: Vec<BlockingEdgeCount> = counts
                    .into_iter()
                    .map(|((kind, reason), count)| BlockingEdgeCount {
                        kind,
                        reason,
                        count,
                    })
                    .collect();
                blocking_edges.sort_by_key(|row| std::cmp::Reverse(row.count));
                let residual_modules = component_of.len();
                let residual_components = residual_components
                    .into_iter()
                    .map(|(_, component)| component)
                    .collect();
                SccVerdict::PairsCannotBreak {
                    counts_are_lower_bounds: !complete,
                    residual_modules,
                    residual_components,
                    blocking_edges,
                }
            };
            outcomes.push(SccOutcome {
                scc_id,
                modules: scc.modules.clone(),
                unproven_endpoints: scc.unproven_endpoints.len(),
                verdict,
            });
        }
        Ok(outcomes)
    }

    /// Why a constraining edge no proposal inerts stays constraining.
    fn blocking_reason(&self, scc: &SccState, id: DependencyEdgeId) -> Result<BlockingReason> {
        let edge = &self.report.edges[id.0];
        let topology_edge = self.graph.edge(id);
        if !matches!(
            topology_edge.reason.kind(),
            DepKind::Sequenced | DepKind::CoDeclaration
        ) {
            return Ok(BlockingReason::KindNotInertable);
        }
        let (source, target) = (topology_edge.from.0, topology_edge.to.0);
        if let Some(&reason) = scc
            .rejected_pairs
            .get(&(source.min(target), source.max(target)))
        {
            return Ok(reason);
        }
        Ok(match &edge.premise_verdict {
            None => BlockingReason::VerdictNotRecorded,
            Some(PremiseVerdict::Summarized) => BlockingReason::Summarized,
            Some(PremiseVerdict::Known { .. }) => BlockingReason::KnownConflict,
            // Every constraining sequenced edge's pair is either rejected or
            // proposed, and a proposal inerts every inertable edge of its pair.
            Some(PremiseVerdict::Inertable) => bail!(
                "inertable edge {} is neither inerted by a proposal nor in a rejected pair",
                edge.id
            ),
        })
    }
}
