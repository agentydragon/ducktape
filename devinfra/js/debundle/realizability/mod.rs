//! Canonical assignment constraints and verification of supplied ESM schedules.
//!
//! Production callers project an exact owner map once, then verify its declared
//! priority or inspect those constraints for the proved residual constructor.
//! The gate checks constraining finish order and cross-owner rebindings. Atom
//! integrity and selector realization remain separate obligations at the shared
//! assignment/writer boundary.
//!
//! The general schedule derivation (`schedule_from_canonical`) is compiled only
//! for bounded test oracles; it has no polynomial complexity guarantee.

use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use rustc_hash::FxHashMap;

use analysis::ids::ModuleId;
use graph::module_assignment::ModuleAssignment;
use graph::report::SccCore;
use graph::{
    ChunkConstrainingEdgeSet, DependencyEdgeId, DependencyGraph, chunk_constraining_module_edges,
};

mod effect_translation_validation;
#[cfg(test)]
mod esm_simulator;
mod translation_validation;

pub use effect_translation_validation::{
    EffectConflictCell, EffectConflictDiagnosticRow, EffectConflictDisposition, EffectConflictRow,
    EffectCoverageReport, EffectOpacity, EffectRelocationAcceptanceScope,
    EffectRelocationAssessment, EffectRelocationConflict, EffectRelocationDecision,
    EffectTranslationValidationReport, EffectTranslationValidationResult,
    LegacyEdgeComparisonCounts,
};
pub(crate) use effect_translation_validation::{
    EffectTranslationValidationInputs, effect_translation_assess,
};
#[cfg(test)]
use esm_schedule::EsmImportOrder;
use esm_schedule::{EsmImportSchedule, ImportScheduleError};
#[cfg(test)]
use esm_simulator::EsmEvaluationSimulator;
pub(crate) use translation_validation::CandidateEmittedOrder;
pub use translation_validation::{
    TranslationValidationEvidenceReport, TranslationValidationReport, TranslationValidationResult,
    translation_validate,
};

/// Canonical in-memory diagnosis of one offending module-quotient
/// SCC. The presence of any such diagnosis on a
/// [`RealizabilityVerdict`] violates clause 3 (Pass 1: multi-module SCC
/// in the constraining-edge subgraph of the quotient; Pass 2: a TDZ
/// proved by the ESM evaluation simulator).
///
/// This is the in-memory consumer of the shared [`SccCore`] shape
/// (typed `ModuleId`s + `DependencyEdgeId` evidence, no rendering) plus one
/// decoration — `rejection`. The exact edge set in `core` depends on
/// `rejection`:
///
/// - [`SccRejection::MutualConstrainingCycle`]: every constraining
///   cross-module source unit edge whose endpoints both fall inside
///   `core.modules`.
/// - [`SccRejection::EsmEvaluationTdz`]: only the source unit edges backing
///   the constraining `(from, to)` pairs whose simulated post-order
///   check failed — the surgical set whose removal (by co-locating the
///   binding pair) lifts the violation.
///
/// [`SccCore`] documents the rendered/wire projections of the same
/// shape ([`crate::validation::report::CycleReport`],
/// [`graph::report::QuotientSccReport`]).
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SccDiagnosis {
    pub core: SccCore,
    /// Which gate pass rejected this SCC.
    pub rejection: SccRejection,
}

/// How the realizability gate decided an SCC is unrealizable. See
/// docs/design.md "Lemma 2: entry-side import ordering" for the
/// two-pass gating rule.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SccRejection {
    /// Pass 1: the SCC is cyclic in the constraining-edge subgraph
    /// alone — a mutual-eager cycle no source import order can
    /// satisfy.
    MutualConstrainingCycle,
    /// Pass 2: the constraining subgraph of the SCC is acyclic, but
    /// the full I-graph (constraining ∪ lazy back-edges) is cyclic
    /// and the ESM evaluation simulator proved a TDZ: some
    /// constraining edge's target evaluates at or after its source
    /// under the materializer's actual import-order choices. This also includes
    /// the implicit entry-to-module edge: entry always evaluates last.
    ///
    /// Pass 2 runs only when Pass 1 finds no cycle. A cyclic constraining
    /// subgraph has no linker order, so the materializer has no import
    /// order for the simulator to check; every path skips Pass 2 then.
    EsmEvaluationTdz,
}

/// Cross-destination rebinding write. ESM imports are read-only in the
/// importing module, so any such edge violates clause 2. One entry per
/// dependency edge.
#[derive(Debug, Clone, Eq, PartialEq, serde::Serialize)]
pub struct CrossRebindEdge {
    pub from: ModuleId,
    pub to: ModuleId,
    pub dependency_edge: DependencyEdgeId,
}

/// Verdict on a (current or hypothetical) destination assignment.
/// Empty `unrealizable_sccs` + `cross_rebinds` ↔ realizable per
/// clauses 2 and 3.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct RealizabilityVerdict {
    pub unrealizable_sccs: Vec<SccDiagnosis>,
    pub cross_rebinds: Vec<CrossRebindEdge>,
}

impl RealizabilityVerdict {
    pub fn is_realizable(&self) -> bool {
        self.unrealizable_sccs.is_empty() && self.cross_rebinds.is_empty()
    }
}

/// Entry imports every emitted module and evaluates last. A constraining edge
/// into entry is therefore a TDZ/order violation even if the source I-graph has
/// no return edge. Diagnose that two-node runtime cycle directly, without
/// adding universal entry fan-out to the I-graph.
fn reject_entry_dependency(
    verdict: &mut RealizabilityVerdict,
    from: ModuleId,
    entry_module_id: ModuleId,
    mut dependency_edges: Vec<DependencyEdgeId>,
) {
    if dependency_edges.is_empty()
        || verdict.unrealizable_sccs.iter().any(|scc| {
            scc.core.modules.contains(&from) && scc.core.modules.contains(&entry_module_id)
        })
    {
        return;
    }
    dependency_edges.sort();
    verdict.unrealizable_sccs.push(SccDiagnosis {
        core: SccCore {
            modules: BTreeSet::from([from, entry_module_id]),
            constraining_dependency_edges: dependency_edges,
        },
        rejection: SccRejection::EsmEvaluationTdz,
    });
}

/// Pure-function form for tests: projects the assignment and checks it under
/// the derived import schedule.
#[cfg(test)]
pub fn check_realizability(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
) -> RealizabilityVerdict {
    let (canonical, cross_rebinds) = canonical_assignment_constraints(dependency_graph, assignment);
    verdict_from_canonical(
        canonical,
        cross_rebinds,
        assignment.entry_destination().module_id(),
    )
}

/// Validate the exact schedule retained for this assignment. Malformed target
/// sets are errors, not a way to omit imports from the ESM traversal.
pub fn check_realizability_with_schedule(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    schedule: &EsmImportSchedule,
) -> Result<RealizabilityVerdict, ImportScheduleError> {
    let AssignmentConstraints {
        canonical,
        cross_rebinds,
    } = project_assignment_constraints(dependency_graph, assignment);
    let entry = assignment.entry_destination().module_id();
    let nodes = schedule.modules();
    if assignment
        .iter()
        .any(|(_, module)| !nodes.contains(&module))
    {
        return Err(ImportScheduleError::ModuleUniverse);
    }
    schedule.check_targets(entry, &canonical.i_successors, &nodes)?;
    Ok(verdict_from_canonical_scheduled(
        canonical,
        cross_rebinds,
        entry,
        schedule,
    ))
}

/// Verify the supplied shared priority with one canonical source projection.
/// A failed ordering is returned as its concrete verdict; no alternatives are
/// tried and no schedule-existence claim is inferred from that failure.
pub fn check_realizability_with_priority(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    nodes: &BTreeSet<ModuleId>,
    priority: &[ModuleId],
) -> Result<(RealizabilityVerdict, EsmImportSchedule), ImportScheduleError> {
    if assignment
        .iter()
        .any(|(_, module)| !nodes.contains(&module))
    {
        return Err(ImportScheduleError::ModuleUniverse);
    }
    let entry = assignment.entry_destination().module_id();
    let AssignmentConstraints {
        canonical,
        cross_rebinds,
    } = project_assignment_constraints(dependency_graph, assignment);
    let schedule =
        EsmImportSchedule::from_priority(entry, &canonical.i_successors, nodes, priority)?;
    let verdict = verdict_from_canonical_scheduled(canonical, cross_rebinds, entry, &schedule);
    Ok((verdict, schedule))
}

/// Retain the accepted schedule from the same canonical projection and search.
/// The explicit universe includes empty emitted owners, which have no edges.
#[cfg(test)]
pub fn check_realizability_retaining_schedule(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    nodes: &BTreeSet<ModuleId>,
) -> Result<(RealizabilityVerdict, EsmImportSchedule), ImportScheduleError> {
    let entry = assignment.entry_destination().module_id();
    if !nodes.contains(&entry)
        || assignment
            .iter()
            .any(|(_, module)| !nodes.contains(&module))
    {
        return Err(ImportScheduleError::ModuleUniverse);
    }
    let (canonical, cross_rebinds) = canonical_assignment_constraints(dependency_graph, assignment);
    let schedule = schedule_from_canonical(&canonical, entry, nodes);
    let verdict = verdict_from_canonical_scheduled(canonical, cross_rebinds, entry, &schedule);
    Ok((verdict, schedule))
}

/// Canonical assignment constraints shared by the gate and placement callers.
/// The import graph includes lazy imports; `canonical.edges` contains only
/// imports that constrain initialization order. Cross-destination rebinds stay
/// separate because they are a distinct gate clause and never become imports.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct AssignmentConstraints {
    pub canonical: ChunkConstrainingEdgeSet,
    pub cross_rebinds: Vec<CrossRebindEdge>,
}

/// Project one exact source-unit assignment into the canonical gate graph and
/// cross-destination rebind constraints. This does not choose an import order.
pub fn project_assignment_constraints(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
) -> AssignmentConstraints {
    let (canonical, cross_rebinds) = canonical_assignment_constraints(dependency_graph, assignment);
    AssignmentConstraints {
        canonical,
        cross_rebinds,
    }
}

fn canonical_assignment_constraints(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
) -> (ChunkConstrainingEdgeSet, Vec<CrossRebindEdge>) {
    // Cross-destination rebinds are a separate clause-2 violation and
    // not part of the I-graph. Collect them in a single pass over
    // source unit edges; the canonical edge set handles everything else.
    let mut cross_rebinds = Vec::new();
    for edge in dependency_graph.iter_edges() {
        if dependency_graph.node(edge.from).is_none() || dependency_graph.node(edge.to).is_none() {
            continue;
        }
        if !edge.reason.is_rebind() {
            continue;
        }
        let Some((from, to)) =
            graph::assignment_endpoints(edge, assignment, graph::EndpointView::Gate)
        else {
            continue;
        };
        cross_rebinds.push(CrossRebindEdge {
            from,
            to,
            dependency_edge: edge.id,
        });
    }

    // One canonical projection provides two distinct adjacencies: `edges`
    // contains constraining dependencies for Pass 1, while `i_successors`
    // includes lazy import back-edges for Pass 2's ESM evaluation topology.
    // Sharing the projection does not make the two SCC partitions equivalent.
    let canonical = chunk_constraining_module_edges(dependency_graph, assignment);
    (canonical, cross_rebinds)
}

/// Run the complete gate over the canonical module projection under the
/// derived import schedule.
#[cfg(test)]
fn verdict_from_canonical(
    canonical: ChunkConstrainingEdgeSet,
    cross_rebinds: Vec<CrossRebindEdge>,
    entry_module_id: ModuleId,
) -> RealizabilityVerdict {
    if canonical.edges.is_empty() {
        return RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds,
        };
    }
    let mut nodes = BTreeSet::from([entry_module_id]);
    for (&from, targets) in &canonical.i_successors {
        nodes.insert(from);
        nodes.extend(targets);
    }
    let schedule = schedule_from_canonical(&canonical, entry_module_id, &nodes);
    verdict_from_canonical_scheduled(canonical, cross_rebinds, entry_module_id, &schedule)
}

#[cfg(test)]
fn schedule_from_canonical(
    canonical: &ChunkConstrainingEdgeSet,
    entry_module_id: ModuleId,
    nodes: &BTreeSet<ModuleId>,
) -> EsmImportSchedule {
    let pairs = canonical.pairs().collect();
    let policy = EsmImportOrder::build(&pairs, &canonical.i_successors, nodes);
    EsmImportSchedule::construct(
        entry_module_id,
        &pairs,
        &canonical.i_successors,
        nodes,
        &policy,
    )
    .unwrap_or_else(|_| {
        EsmImportSchedule::from_policy(entry_module_id, &canonical.i_successors, nodes, &policy)
    })
}

fn verdict_from_canonical_scheduled(
    canonical: ChunkConstrainingEdgeSet,
    cross_rebinds: Vec<CrossRebindEdge>,
    entry_module_id: ModuleId,
    schedule: &EsmImportSchedule,
) -> RealizabilityVerdict {
    let mut verdict = RealizabilityVerdict {
        unrealizable_sccs: constraining_cycles(&canonical),
        cross_rebinds,
    };
    if canonical.edges.is_empty() {
        return verdict;
    }

    // Pass 1: Tarjan over the constraining-edge subgraph. Catches
    // **mutual** constraining cycles (both sides eager-read each
    // other; no source order can satisfy both). Pass 2 below applies
    // the simulator only to SCCs not already flagged here.
    let reported: BTreeSet<_> = verdict
        .unrealizable_sccs
        .iter()
        .map(|diagnosis| diagnosis.core.modules.clone())
        .collect();

    // Pass 2: Tarjan over the full I-graph (canonical
    // `i_successors`, which includes lazy back-edges). Multi-module
    // I-SCCs not already in `reported` are the asymmetric
    // `(at-init forward, lazy back)` candidates: the constraining
    // subgraph alone is acyclic, but the lazy back-edge closes a
    // cycle in the runtime DFS topology. Lemma 2
    // (`chunk_source_import_order_from_adjacency`) reverses entry's
    // import order within each I-SCC so DFS lands on the dependent
    // first and unwinds through the dependency; the simulator below
    // checks whether that reversal actually rescues evaluation given
    // the spec's full import topology.
    let mut i_graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for (from, succs) in &canonical.i_successors {
        for to in succs {
            i_graph.add_edge(*from, *to, ());
        }
    }
    let i_sccs = tarjan_scc(&i_graph);

    // Assign each module to its Tarjan component once, then identify
    // constraining edges internal to those components in one edge pass.
    // The candidate filter below used to rescan every canonical edge for
    // each I-SCC, making this O(number of SCCs × number of edges).
    let mut i_scc_by_module = FxHashMap::default();
    for (scc_index, scc) in i_sccs.iter().enumerate() {
        for &module in scc {
            i_scc_by_module.insert(module, scc_index);
        }
    }
    let mut i_scc_has_constraining_edge = vec![false; i_sccs.len()];
    for &(from, to) in canonical.edges.keys() {
        let from_scc = i_scc_by_module[&from];
        let to_scc = i_scc_by_module[&to];
        if from_scc == to_scc {
            i_scc_has_constraining_edge[from_scc] = true;
        }
    }

    let candidate_sccs: Vec<BTreeSet<ModuleId>> = i_sccs
        .into_iter()
        .enumerate()
        .filter_map(|(scc_index, scc)| {
            if scc.len() < 2 {
                return None;
            }
            let modules: BTreeSet<ModuleId> = scc.into_iter().collect();
            if reported.contains(&modules) {
                return None;
            }
            // Skip SCCs that carry no constraining edge between
            // members — pure-lazy I-cycles never TDZ regardless of
            // entry's import order.
            if !i_scc_has_constraining_edge[scc_index] {
                return None;
            }
            Some(modules)
        })
        .collect();

    // Pass 2 needs the linker order, which exists only when Pass 1 found
    // no constraining cycle.
    if reported.is_empty() && !candidate_sccs.is_empty() {
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        for modules in candidate_sccs {
            let tdz_pairs: Vec<(ModuleId, ModuleId)> = schedule
                .tdz_pairs(&constraining_pairs)
                .filter(|(from, to)| modules.contains(from) && modules.contains(to))
                .collect();
            if tdz_pairs.is_empty() {
                continue;
            }
            let mut dependency_edges: Vec<DependencyEdgeId> = Vec::new();
            for (from, to) in &tdz_pairs {
                dependency_edges.extend_from_slice(canonical.edges_for(*from, *to));
            }
            dependency_edges.sort();
            verdict.unrealizable_sccs.push(SccDiagnosis {
                core: SccCore {
                    modules,
                    constraining_dependency_edges: dependency_edges,
                },
                rejection: SccRejection::EsmEvaluationTdz,
            });
        }
    }

    for (&(from, to), edges) in &canonical.edges {
        if to == entry_module_id {
            reject_entry_dependency(&mut verdict, from, to, edges.clone());
        }
    }

    verdict
}

/// Order-independent Pass-1 diagnoses for generated-layout construction.
/// This is not a realizability verdict: no import order is inferred or checked.
pub fn assignment_constraining_cycles(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
) -> Vec<SccDiagnosis> {
    constraining_cycles(&chunk_constraining_module_edges(
        dependency_graph,
        assignment,
    ))
}

fn constraining_cycles(canonical: &ChunkConstrainingEdgeSet) -> Vec<SccDiagnosis> {
    let mut graph = DiGraphMap::<ModuleId, ()>::new();
    for (from, to) in canonical.pairs() {
        graph.add_edge(from, to, ());
    }
    let mut by_module = FxHashMap::default();
    let mut diagnoses = Vec::new();
    for component in tarjan_scc(&graph)
        .into_iter()
        .filter(|component| component.len() > 1)
    {
        let index = diagnoses.len();
        for &module in &component {
            by_module.insert(module, index);
        }
        diagnoses.push(SccDiagnosis {
            core: SccCore {
                modules: component.into_iter().collect(),
                constraining_dependency_edges: Vec::new(),
            },
            rejection: SccRejection::MutualConstrainingCycle,
        });
    }
    for (&(from, to), edges) in &canonical.edges {
        if let Some(&index) = by_module.get(&from)
            && by_module.get(&to) == Some(&index)
        {
            diagnoses[index]
                .core
                .constraining_dependency_edges
                .extend_from_slice(edges);
        }
    }
    for diagnosis in &mut diagnoses {
        diagnosis.core.constraining_dependency_edges.sort_unstable();
    }
    diagnoses
}

#[cfg(test)]
mod tests;
