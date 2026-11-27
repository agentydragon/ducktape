use crate::realizability::{RealizabilityVerdict, SccDiagnosis, SccRejection};
use analysis::ModuleId;
use analysis_model::DepKind;
use graph::module_assignment::ModuleAssignment;
use graph::{DependencyEdgeId, DependencyGraph, EndpointView, assignment_endpoints};
use petgraph::algo::greedy_feedback_arc_set;
use petgraph::graph::DiGraph;
use render::CYCLE_WITNESS_PREVIEW_STEPS;
use report::{
    AssignmentReport, AssignmentValidation, BlockingRejection, CycleEdge, CycleReport,
    PassOneCycleWitness, explain_edge,
};
use source_identity::SourceUnitOrdinal;
use spec::ModulePath;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use swc_atoms::Atom;

pub(crate) mod render;
pub(crate) mod report;

/// Project the realizability verdict onto the validator's rendered
/// report: one [`CycleReport`] per [`crate::realizability::SccDiagnosis`].
///
/// The verdict is the single source of truth for *which* SCCs block
/// materialization (docs/design.md "Realizability primitive" —
/// "Invariant: no bespoke parallel walks"); this function only
/// decorates each diagnosis's source-unit edge provenance into the
/// stringified, binding-pair-labelled rows spec authors read. In
/// particular, an asymmetric I-cycle that Lemma 2's source-import
/// reversal rescues never appears here, even when another SCC in the
/// same chunk is genuinely unrealizable.
#[cfg(test)]
pub fn validate_assignment(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> AssignmentReport {
    validate_assignment_with_verdict(dependency_graph, assignment, module_path).report
}

/// Validate once and retain both the canonical typed verdict and its rendered
/// report for in-memory consumers that need to pair it with candidate-order
/// analyses. Callers must not infer gate facts from rendered strings.
#[cfg(test)]
pub fn validate_assignment_with_verdict(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> AssignmentValidation {
    let nodes = assignment
        .iter()
        .map(|(_, module)| module)
        .chain(std::iter::once(assignment.entry_destination().module_id()))
        .collect();
    let (verdict, schedule) = crate::realizability::check_realizability_retaining_schedule(
        dependency_graph,
        assignment,
        &nodes,
    )
    .expect("legacy assignment contains exactly its projected module universe");
    validation_from_verdict(
        dependency_graph,
        assignment,
        module_path,
        verdict,
        &schedule,
    )
}

/// Validate exactly the authored shared priority and retain its concrete
/// schedule. A rejected priority is returned as a gate verdict; it is not
/// repaired by searching for another schedule.
pub fn validate_assignment_with_priority(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    nodes: &BTreeSet<ModuleId>,
    priority: &[ModulePath],
) -> Result<
    (AssignmentValidation, esm_schedule::EsmImportSchedule),
    esm_schedule::ImportScheduleError,
> {
    let entry = assignment.entry_destination().module_id();
    let by_path: BTreeMap<_, _> = nodes
        .iter()
        .copied()
        .filter(|&module| module != entry)
        .map(|module| (module_path(module), module))
        .collect();
    if by_path.len() != nodes.len().saturating_sub(1)
        || priority.len() != by_path.len()
        || priority.iter().collect::<BTreeSet<_>>() != by_path.keys().collect()
    {
        return Err(esm_schedule::ImportScheduleError::PriorityPaths {
            expected: by_path.keys().cloned().collect(),
            actual: priority.to_vec(),
        });
    }
    let ids: Vec<_> = priority.iter().map(|path| by_path[path]).collect();
    let (verdict, schedule) = crate::realizability::check_realizability_with_priority(
        dependency_graph,
        assignment,
        nodes,
        &ids,
    )?;
    Ok((
        validation_from_verdict(
            dependency_graph,
            assignment,
            module_path,
            verdict,
            &schedule,
        ),
        schedule,
    ))
}

pub(crate) fn validation_from_verdict(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    verdict: RealizabilityVerdict,
    schedule: &esm_schedule::EsmImportSchedule,
) -> AssignmentValidation {
    let cross_rebinds = render_cross_rebinds(dependency_graph, &verdict, module_path);
    if verdict.unrealizable_sccs.is_empty() {
        return AssignmentValidation {
            verdict,
            report: AssignmentReport {
                cycles: Vec::new(),
                atomic_group_conflicts: Vec::new(),
                linker_order: Vec::new(),
                cross_rebinds,
            },
        };
    }
    // statement_ordinal → first declared binding of the source_unit that
    // owns that statement. Used to label the source side of every
    // CycleEdge so diagnostics blame binding pairs, not just module
    // pairs. An source_unit may have no declared bindings (anonymous
    // statement); we leave `from_binding = None` in that case and let
    // the renderer fall back to a statement-ordinal placeholder.
    let from_binding_by_ordinal: HashMap<SourceUnitOrdinal, Atom> = dependency_graph
        .iter_nodes()
        .filter_map(|node| {
            node.declared
                .iter()
                .next()
                .map(|id| (node.statement_ordinal, id.0.clone()))
        })
        .collect();
    let report =
        |diagnosis: &SccDiagnosis, rejection: BlockingRejection, modules: &BTreeSet<ModuleId>| {
            let (cut, lazy_closure, pass_one_witness) = match diagnosis.rejection {
                SccRejection::MutualConstrainingCycle => {
                    let (cut, witness) = compute_realizability_cut(
                        dependency_graph,
                        assignment,
                        &diagnosis.core.constraining_dependency_edges,
                        module_path,
                        &from_binding_by_ordinal,
                    );
                    (cut, Vec::new(), Some(witness))
                }
                // The diagnosis's source-unit edges ARE the cut: the
                // simulator surfaced exactly the constraining
                // `(from, to)` pairs whose post-order check failed.
                SccRejection::EsmEvaluationTdz => (
                    cycle_edges_for(
                        dependency_graph,
                        assignment,
                        diagnosis.core.constraining_dependency_edges.iter().copied(),
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    nonconstraining_closure_edges(
                        dependency_graph,
                        assignment,
                        modules,
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    None,
                ),
            };
            CycleReport {
                rejection,
                modules: modules.iter().copied().map(module_path).collect(),
                pass_one_witness: if rejection == BlockingRejection::MutualConstrainingCycle {
                    pass_one_witness
                } else {
                    None
                },
                cut,
                lazy_closure,
                inert_commuting_edges: inert_commuting_edges(
                    dependency_graph,
                    assignment,
                    modules,
                    module_path,
                    &from_binding_by_ordinal,
                ),
                commuting_premise_locations: dependency_graph
                    .commuting_premises()
                    .iter()
                    .map(|premise| (premise.reference.clone(), premise.spec_location.clone()))
                    .collect(),
            }
        };
    let mut cycles: Vec<CycleReport> = verdict
        .unrealizable_sccs
        .iter()
        .map(|diagnosis| {
            report(
                diagnosis,
                BlockingRejection::of(diagnosis.rejection),
                &diagnosis.core.modules,
            )
        })
        .collect();
    let pass_one: Vec<&BTreeSet<ModuleId>> = verdict
        .unrealizable_sccs
        .iter()
        .filter(|diagnosis| diagnosis.rejection == SccRejection::MutualConstrainingCycle)
        .map(|diagnosis| &diagnosis.core.modules)
        .collect();
    if !pass_one.is_empty() {
        let collapsed = collapse_modules(dependency_graph, assignment, &pass_one);
        let reported: BTreeSet<&BTreeSet<ModuleId>> = verdict
            .unrealizable_sccs
            .iter()
            .map(|diagnosis| &diagnosis.core.modules)
            .collect();
        let entry = collapsed.assignment.entry_destination().module_id();
        let nodes = schedule
            .modules()
            .into_iter()
            .map(|module| collapsed.representative(module))
            .collect();
        let mut seen = BTreeSet::new();
        let priority: Vec<_> = schedule
            .imports(entry)
            .iter()
            .copied()
            .map(|module| collapsed.representative(module))
            .filter(|&module| module != entry && seen.insert(module))
            .collect();
        let (collapsed_verdict, _) = crate::realizability::check_realizability_with_priority(
            dependency_graph,
            &collapsed.assignment,
            &nodes,
            &priority,
        )
        .expect("contracting a concrete priority retains its exact module universe");
        for diagnosis in collapsed_verdict
            .unrealizable_sccs
            .iter()
            .filter(|diagnosis| diagnosis.rejection == SccRejection::EsmEvaluationTdz)
        {
            let modules = collapsed.expand(&diagnosis.core.modules);
            if !reported.contains(&modules) {
                cycles.push(report(
                    diagnosis,
                    BlockingRejection::TdzAfterCollapsingPassOne,
                    &modules,
                ));
            }
        }
    }
    AssignmentValidation {
        verdict,
        report: AssignmentReport {
            cycles,
            atomic_group_conflicts: Vec::new(),
            linker_order: Vec::new(),
            cross_rebinds,
        },
    }
}

/// `assignment` with every module of each Pass-1 SCC moved into one
/// representative: the entry when the SCC contains it, else its smallest
/// module. Collapsing whole SCCs of the constraining graph leaves it acyclic.
struct CollapsedAssignment {
    assignment: ModuleAssignment,
    members: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    representatives: BTreeMap<ModuleId, ModuleId>,
}

impl CollapsedAssignment {
    fn representative(&self, module: ModuleId) -> ModuleId {
        self.representatives.get(&module).copied().unwrap_or(module)
    }
    fn expand(&self, modules: &BTreeSet<ModuleId>) -> BTreeSet<ModuleId> {
        modules
            .iter()
            .flat_map(|module| {
                self.members
                    .get(module)
                    .cloned()
                    .unwrap_or_else(|| BTreeSet::from([*module]))
            })
            .collect()
    }
}

fn collapse_modules(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    sccs: &[&BTreeSet<ModuleId>],
) -> CollapsedAssignment {
    let entry = assignment.entry_destination().module_id();
    let mut representative = BTreeMap::new();
    let mut members = BTreeMap::new();
    for scc in sccs {
        let target = if scc.contains(&entry) {
            entry
        } else {
            *scc.first().expect("a Pass-1 SCC has at least two modules")
        };
        for &module in *scc {
            representative.insert(module, target);
        }
        members.insert(target, (*scc).clone());
    }
    let of = dependency_graph
        .iter_nodes()
        .map(|node| {
            let module = assignment.of(node.id);
            representative.get(&module).copied().unwrap_or(module)
        })
        .collect();
    CollapsedAssignment {
        assignment: ModuleAssignment::from_assignments(of, assignment.entry_destination()),
        members,
        representatives: representative,
    }
}

/// Render the verdict's clause-2 cross-rebind diagnoses as
/// human-readable lines for [`AssignmentReport::cross_rebinds`].
fn render_cross_rebinds(
    dependency_graph: &DependencyGraph,
    verdict: &RealizabilityVerdict,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> Vec<String> {
    verdict
        .cross_rebinds
        .iter()
        .map(|rebind| {
            let edge = dependency_graph.edge(rebind.dependency_edge);
            let binding = match &edge.reason.binding {
                Some(id) => id.0.to_string(),
                None => format!("<anon stmt #{}>", edge.reason.statement_ordinal.0),
            };
            format!(
                "{} --{}--> {} (binding `{}`)",
                module_path(rebind.from),
                edge.reason.kind.diagnostic_label(),
                module_path(rebind.to),
                binding,
            )
        })
        .collect()
}

/// Project verdict source-unit edge provenance onto rendered [`CycleEdge`]
/// rows, sorted deterministically
/// `(from, to, statement_ordinal, binding, kind)` so test snapshots
/// compare cleanly. Endpoints use the gate view
/// ([`EndpointView::Gate`]) — the same projection the realizability
/// primitive used to produce the edges, so promoted at-init edges
/// keep their cross-module endpoints.
fn cycle_edges_for(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    edge_ids: impl IntoIterator<Item = DependencyEdgeId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let mut out: Vec<CycleEdge> = edge_ids
        .into_iter()
        .map(|edge_id| {
            cycle_edge_for(
                dependency_graph,
                assignment,
                edge_id,
                module_path,
                from_binding_by_ordinal,
            )
        })
        .collect();
    CycleEdge::sort_for_report(&mut out);
    out
}

fn cycle_edge_for(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    edge_id: DependencyEdgeId,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> CycleEdge {
    let edge = dependency_graph.edge(edge_id);
    let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)
        .expect("verdict source-unit edge provenance is cross-module in the gate view");
    CycleEdge {
        from: module_path(from),
        to: module_path(to),
        from_source_unit_id: graph::report::source_unit_key(edge.from),
        to_source_unit_id: graph::report::source_unit_key(edge.to),
        statement_ordinal: edge.reason.statement_ordinal,
        binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
        from_binding: from_binding_by_ordinal
            .get(&edge.reason.statement_ordinal)
            .cloned(),
        kind: edge.reason.kind,
        sequenced_source_unit: graph::report::sequenced_edge_cause(dependency_graph, edge),
        explanation: explain_edge(dependency_graph, edge),
        commuting_premise: edge.reason.commuting_premise().cloned(),
    }
}

/// Cross-module edges between members of `modules` (gate view) that an
/// author `commuting_statements` premise made inert. See
/// [`CycleReport::inert_commuting_edges`].
fn inert_commuting_edges(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    modules: &BTreeSet<ModuleId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let inert_edge_ids = dependency_graph.iter_edges().filter_map(|edge| {
        edge.reason.commuting_premise()?;
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)?;
        (modules.contains(&from) && modules.contains(&to)).then_some(edge.id)
    });
    cycle_edges_for(
        dependency_graph,
        assignment,
        inert_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}

/// Nonconstraining cross-module read edges between members of `modules`, gate
/// view. For a [`SccRejection::EsmEvaluationTdz`] rejection the
/// constraining subgraph alone is acyclic — these are the back-edges
/// closing the I-cycle the ESM evaluation simulator walked.
fn nonconstraining_closure_edges(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    modules: &BTreeSet<ModuleId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let lazy_edge_ids = dependency_graph.iter_edges().filter_map(|edge| {
        if !matches!(edge.reason.kind, DepKind::LazyUse | DepKind::HoistedUse)
            || dependency_graph.node(edge.from).is_none()
            || dependency_graph.node(edge.to).is_none()
        {
            return None;
        }
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)?;
        (modules.contains(&from) && modules.contains(&to)).then_some(edge.id)
    });
    cycle_edges_for(
        dependency_graph,
        assignment,
        lazy_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}

/// Compute a near-minimum cut of realizability-constraining edges
/// whose removal makes a [`SccRejection::MutualConstrainingCycle`]
/// SCC realizable.
///
/// Input is the diagnosis's source-unit edge provenance — every
/// constraining cross-module source_unit edge inside the SCC, in the gate
/// view. The algorithm groups the source-unit edges by their gate-view
/// module pair, builds a `DiGraph` with one edge per pair (strongly
/// connected by construction: the diagnosis's modules are one SCC of
/// the constraining subgraph), and runs
/// `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth, 1993,
/// `O(V + E)`). Every FAS-picked pair contributes its source-unit edges to
/// the cut.
///
/// Soundness: removing every FAS pair makes the SCC's constraining
/// subgraph acyclic, so the surviving cross-module edges have a
/// valid evaluation order — realizable per the docs/design.md
/// realizability theorem. Heuristic-minimum: petgraph's FAS
/// approximates within a constant factor on dense instances.
fn compute_realizability_cut(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    constraining_dependency_edges: &[DependencyEdgeId],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> (Vec<CycleEdge>, PassOneCycleWitness) {
    let mut by_pair: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = BTreeMap::new();
    for &edge_id in constraining_dependency_edges {
        let edge = dependency_graph.edge(edge_id);
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)
            .expect("verdict source-unit edge provenance is cross-module in the gate view");
        by_pair.entry((from, to)).or_default().push(edge_id);
    }

    // Module-pair graph (index-based node ids required by
    // `greedy_feedback_arc_set`). Edge weight carries the
    // `(from, to)` pair so FAS picks map back to `by_pair`.
    let mut pair_graph: DiGraph<ModuleId, (ModuleId, ModuleId)> = DiGraph::new();
    let mut idx_of: HashMap<ModuleId, _> = HashMap::new();
    for &(from, to) in by_pair.keys() {
        for module in [from, to] {
            idx_of
                .entry(module)
                .or_insert_with(|| pair_graph.add_node(module));
        }
    }
    for &(from, to) in by_pair.keys() {
        pair_graph.add_edge(idx_of[&from], idx_of[&to], (from, to));
    }

    let mut cut_edge_ids: Vec<DependencyEdgeId> = Vec::new();
    for fas_edge in greedy_feedback_arc_set(&pair_graph) {
        cut_edge_ids.extend_from_slice(&by_pair[fas_edge.weight()]);
    }
    // Anchor at a deterministic edge of the actionable cut. The verdict's
    // Pass-1 SCC is strongly connected, so a reciprocal path exists through
    // these same active constraining pairs.
    let anchor = *cut_edge_ids
        .iter()
        .min()
        .expect("a Pass-1 SCC has a nonempty feedback cut");
    let anchor_edge = dependency_graph.edge(anchor);
    let (anchor_from, anchor_to) =
        assignment_endpoints(anchor_edge, assignment, EndpointView::Gate)
            .expect("cut edge is cross-module in the gate view");
    let mut queue = VecDeque::from([anchor_to]);
    let mut seen = BTreeSet::from([anchor_to]);
    let mut predecessor: BTreeMap<ModuleId, (ModuleId, DependencyEdgeId)> = BTreeMap::new();
    while let Some(from) = queue.pop_front() {
        if from == anchor_from {
            break;
        }
        // Reuse the pair graph rather than retaining another adjacency map.
        // Its edges were inserted in BTreeMap order, so traversal is repeatable.
        for pair_edge in pair_graph.edges(idx_of[&from]) {
            let pair = pair_edge.weight();
            let to = pair.1;
            if seen.insert(to) {
                let edge_id = *by_pair[pair].iter().min().expect("a pair has an edge");
                predecessor.insert(to, (from, edge_id));
                queue.push_back(to);
            }
        }
    }
    assert!(
        seen.contains(&anchor_from),
        "Pass-1 verdict edge has no reciprocal path inside its SCC"
    );
    let mut return_edge_ids = Vec::new();
    let mut at = anchor_from;
    while at != anchor_to {
        let &(previous, edge_id) = predecessor
            .get(&at)
            .expect("BFS path has a predecessor until its start");
        return_edge_ids.push(edge_id);
        at = previous;
    }
    return_edge_ids.reverse();
    let witness_steps: Vec<CycleEdge> = std::iter::once(anchor)
        .chain(return_edge_ids)
        .map(|edge_id| {
            cycle_edge_for(
                dependency_graph,
                assignment,
                edge_id,
                module_path,
                from_binding_by_ordinal,
            )
        })
        .collect();
    let witness = PassOneCycleWitness {
        complete: true,
        text_preview_omitted_steps: witness_steps
            .len()
            .saturating_sub(CYCLE_WITNESS_PREVIEW_STEPS),
        steps: witness_steps,
    };
    let cut = cycle_edges_for(
        dependency_graph,
        assignment,
        cut_edge_ids,
        module_path,
        from_binding_by_ordinal,
    );
    (cut, witness)
}
