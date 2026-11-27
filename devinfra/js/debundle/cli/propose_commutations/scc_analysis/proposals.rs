//! Endpoint selectors and the proposals emitted for ranked candidates.

use super::{Analysis, EndpointPlan, EndpointSelector, RankedCandidate};
use crate::propose_commutations::report::{
    BlockingReason, CommutationProposal, EndpointSelectorFailure, InertedEdge, ProposedEndpoint,
    RankReason, RejectedCandidate, RejectionReason, SelectorOrigin,
};
use anyhow::{Context, Result};
use graph::{DependencyEdgeReport, ModuleDestinationKind};
use selector_codemod::source_unit::{
    SourceUnitSelectorRequest, SourceUnitSelectorSynthesis, SourceUnitSelectorSynthesizer,
    with_source_unit_selector_synthesizer,
};
use spec::ResolvedMemberSelector;
use spec::input_contracts::{CommutingStatementsContract, SourceUnitSelector};
use std::collections::HashMap;

const EXCERPT_LIMIT: usize = 120;

fn excerpt_of(original_source: &str) -> String {
    let first_line = original_source.lines().next().unwrap_or_default().trim();
    match first_line.char_indices().nth(EXCERPT_LIMIT) {
        Some((cut, _)) => format!("{}…", &first_line[..cut]),
        None => first_line.to_owned(),
    }
}

impl Analysis<'_> {
    fn endpoint_plan(&self, node_index: usize) -> Result<EndpointPlan, EndpointSelectorFailure> {
        let node = self.node(node_index);
        if let Some(binding) = node.declared_bindings.first() {
            let member = self.authored_modules.and_then(|authored| {
                let entry = self.report.module(&node.destination)?;
                (entry.kind == ModuleDestinationKind::Specified)
                    .then(|| authored.get(&entry.path))
                    .flatten()
            });
            return Ok(match member {
                Some(module) => EndpointPlan::ResolvedMember(ResolvedMemberSelector {
                    module: module.clone(),
                    name: binding.export_name.to_string(),
                }),
                None => EndpointPlan::Synthesize(SourceUnitSelectorRequest::DeclaredBinding {
                    binding: binding.binding.to_string(),
                }),
            });
        }
        let body_index = self.body_indices[node_index].ok_or_else(|| {
            EndpointSelectorFailure::OutsideParsedSource {
                source_unit: node.id.clone(),
                statement_ordinal: node.statement_ordinal.0,
            }
        })?;
        Ok(EndpointPlan::Synthesize(
            SourceUnitSelectorRequest::AnonymousStatement { body_index },
        ))
    }

    fn endpoint_report(
        &self,
        node_index: usize,
        selector_origin: SelectorOrigin,
    ) -> Result<ProposedEndpoint> {
        let node = self.node(node_index);
        let excerpt = match self.body_indices[node_index] {
            Some(index) => Some(excerpt_of(
                self.source
                    .source_unit_original_source(index, node.statement_ordinal)?,
            )),
            None => None,
        };
        Ok(ProposedEndpoint {
            source_unit: node.id.clone(),
            binding: node.declared_bindings.first().map(|b| b.binding.clone()),
            module: self.module_path(&node.destination)?.clone(),
            source_location: node.source_location.clone(),
            excerpt,
            selector_origin,
        })
    }
    /// Walk `ranked` in order, emitting a proposal for each candidate whose
    /// endpoints have selectors until `limit` are emitted, and demoting one
    /// without to `rejected`. Past the limit, an anonymous endpoint is still
    /// proven (one resolve of its exact statement) and a declared-binding
    /// one, whose proof is a minimizing search, is assumed selectable and
    /// recorded as unproven. Each SCC's `inerted` gets every candidate not
    /// demoted. Returns the proposals and how many candidates were not
    /// demoted.
    pub(crate) fn emit_proposals(
        &mut self,
        ranked: Vec<RankedCandidate>,
        limit: Option<usize>,
        rejected: &mut Vec<RejectedCandidate>,
    ) -> Result<(Vec<CommutationProposal>, usize)> {
        let source = self.source;
        with_source_unit_selector_synthesizer(source.module(), |synthesizer| {
            let mut selectors = HashMap::new();
            let mut proposals = Vec::new();
            let mut total = 0;
            for ranked in ranked {
                let emit = limit.is_none_or(|limit| proposals.len() < limit);
                let candidate = &ranked.candidate;
                let endpoints = [candidate.a, candidate.b].map(|node_index| {
                    self.endpoint_selector(synthesizer, &mut selectors, node_index, emit)
                });
                let [a, b] = endpoints;
                let (a, b) = (a?, b?);
                let failure = [(candidate.a, &a), (candidate.b, &b)].into_iter().find_map(
                    |(node_index, selector)| match selector {
                        Some(Err(detail)) => Some((node_index, detail.clone())),
                        _ => None,
                    },
                );
                let sccs = std::iter::once(candidate.scc_id)
                    .chain(ranked.also_in_sccs.iter().copied())
                    .collect::<Vec<_>>();
                if let Some((failed, detail)) = failure {
                    for &scc_id in &sccs {
                        self.scc_mut(scc_id)
                            .rejected_pairs
                            .insert((candidate.a, candidate.b), BlockingReason::NoUniqueSelector);
                    }
                    let (a, b) = (
                        Some(self.node(candidate.a).id.clone()),
                        Some(self.node(candidate.b).id.clone()),
                    );
                    rejected.push(RejectedCandidate {
                        scc_id: ranked.candidate.scc_id,
                        also_in_sccs: ranked.also_in_sccs,
                        cut_edges: ranked.candidate.cut_edges,
                        a,
                        b,
                        reason: RejectionReason::NoUniqueSelector {
                            endpoint: self.node(failed).id.clone(),
                            detail,
                        },
                    });
                    continue;
                }
                total += 1;
                let unproven = [(candidate.a, &a), (candidate.b, &b)]
                    .into_iter()
                    .filter(|(_, selector)| selector.is_none())
                    .map(|(node_index, _)| node_index)
                    .collect::<Vec<_>>();
                for &scc_id in &sccs {
                    let scc = self.scc_mut(scc_id);
                    scc.inerted.extend(candidate.inerted.iter().copied());
                    scc.unproven_endpoints.extend(unproven.iter().copied());
                }
                if let (Some(Ok(a)), Some(Ok(b))) = (a, b)
                    && emit
                {
                    let rank = proposals.len() + 1;
                    proposals.push(self.proposal(rank, ranked, a, b)?);
                }
            }
            Ok((proposals, total))
        })
    }

    /// The selector of one endpoint, synthesized and cached on first need:
    /// `None` for a declared binding not yet synthesized when `prove` is
    /// false.
    fn endpoint_selector(
        &self,
        synthesizer: &SourceUnitSelectorSynthesizer<'_>,
        selectors: &mut HashMap<usize, EndpointSelector>,
        node_index: usize,
        prove: bool,
    ) -> Result<Option<EndpointSelector>> {
        if let Some(selector) = selectors.get(&node_index) {
            return Ok(Some(selector.clone()));
        }
        let selector = match self.endpoint_plan(node_index) {
            Err(reason) => Err(reason),
            Ok(EndpointPlan::ResolvedMember(member)) => Ok((
                SourceUnitSelector::ResolvedMember(member),
                SelectorOrigin::ResolvedMember,
            )),
            Ok(EndpointPlan::Synthesize(SourceUnitSelectorRequest::DeclaredBinding { .. }))
                if !prove =>
            {
                return Ok(None);
            }
            Ok(EndpointPlan::Synthesize(request)) => match synthesizer.synthesize(&request)? {
                SourceUnitSelectorSynthesis::Synthesized(selector)
                    if self.body_indices[node_index] == Some(selector.body_index) =>
                {
                    Ok((
                        SourceUnitSelector::SourceMatch(selector.claim),
                        SelectorOrigin::SynthesizedSourceMatch,
                    ))
                }
                SourceUnitSelectorSynthesis::Synthesized(selector) => {
                    Err(EndpointSelectorFailure::WrongBodyIndex {
                        actual: selector.body_index,
                        expected: self.body_indices[node_index],
                    })
                }
                SourceUnitSelectorSynthesis::Skipped { reason } => {
                    Err(EndpointSelectorFailure::Synthesis {
                        reason: reason.to_report(),
                    })
                }
            },
        };
        selectors.insert(node_index, selector.clone());
        Ok(Some(selector))
    }

    fn proposal(
        &self,
        rank: usize,
        ranked: RankedCandidate,
        (a_selector, a_origin): (SourceUnitSelector, SelectorOrigin),
        (b_selector, b_origin): (SourceUnitSelector, SelectorOrigin),
    ) -> Result<CommutationProposal> {
        let candidate = ranked.candidate;
        let inerted_edges = candidate
            .inerted
            .iter()
            .map(|id| {
                let edge: &DependencyEdgeReport = &self.report.edges[id.0];
                let cause = edge
                    .sequenced_cause
                    .clone()
                    .with_context(|| format!("inertable edge {} has no retained cause", edge.id))?;
                Ok(InertedEdge {
                    edge: edge.id.clone(),
                    from: edge.source.clone(),
                    to: edge.target.clone(),
                    explanation_text: cause.to_string(),
                    explanation: cause,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(CommutationProposal {
            rank,
            scc_id: candidate.scc_id,
            also_in_sccs: ranked.also_in_sccs,
            cut_edges: candidate.cut_edges,
            rank_reason: if ranked.single_pair_cut {
                RankReason::SinglePairCut
            } else {
                RankReason::PartialCut
            },
            single_pair_cut: ranked.single_pair_cut,
            remaining_module_pair_edges: ranked.remaining_module_pair_edges,
            entry: CommutingStatementsContract {
                a: a_selector,
                b: b_selector,
                note: None,
            },
            a: self.endpoint_report(candidate.a, a_origin)?,
            b: self.endpoint_report(candidate.b, b_origin)?,
            inerted_edges,
        })
    }
}
