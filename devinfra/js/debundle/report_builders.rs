//! dependency graph report builders: project a [`AssignmentAnalysis`]
//! into the wire-format `DependencyGraphReport` family defined in
//! `analysis::reports::schema`. The wire-stable key codecs
//! (`source_unit_key`, `module_key`, …) live in `analysis::reports`.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use rayon::prelude::*;
use swc_ecma_ast::Id;

use analysis::graph::EdgeRole;
use analysis::reports::schema::{
    CallbackFactoryCallbackSourceReport, CallbackFactoryUseReport, ImportedCallableSummaryReport,
    LineRange, ReturnedRecordUseReport,
};
use analysis::reports::{atomic_group_key, module_id_from_key, module_key, source_unit_key};
use analysis::{
    AtomicGraphReport, AtomicGroup, AtomicGroupEdgeReport, AtomicGroupReport, BindingId,
    BindingReport, CallbackFactoryBarrierRejection, CallbackFactoryBarrierReport,
    CallbackFactoryCallArgumentWitnessReport, CallbackFactoryContractReport,
    CallbackFactoryRejectionReport, CallbackFactoryWriteWitnessReport, DepKind,
    DependencyEdgeReport, DependencyGraphReport, EdgeRoleReport, ModuleDestinationKind,
    ModuleEntry, ModuleId, ModuleKey, ModuleQuotientReport, ModuleRole, Purity, PurityCauseReport,
    QuotientEdgeReport, QuotientSccReport, SourceUnitClass, SourceUnitReport,
};
use source_identity::SourceUnitOrdinal;
use swc_atoms::Atom;

use crate::assignment_analysis::AssignmentAnalysis;

#[derive(Debug, Clone, Default)]
struct AtomicEdgeAccumulator {
    kinds: BTreeSet<DepKind>,
    dependency_edge_ids: Vec<String>,
    constrains_init_order: bool,
}

pub(crate) fn build_dependency_graph_report(
    assignment_analysis: &AssignmentAnalysis,
) -> DependencyGraphReport {
    // The three sub-reports below — `(quotient_nodes, quotient_edges)`,
    // `nodes` + `edges`, and `atomic_graph` — share no mutable state
    // and individually account for tens of seconds on chunks with
    // thousands of source units. Run them concurrently via `rayon::join`
    // so wall-clock collapses to roughly the slowest one. SCC
    // construction depends on `quotient_edges`, so it runs after the
    // join with the now-ready edge list.
    let (quotient_pair, (nodes_edges, atomic_graph)) = rayon::join(
        || {
            rayon::join(
                || build_quotient_node_reports(assignment_analysis),
                || build_quotient_edge_reports(assignment_analysis),
            )
        },
        || {
            rayon::join(
                || build_source_unit_nodes_and_edges(assignment_analysis),
                || build_atomic_graph_report(assignment_analysis),
            )
        },
    );
    let (quotient_nodes, quotient_edges) = quotient_pair;
    let (mut nodes, edges) = nodes_edges;
    let purity_causes = collect_purity_causes(&mut nodes);
    let mut imported_callable_summaries = BTreeMap::<String, (Vec<_>, Option<_>)>::new();
    for source_unit in assignment_analysis.inputs().dependency_graph().iter_nodes() {
        for proof_use in &source_unit.callback_factory_uses {
            let entry = imported_callable_summaries
                .entry(proof_use.imported_local.clone())
                .or_default();
            assert!(entry.0.is_empty() || entry.0 == proof_use.evidence);
            entry.0 = proof_use.evidence.clone();
        }
        for proof_use in &source_unit.returned_record_uses {
            let entry = imported_callable_summaries
                .entry(proof_use.imported_local.clone())
                .or_default();
            assert!(
                entry
                    .1
                    .as_ref()
                    .is_none_or(|evidence| evidence == &proof_use.evidence)
            );
            entry.1 = Some(proof_use.evidence.clone());
        }
    }
    let imported_callable_summaries = imported_callable_summaries
        .into_iter()
        .map(
            |(imported_local, (callback_factory_evidence, returned_record))| {
                ImportedCallableSummaryReport {
                    id: format!("imported_callable:{imported_local}"),
                    imported_local,
                    callback_factory_evidence,
                    returned_record,
                }
            },
        )
        .collect();
    let quotient_sccs =
        build_quotient_scc_reports(assignment_analysis, &quotient_nodes, &quotient_edges);
    let mut native_integrity_witnesses = assignment_analysis
        .inputs()
        .dependency_graph()
        .iter_nodes()
        .next()
        .map(|source_unit| source_unit.native_integrity_witnesses.as_ref().clone())
        .unwrap_or_default();
    for witness in &mut native_integrity_witnesses {
        if witness.chunk_id.is_empty() {
            witness.chunk_id = assignment_analysis.inputs().chunk_id().to_string();
        }
    }
    let mut native_receiver_certificates = assignment_analysis
        .inputs()
        .dependency_graph()
        .iter_nodes()
        .next()
        .map(|source_unit| source_unit.native_receiver_certificates.as_ref().clone())
        .unwrap_or_default();
    for certificate in &mut native_receiver_certificates {
        if certificate.chunk_id.is_empty() {
            certificate.chunk_id = assignment_analysis.inputs().chunk_id().to_string();
        }
    }
    let mut native_prototype_mutations = assignment_analysis
        .inputs()
        .dependency_graph()
        .iter_nodes()
        .next()
        .map(|source_unit| source_unit.native_prototype_mutations.as_ref().clone())
        .unwrap_or_default();
    for mutation in &mut native_prototype_mutations {
        if mutation.chunk_id.is_empty() {
            mutation.chunk_id = assignment_analysis.inputs().chunk_id().to_string();
        }
    }
    DependencyGraphReport {
        native_prototype_mutations,
        native_integrity_witnesses,
        native_receiver_certificates,
        provenance: None,
        chunk_id: assignment_analysis.inputs().chunk_id().to_string(),
        nodes,
        purity_causes,
        imported_callable_summaries,
        edges,
        quotient: ModuleQuotientReport {
            nodes: quotient_nodes,
            edges: quotient_edges,
            sccs: quotient_sccs,
        },
        atomic_graph,
    }
}

/// Serialize the immutable call-cause DAG once per report. Each call-site
/// reason keeps its own location and points to a shared callee verdict;
/// traversing the table yields the full chain without duplicating it at
/// every caller. Pointer identity is stable for the lifetime of this build,
/// and IDs are assigned in deterministic source unit/reason traversal order.
fn collect_purity_causes(nodes: &mut [SourceUnitReport]) -> Vec<PurityCauseReport> {
    let mut ids = HashMap::<usize, String>::new();
    let mut causes = Vec::new();
    for node in nodes {
        attach_cause_refs(&mut node.purity, &mut ids, &mut causes);
    }
    causes
}

fn attach_cause_refs(
    purity: &mut Purity,
    ids: &mut HashMap<usize, String>,
    causes: &mut Vec<PurityCauseReport>,
) {
    let Purity::NotPure { reasons } = purity else {
        return;
    };
    for reason in reasons {
        let Some(cause) = &reason.cause else {
            continue;
        };
        let ptr = std::sync::Arc::as_ptr(cause) as usize;
        if let Some(id) = ids.get(&ptr) {
            reason.cause_ref = Some(id.clone());
            continue;
        }
        let id = format!("purity_cause:{}", causes.len());
        ids.insert(ptr, id.clone());
        let index = causes.len();
        causes.push(PurityCauseReport {
            id: id.clone(),
            purity: Purity::pure(),
        });
        let mut callee = (**cause).clone();
        attach_cause_refs(&mut callee, ids, causes);
        causes[index].purity = callee;
        reason.cause_ref = Some(id);
    }
}

/// Build the per-source-unit node report and the per-edge edge report
/// together. Both walks are independent, but bundling them lets the
/// rayon `join` split four ways without an explicit `scope` and keeps
/// the assignment-of()/export-name-for()/etc.\ touches paired in cache.
fn build_source_unit_nodes_and_edges(
    assignment_analysis: &AssignmentAnalysis,
) -> (Vec<SourceUnitReport>, Vec<DependencyEdgeReport>) {
    let assignment = assignment_analysis.assignment();
    let dependency_graph = assignment_analysis.inputs().dependency_graph();
    let source_units_by_ordinal = dependency_graph
        .iter_nodes()
        .map(|source_unit| (source_unit.statement_ordinal, source_unit))
        .collect::<BTreeMap<SourceUnitOrdinal, _>>();
    let nodes = dependency_graph
        .iter_nodes()
        .map(|node| SourceUnitReport {
            id: source_unit_key(node.id),
            statement_ordinal: node.statement_ordinal,
            source_location: node.source_location.clone(),
            declared_bindings: binding_reports(assignment_analysis, node.declared.iter()),
            statement_kind: node.kind,
            purity: node.purity.clone(),
            heap_native_integrity_uses: node.heap_native_integrity_uses.clone(),
            synchronous_execution: node.synchronous_execution.as_ref().map(|execution| {
                let mut report = execution.clone();
                report.callback_factory_uses = node.callback_factory_uses.iter().map(|proof_use| {
                    CallbackFactoryUseReport {
                        summary_ref: format!("imported_callable:{}", proof_use.imported_local),
                        kind: proof_use.kind,
                        lexical_statement_ordinal: proof_use.lexical_statement_ordinal,
                        source_location: proof_use.source_location.clone(),
                    }
                }).collect();
                report.returned_record_uses = node.returned_record_uses.iter().map(|proof_use| {
                    ReturnedRecordUseReport {
                        summary_ref: format!("imported_callable:{}", proof_use.imported_local),
                        kind: proof_use.kind, lexical_statement_ordinal: proof_use.lexical_statement_ordinal,
                        source_location: proof_use.source_location.clone(),
                        native_integrity_value_inputs: proof_use
                            .evidence
                            .native_integrity_value_inputs
                            .clone(),
                    }
                }).collect();
                report
            }),
            callback_factory_barriers: node
                .callback_factory_barriers
                .iter()
                .map(|barrier| {
                    let witness_report = |witness: &analysis::CallbackFactoryWriteWitness| {
                        let witness_source = source_units_by_ordinal
                            .get(&witness.statement_ordinal)
                            .expect("callback factory witness must identify a graph source unit");
                        CallbackFactoryWriteWitnessReport {
                            source_unit: source_unit_key(witness_source.id),
                            statement_ordinal: witness.statement_ordinal,
                            source_location: witness_source.source_location.clone(),
                            receiver_binding: Atom::from(witness.receiver_binding.as_str()),
                            property: witness.property.clone(),
                        }
                    };
                    let rejection = match &barrier.rejection {
                        CallbackFactoryBarrierRejection::NamespaceMemberWrite { witness } => {
                            CallbackFactoryRejectionReport::NamespaceMemberWrite {
                                witness: witness_report(witness),
                            }
                        }
                        CallbackFactoryBarrierRejection::NamespaceDynamicPropertyWrite {
                            witness,
                        } => CallbackFactoryRejectionReport::NamespaceDynamicPropertyWrite {
                            witness: witness_report(witness),
                        },
                        CallbackFactoryBarrierRejection::NamespaceCallArgument { witness } => {
                            let witness_source = source_units_by_ordinal
                                .get(&witness.statement_ordinal)
                                .expect("call argument witness must identify a graph source unit");
                            let opaque_call = witness_source.synchronous_execution.as_ref()
                                .is_some_and(|execution| execution.precision_losses.iter().any(|loss| {
                                    loss.is_unknown_call_target_at(witness.statement_ordinal, witness.call_span)
                                }));
                            CallbackFactoryRejectionReport::NamespaceCallArgument {
                                witness: CallbackFactoryCallArgumentWitnessReport {
                                    source_unit: source_unit_key(witness_source.id),
                                    statement_ordinal: witness.statement_ordinal,
                                    source_location: witness_source.source_location.clone(),
                                    namespace_binding: Atom::from(witness.namespace_binding.as_str()),
                                    call_span_start: witness.call_span.lo.0,
                                    call_span_end: witness.call_span.hi.0,
                                    argument_index: witness.argument_index,
                                    opaque_call,
                                },
                            }
                        }
                        CallbackFactoryBarrierRejection::UnsupportedGuard => {
                            CallbackFactoryRejectionReport::UnsupportedGuard
                        }
                    };
                    CallbackFactoryBarrierReport {
                        contract: CallbackFactoryContractReport {
                            namespace_binding: Atom::from(barrier.namespace_binding.as_str()),
                            member: barrier.member.clone(),
                            argument_count: barrier.argument_count,
                            callback_arguments: barrier.callback_arguments.clone(),
                            callback_source_units: barrier.callback_source_units.as_ref().map(
                                |ordinals| {
                                    ordinals
                                        .iter()
                                        .map(|ordinal| {
                                            let source_unit = source_units_by_ordinal
                                                .get(ordinal)
                                                .expect(
                                                    "callback source must identify a graph source unit",
                                                );
                                            CallbackFactoryCallbackSourceReport {
                                                source_unit: source_unit_key(source_unit.id),
                                                statement_ordinal: *ordinal,
                                            }
                                        })
                                        .collect()
                                },
                            ),
                        },
                        rejection,
                    }
                })
                .collect(),
            destination: module_key(assignment.of(node.id)),
        })
        .collect();
    let edges = dependency_graph
        .iter_edges()
        .map(|edge| DependencyEdgeReport {
            id: edge.id.report_key(),
            source: source_unit_key(edge.from),
            target: source_unit_key(edge.to),
            edge_kind: edge.reason.kind,
            binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
            statement_ordinal: edge.reason.statement_ordinal,
            constrains_init_order: edge.reason.constrains_init_order(),
            role: edge_role_report(edge.reason.role()),
        })
        .collect();
    (nodes, edges)
}

fn binding_reports<'a, I>(
    assignment_analysis: &AssignmentAnalysis,
    bindings: I,
) -> Vec<BindingReport>
where
    I: IntoIterator<Item = &'a Id>,
{
    bindings
        .into_iter()
        .map(|id| BindingReport {
            binding: id.0.clone(),
            export_name: assignment_analysis
                .inputs()
                .export_name_for(&BindingId::new(
                    assignment_analysis.inputs().chunk_identity(),
                    id.clone(),
                ))
                .expect("dependency graph bindings belong to the analysis chunk"),
        })
        .collect()
}

fn build_quotient_node_reports(assignment_analysis: &AssignmentAnalysis) -> Vec<ModuleEntry> {
    let mut modules = BTreeSet::<ModuleId>::new();
    for idx in 0..assignment_analysis.inputs().planned_modules().len() {
        modules.insert(ModuleId(idx));
    }
    modules.insert(
        assignment_analysis
            .assignment()
            .entry_destination()
            .module_id(),
    );
    for (_, module) in assignment_analysis.assignment().iter() {
        modules.insert(module);
    }
    for (from, to) in assignment_analysis.module_quotient().all_edges() {
        modules.insert(from);
        modules.insert(to);
    }
    modules
        .into_iter()
        .map(|id| module_entry(assignment_analysis, id))
        .collect()
}

fn build_quotient_edge_reports(
    assignment_analysis: &AssignmentAnalysis,
) -> Vec<QuotientEdgeReport> {
    // Use a `HashMap` for the per-(from,to) accumulator — quotient
    // edges count in the tens of thousands and `BTreeMap::entry` is
    // `O(log n)` per insert. The output ordering still has to match
    // the previous `BTreeMap` traversal (ascending `(from, to)`), so
    // we sort once at the end. `kinds` likewise: build into a
    // `BTreeSet` per pair (small — at most one entry per `DepKind`
    // variant) so the final `edge_kinds: Vec<DepKind>` stays sorted
    // without a per-pair sort.
    let assignment = assignment_analysis.assignment();
    let dependency_graph = assignment_analysis.inputs().dependency_graph();
    let mut accum: HashMap<(ModuleId, ModuleId), QuotientEdgeAccumulator> =
        HashMap::with_capacity(dependency_graph.num_edges());
    let mut seen_side_effect_module_pairs: HashSet<(ModuleId, ModuleId)> = HashSet::new();
    for edge in dependency_graph.iter_edges() {
        // Same assignment view every other quotient consumer uses; see
        // `assignment_endpoints` invariant doc.
        let Some((from, to)) = analysis::graph::assignment_endpoints(
            edge,
            assignment,
            analysis::graph::EndpointView::Lenient,
        ) else {
            continue;
        };
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        let entry = accum.entry((from, to)).or_default();
        entry.kinds.insert(edge.reason.kind);
        entry.constrains_init_order |= edge.reason.constrains_init_order();
    }
    let mut pairs: Vec<((ModuleId, ModuleId), QuotientEdgeAccumulator)> =
        accum.into_iter().collect();
    pairs.sort_by_key(|((from, to), _)| (*from, *to));
    pairs
        .into_iter()
        .enumerate()
        .map(|(idx, ((from, to), entry))| QuotientEdgeReport {
            id: format!("module_edge:{idx}"),
            source: module_key(from),
            target: module_key(to),
            edge_kinds: entry.kinds.into_iter().collect(),
            constrains_init_order: entry.constrains_init_order,
        })
        .collect()
}

#[derive(Debug, Clone, Default)]
struct QuotientEdgeAccumulator {
    kinds: BTreeSet<DepKind>,
    constrains_init_order: bool,
}

fn build_atomic_group_report(
    assignment_analysis: &AssignmentAnalysis,
    idx: usize,
    atomic_group: &AtomicGroup,
) -> AtomicGroupReport {
    let mut source_unit_ids = Vec::with_capacity(atomic_group.members.len());
    let mut members = Vec::new();
    let mut anonymous_statement_source_unit_ids = Vec::new();
    // Destinations dedup-by-key: tiny set per group (rare to exceed
    // a handful), so a Vec + linear-scan dedup is cheaper than a
    // `BTreeMap` allocation per group.
    let mut destinations: Vec<ModuleKey> = Vec::new();
    // `atomic_group.causes` is a `BTreeSet<DepKind>` — iteration is already
    // `DepKind`-`Ord`-stable so no post-collection sort is needed.
    let causes: Vec<DepKind> = atomic_group.causes.iter().copied().collect();
    let mut line_range = LineRange::new();
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    for source_unit_id in &atomic_group.members {
        source_unit_ids.push(source_unit_key(*source_unit_id));
        if let Some(node) = assignment_analysis
            .inputs()
            .dependency_graph()
            .node(*source_unit_id)
        {
            if node.class() == SourceUnitClass::AnonymousStatement {
                anonymous_statement_source_unit_ids.push(source_unit_key(*source_unit_id));
            }
            members.extend(binding_reports(assignment_analysis, node.declared.iter()));
            if let Some(location) = &node.source_location {
                line_range.expand(location);
            }
            min_ordinal = min_ordinal.min(node.statement_ordinal.0);
            max_ordinal = max_ordinal.max(node.statement_ordinal.0);
            let destination = module_key(assignment_analysis.assignment().of(*source_unit_id));
            if !destinations.contains(&destination) {
                destinations.push(destination);
            }
        }
    }
    members.sort();
    members.dedup();
    anonymous_statement_source_unit_ids.sort();
    destinations.sort();
    AtomicGroupReport {
        id: atomic_group_key(idx),
        source_unit_ids,
        members,
        anonymous_statement_source_unit_ids,
        destinations,
        causes,
        size_lines_estimate: line_range.size_estimate(),
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
    }
}

fn build_atomic_graph_report(assignment_analysis: &AssignmentAnalysis) -> AtomicGraphReport {
    let dependency_graph = assignment_analysis.inputs().dependency_graph();
    let mut atomic_groups = assignment_analysis.atomic_groups().to_vec();
    atomic_groups.sort_by_key(|atomic_group| {
        atomic_group
            .members
            .iter()
            .copied()
            .min()
            .map(|source_unit| source_unit.0)
    });
    // `atomic_group_by_source_unit` is lookup-only after construction — a `HashMap`
    // is O(1) per get vs `BTreeMap`'s O(log n) and the per-edge loop
    // below hits it 2× per edge across ~26K edges on big chunks.
    let source_unit_count = dependency_graph.num_nodes();
    let mut atomic_group_by_source_unit: Vec<Option<usize>> = vec![None; source_unit_count];
    for (atomic_group_idx, atomic_group) in atomic_groups.iter().enumerate() {
        for source_unit in &atomic_group.members {
            if let Some(slot) = atomic_group_by_source_unit.get_mut(source_unit.0) {
                *slot = Some(atomic_group_idx);
            }
        }
    }

    // Per-group reports are independent; parallelise via rayon so the
    // big-chunk case (thousands of groups) saturates worker threads.
    let nodes = atomic_groups
        .par_iter()
        .enumerate()
        .map(|(idx, atomic_group)| {
            build_atomic_group_report(assignment_analysis, idx, atomic_group)
        })
        .collect();

    // Per-(from_group, to_group) accumulator: HashMap keyed by the
    // group pair (cheaper than BTreeMap for the ~tens-of-thousands
    // edges across thousands of groups). source-unit-edge IDs accumulate
    // into a Vec — dedup is unnecessary because each `DependencyEdgeId`
    // is unique per source unit edge. Order is restored by a single final
    // sort.
    let mut accum: HashMap<(usize, usize), AtomicEdgeAccumulator> = HashMap::new();
    for edge in dependency_graph.iter_edges() {
        if matches!(edge.reason.kind, DepKind::LazyUse | DepKind::HoistedUse) {
            continue;
        }
        let (Some(from_group), Some(to_group)) = (
            atomic_group_by_source_unit
                .get(edge.from.0)
                .copied()
                .flatten(),
            atomic_group_by_source_unit
                .get(edge.to.0)
                .copied()
                .flatten(),
        ) else {
            continue;
        };
        if from_group == to_group {
            continue;
        }
        let entry = accum.entry((from_group, to_group)).or_default();
        entry.kinds.insert(edge.reason.kind);
        entry.dependency_edge_ids.push(edge.id.report_key());
        entry.constrains_init_order |= edge.reason.constrains_init_order();
    }
    let mut accum: Vec<((usize, usize), AtomicEdgeAccumulator)> = accum.into_iter().collect();
    accum.sort_by_key(|((from, to), _)| (*from, *to));
    for (_, entry) in accum.iter_mut() {
        entry.dependency_edge_ids.sort();
    }
    let edges = accum
        .into_iter()
        .enumerate()
        .map(|(idx, ((from, to), entry))| AtomicGroupEdgeReport {
            id: format!("atomic_edge:{idx}"),
            source: atomic_group_key(from),
            target: atomic_group_key(to),
            edge_kinds: entry.kinds.into_iter().collect(),
            dependency_edge_ids: entry.dependency_edge_ids,
            constrains_init_order: entry.constrains_init_order,
        })
        .collect();

    AtomicGraphReport { nodes, edges }
}

fn build_quotient_scc_reports(
    assignment_analysis: &AssignmentAnalysis,
    quotient_nodes: &[ModuleEntry],
    quotient_edges: &[QuotientEdgeReport],
) -> Vec<QuotientSccReport> {
    let quotient_edges_by_source = quotient_edge_indices_by_source(quotient_edges);
    let mut sccs = Vec::new();
    for scc in assignment_analysis.module_quotient_sccs() {
        let is_cycle = scc.len() > 1
            || (scc.len() == 1
                && assignment_analysis
                    .module_quotient()
                    .contains_edge(scc[0], scc[0]));
        if !is_cycle {
            continue;
        }
        let in_scc: BTreeSet<ModuleId> = scc.iter().copied().collect();
        let mut module_edge_ids = Vec::new();
        let mut constraining_module_edge_ids = Vec::new();
        for &source in &in_scc {
            let Some(out_edges) = quotient_edges_by_source.get(&source) else {
                continue;
            };
            for &(target, edge_idx) in out_edges {
                if !in_scc.contains(&target) {
                    continue;
                }
                let edge = &quotient_edges[edge_idx];
                module_edge_ids.push(edge.id.clone());
                if edge.constrains_init_order {
                    constraining_module_edge_ids.push(edge.id.clone());
                }
            }
        }
        let mut modules: Vec<ModuleKey> = in_scc.iter().copied().map(module_key).collect();
        modules.sort();
        module_edge_ids.sort();
        constraining_module_edge_ids.sort();
        sccs.push(QuotientSccReport {
            id: format!("scc:{}", sccs.len()),
            modules,
            is_cycle,
            realizable: constraining_module_edge_ids.is_empty(),
            module_edge_ids,
            constraining_module_edge_ids,
        });
    }
    // Preserve cycle IDs, then include every other module as a singleton.
    // The dependency graph is edge-built, so its SCCs omit isolated modules.
    let cyclic_modules: HashSet<_> = sccs
        .iter()
        .flat_map(|scc| scc.modules.iter().cloned())
        .collect();
    for module in quotient_nodes {
        if !cyclic_modules.contains(&module.key) {
            sccs.push(QuotientSccReport {
                id: format!("scc:{}", sccs.len()),
                modules: vec![module.key.clone()],
                is_cycle: false,
                realizable: true,
                module_edge_ids: Vec::new(),
                constraining_module_edge_ids: Vec::new(),
            });
        }
    }
    sccs
}

fn quotient_edge_indices_by_source(
    quotient_edges: &[QuotientEdgeReport],
) -> BTreeMap<ModuleId, Vec<(ModuleId, usize)>> {
    let mut by_source = BTreeMap::<ModuleId, Vec<(ModuleId, usize)>>::new();
    for (idx, edge) in quotient_edges.iter().enumerate() {
        let Some(source) = module_id_from_key(&edge.source) else {
            continue;
        };
        let Some(target) = module_id_from_key(&edge.target) else {
            continue;
        };
        by_source.entry(source).or_default().push((target, idx));
    }
    by_source
}

/// Preserve the disjoint entry identity and planned-output policy in the
/// report. Neither is inferred from an authored module path.
fn destination_kind(
    assignment_analysis: &AssignmentAnalysis,
    id: ModuleId,
) -> ModuleDestinationKind {
    if id
        == assignment_analysis
            .assignment()
            .entry_destination()
            .module_id()
    {
        return ModuleDestinationKind::Entry;
    }
    match assignment_analysis
        .inputs()
        .planned_module(id)
        .expect("non-entry destination has a planned module")
        .role
    {
        ModuleRole::Specified => ModuleDestinationKind::Specified,
        ModuleRole::Unassigned => ModuleDestinationKind::Unassigned,
    }
}

/// Map a typed [`EdgeRole`] to its wire-format projection. `None`
/// is shorthand for `EdgeRole::Direct` (skipped from the JSON wire
/// shape via `skip_serializing_if = Option::is_none`); promoted
/// edges round-trip through [`EdgeRoleReport::PromotedAtInit`].
fn edge_role_report(role: EdgeRole) -> Option<EdgeRoleReport> {
    match role {
        EdgeRole::Direct => None,
        EdgeRole::PromotedAtInit { callee_source_unit } => Some(EdgeRoleReport::PromotedAtInit {
            callee_source_unit: source_unit_key(callee_source_unit),
        }),
    }
}

/// Build the module-table entry for `id`: the single place its
/// canonical [`spec::ModulePath`] and destination kind are recorded.
fn module_entry(assignment_analysis: &AssignmentAnalysis, id: ModuleId) -> ModuleEntry {
    ModuleEntry {
        key: module_key(id),
        path: assignment_analysis.module_path(id),
        kind: destination_kind(assignment_analysis, id),
    }
}
