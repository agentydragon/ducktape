use super::*;

/// How a `commuting_statements` premise over an edge's two endpoints may
/// treat that edge. Decided once per edge by [`premise_verdict`] when the
/// graph is built and retained on the edge (`EdgeReason::premise_verdict`,
/// `dependency_graph.json.gz` `edges[].premise_verdict`), so the gate applying
/// a premise and `spec propose-commutations` drafting one read the same
/// classification.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum PremiseVerdict {
    /// The premise may make the edge inert.
    Inertable,
    /// The analysis already knows the two effects are unordered; the edge
    /// exists only as strict-chain adjacency.
    Summarized,
    /// The analysis derived a modeled conflict the premise contradicts.
    Known { conflict: KnownConflict },
}

/// Classify one dependency edge between the two source units of a premise
/// from its retained cause and the endpoints' recorded effects. This is the
/// soundness boundary of `commuting_statements`:
///
/// - `CoDeclaration` is derived from syntax: known.
/// - `SharedVarAccess`, `BindingTimeline` and `UninitializedObservation`
///   order two effects on a named binding: known when both effects are
///   `EffectProvenance::Observed`; inertable when either was only assumed by
///   a conservative path (an opaque effect, like a barrier).
///   Derived order routed through an assumed effect is retained separately
///   (`promote_at_init_calls` offers the observed-only timeline too).
/// - `CellOrder` with an explicit relation (`ReadAfterWrite`,
///   `WriteAfterWrite`, `WriteAfterRead`, `UseAfterAllocation`) is derived
///   from recorded effects on both sides, whatever each side's class: known.
///   A `CellOrder` edge carrying only barrier relations (`AfterBarrier`,
///   `BarrierAfterPrior`, `FrontierAfterCell`) exists because one side's
///   effects are not summarizable; that is what the premise certifies.
/// - `StrictChain` consults no cells, so the endpoints' facts decide: an
///   unsummarizable endpoint (`havoc` non-empty) makes the edge
///   inertable; two summarized endpoints are already decided by the
///   analysis and the premise buys nothing (`Summarized`).
/// - Before accepting any edge, the two recorded summaries are compared:
///   a shared cell with a write is a known conflict even when the emitting
///   rule did not name it (a barrier target's own recorded effects).
///   `StatementFacts::effects` holds only syntax-derived cells, never a
///   fallback contribution.
/// - A missing cause is treated as known, so the premise never widens
///   through an unclassified edge.
///
/// The premise cell view also includes source-known callee heap accesses on
/// non-assumed invocation paths. Assumed callback and fallback cells remain
/// conservative ordering barriers, not contradictions to author assertions.
pub(super) fn premise_verdict(
    edge: &DependencyEdge,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    premise_effects: &HashMap<SourceUnitId, StatementEffectSummary>,
) -> Option<PremiseVerdict> {
    let from = stmt_by_source_unit[&edge.from];
    let to = stmt_by_source_unit[&edge.to];
    let known = |conflict| PremiseVerdict::Known { conflict };
    let verdict = match edge.reason.kind {
        DepKind::CoDeclaration => known(KnownConflict::CoDeclaration {
            binding: edge
                .reason
                .binding()
                .expect("co-declaration edges name their binding")
                .0
                .clone(),
        }),
        DepKind::Sequenced => match edge.reason.sequenced_cause() {
            None => known(KnownConflict::Unclassified),
            Some(
                cause @ (SequencedCause::SharedVarAccess { .. }
                | SequencedCause::BindingTimeline { .. }
                | SequencedCause::UninitializedObservation { .. }),
            ) => {
                if binding_effects_observed(cause) {
                    known(KnownConflict::Sequenced(cause.clone()))
                } else {
                    PremiseVerdict::Inertable
                }
            }
            Some(cause @ SequencedCause::CellOrder { conflicts, .. }) => {
                let explicit = conflicts.iter().any(|conflict| {
                    matches!(
                        conflict,
                        CellConflict::ReadAfterWrite { .. }
                            | CellConflict::WriteAfterWrite { .. }
                            | CellConflict::WriteAfterRead { .. }
                            | CellConflict::UseAfterAllocation { .. }
                    )
                });
                if explicit {
                    known(KnownConflict::Sequenced(cause.clone()))
                } else {
                    PremiseVerdict::Inertable
                }
            }
            Some(SequencedCause::StrictChain) => {
                if from.dataflow_summarizable() && to.dataflow_summarizable() {
                    PremiseVerdict::Summarized
                } else {
                    PremiseVerdict::Inertable
                }
            }
            Some(SequencedCause::PreservedOrder { .. }) => PremiseVerdict::Inertable,
        },
        _ => return None,
    };
    if matches!(verdict, PremiseVerdict::Known { .. }) {
        return Some(verdict);
    }
    // Recorded cells are facts even on an unsummarizable statement (its
    // unknown effects come on top), so a shared written cell wins.
    Some(
        conflicting_cell(&premise_effects[&edge.from], &premise_effects[&edge.to]).map_or(
            verdict,
            |overlap| {
                known(KnownConflict::SharedCell {
                    cell: EffectCellReport::from(overlap.right()),
                    overlap: CellOverlapReport::from(&overlap),
                })
            },
        ),
    )
}

/// Retain [`premise_verdict`] on every edge it classifies, so a premise
/// applied now ([`apply_commuting_statements`]) and one drafted later from
/// `dependency_graph.json.gz` read the same classification.
pub(super) fn retain_premise_verdicts(
    edges: &mut [DependencyEdge],
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    premise_effects: &HashMap<SourceUnitId, StatementEffectSummary>,
) {
    for edge in edges.iter_mut() {
        edge.reason.premise_verdict =
            premise_verdict(edge, stmt_by_source_unit, premise_effects).map(Box::new);
    }
}

/// An overlapping cell footprint where one summary writes and the other reads
/// or writes, if any.
pub(super) fn conflicting_cell(
    a: &StatementEffectSummary,
    b: &StatementEffectSummary,
) -> Option<CellOverlap> {
    for left in &a.writes {
        if let Some(overlap) = first_overlapping(&b.writes, left)
            && let Some(conflict) = may_conflict(left, true, overlap.left(), true)
        {
            return Some(conflict);
        }
        if let Some(overlap) = first_overlapping(&b.reads, left)
            && let Some(conflict) = may_conflict(left, true, overlap.left(), false)
        {
            return Some(conflict);
        }
    }
    for left in &a.reads {
        if let Some(overlap) = first_overlapping(&b.writes, left)
            && let Some(conflict) = may_conflict(left, false, overlap.left(), true)
        {
            return Some(conflict);
        }
    }
    None
}

/// Apply the chunk's `commuting_statements` premises (SPEC.md § Commuting
/// statements). For each pair, every `Sequenced` edge between the two source
/// units (either direction) becomes inert — unless the edge's retained
/// [`PremiseVerdict`] is a modeled conflict, in which case the entry
/// contradicts a source fact and the build fails. `EagerUse` edges are never
/// touched: an at-init read is a dataflow dependence, not an ordering of
/// effects. A pair with no inertable edge is unused: it is returned as a
/// warning and leaves the graph exactly as without the entry.
pub(super) fn apply_commuting_statements(
    nodes: &[SourceUnit],
    edges: &mut Vec<DependencyEdge>,
    commuting_statements: &[ResolvedCommutingStatements],
    premise_effects: &HashMap<SourceUnitId, StatementEffectSummary>,
) -> Result<(Vec<AppliedCommutingPremise>, Vec<CommutingStatementsUnused>), DependencyGraphBuildError>
{
    let mut applied = Vec::with_capacity(commuting_statements.len());
    let mut summarized_endpoints = Vec::with_capacity(commuting_statements.len());
    for premise in commuting_statements {
        let a = SourceUnitId(premise.a.source_unit.0);
        let b = SourceUnitId(premise.b.source_unit.0);
        let mut conflicts = Vec::new();
        let mut inertable = Vec::new();
        let mut summarized = false;
        for edge in edges.iter() {
            if !((edge.from == a && edge.to == b) || (edge.from == b && edge.to == a)) {
                continue;
            }
            match edge.reason.premise_verdict() {
                None => {}
                Some(PremiseVerdict::Inertable) => inertable.push(edge.id),
                Some(PremiseVerdict::Summarized) => summarized = true,
                Some(PremiseVerdict::Known { conflict }) => conflicts.push(ContradictingEdge {
                    from: SourceUnitOrdinal(edge.from.0),
                    to: SourceUnitOrdinal(edge.to.0),
                    conflict: conflict.clone(),
                }),
            }
        }
        // A pair may first acquire a direct edge during order preservation.
        // Its explicit cells still contradict commutation even before then.
        if conflicts.is_empty()
            && let Some(overlap) = conflicting_cell(&premise_effects[&a], &premise_effects[&b])
        {
            conflicts.push(ContradictingEdge {
                from: premise.a.source_unit,
                to: premise.b.source_unit,
                conflict: KnownConflict::SharedCell {
                    cell: EffectCellReport::from(overlap.right()),
                    overlap: CellOverlapReport::from(&overlap),
                },
            });
        }
        if !conflicts.is_empty() {
            return Err(DependencyGraphBuildError::CommutingStatementsContradicted(
                CommutingStatementsContradiction {
                    spec_location: premise.spec_location.clone(),
                    a: premise.a.source_unit,
                    a_binding: premise.a.selected_binding.clone(),
                    b: premise.b.source_unit,
                    b_binding: premise.b.selected_binding.clone(),
                    conflicts,
                },
            ));
        }
        for id in &inertable {
            edges[id.0].reason.inerted_by = Some(premise.reference.clone());
        }
        summarized_endpoints.push(summarized);
        applied.push(AppliedCommutingPremise {
            reference: premise.reference.clone(),
            spec_location: premise.spec_location.clone(),
            a,
            b,
            inerted_edges: inertable,
            note: premise.note.clone(),
        });
    }
    if !applied.is_empty() {
        preserve_commuting_order(nodes, edges, &mut applied);
    }
    // An assertion can discharge an edge exposed by another assertion, in
    // either spec order. Only the completed closure decides whether it was used.
    let mut used = Vec::with_capacity(applied.len());
    let mut unused = Vec::new();
    for ((premise, result), summarized) in commuting_statements
        .iter()
        .zip(applied)
        .zip(summarized_endpoints)
    {
        if !result.inerted_edges.is_empty() {
            used.push(result);
            continue;
        }
        unused.push(CommutingStatementsUnused {
            spec_location: premise.spec_location.clone(),
            a: UnusedCommutingEndpoint::new(nodes, &premise.a),
            b: UnusedCommutingEndpoint::new(nodes, &premise.b),
            reason: if summarized {
                UnusedPremiseReason::SummarizedEndpoints
            } else {
                UnusedPremiseReason::NoSequencedEdge
            },
        });
    }
    Ok((used, unused))
}

/// Preserve third-statement paths using the same closure as proposal queries.
pub(super) fn preserve_commuting_order(
    nodes: &[SourceUnit],
    edges: &mut Vec<DependencyEdge>,
    applied: &mut [AppliedCommutingPremise],
) {
    use crate::graph::CommutationOrder;
    use std::ops::ControlFlow;

    let pairs: Vec<_> = applied
        .iter()
        .map(|premise| (premise.a, premise.b))
        .collect();
    let order = CommutationOrder::from_edges(
        nodes.len(),
        edges
            .iter()
            .map(|edge| (edge, edge.reason.premise_verdict())),
        applied,
    );
    order.preserve(&pairs, |preserved| {
        let id = DependencyEdgeId(edges.len());
        let mut reason = EdgeReason::sequenced(
            nodes[preserved.from.0].statement_ordinal,
            SequencedCause::PreservedOrder {
                via: nodes[preserved.via.0].statement_ordinal,
                commuting_premise: applied[preserved.by_premise].reference.clone(),
            },
        );
        if let Some(index) = preserved.inerted_by {
            reason.inerted_by = Some(applied[index].reference.clone());
            applied[index].inerted_edges.push(id);
        }
        edges.push(DependencyEdge {
            id,
            from: preserved.from,
            to: preserved.to,
            reason,
        });
        ControlFlow::Continue(())
    });
}
