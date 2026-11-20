//! Canonical assignment constraints and verification of supplied ESM schedules.
//!
//! Production callers project an exact owner map once, then verify its declared
//! priority or inspect those constraints for the proved residual constructor.
//! The gate checks constraining finish order and cross-owner rebindings. Atom
//! integrity and selector realization remain separate obligations at the shared
//! assignment/writer boundary.
//!
//! The former general scheduler and mutable hypothetical-move index are compiled
//! only for bounded test oracles. They cannot be used by an ordinary build, and
//! their schedule-existence work has no polynomial complexity guarantee.

#[cfg(test)]
use std::collections::BTreeMap;
use std::collections::BTreeSet;
#[cfg(test)]
use std::sync::OnceLock;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use rustc_hash::FxHashMap;

use analysis::graph::{
    ChunkConstrainingEdgeSet, DependencyEdgeId, DependencyGraph, chunk_constraining_module_edges,
};
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use analysis::reports::SccCore;
#[cfg(test)]
use source_identity::SourceUnitId;

#[cfg(test)]
pub mod condensation_order;
#[cfg(test)]
mod condensation_order_proptest;
mod effect_translation_validation;
#[cfg(test)]
mod esm_simulator;
#[cfg(test)]
mod incremental_quotient;
mod translation_validation;

#[cfg(test)]
use crate::esm_import_order::EsmImportOrder;
use crate::esm_schedule::{EsmImportSchedule, ImportScheduleError};
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
use esm_simulator::EsmEvaluationSimulator;
#[cfg(test)]
use incremental_quotient::{IncrementalQuotient, QuotientOverlay};
#[cfg(test)]
pub use incremental_quotient::{LadderDecision, ModuleAssignmentBatch, ModuleAssignmentDelta};
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
/// shape ([`crate::validation::CycleReport`],
/// [`analysis::reports::schema::QuotientSccReport`]).
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

/// Final adjacency changes after one simultaneous batch. Evidence and parallel
/// counts do not invalidate ordering certificates unless a pair crosses zero.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
#[cfg(test)]
pub struct QuotientTopologyDelta {
    pub imports: BTreeMap<(ModuleId, ModuleId), bool>,
    pub constraining: BTreeMap<(ModuleId, ModuleId), bool>,
}

#[cfg(test)]
impl QuotientTopologyDelta {
    /// Accumulate final changes until a consumer needs its maintained view.
    /// Inverse zero crossings cancel, including speculative rollback.
    pub fn compose(&mut self, next: &Self) {
        compose_topology(&mut self.imports, &next.imports);
        compose_topology(&mut self.constraining, &next.constraining);
    }
}

#[cfg(test)]
fn compose_topology(
    pending: &mut BTreeMap<(ModuleId, ModuleId), bool>,
    delta: &BTreeMap<(ModuleId, ModuleId), bool>,
) {
    for (&pair, &present) in delta {
        if let Some(previous) = pending.remove(&pair) {
            assert_ne!(previous, present, "successive zero crossings alternate");
        } else {
            pending.insert(pair, present);
        }
    }
}

/// Current nontrivial C components and topology work performed to refresh them.
#[derive(Debug, Default)]
#[cfg(test)]
pub struct ConstrainingComponents {
    pub components: Vec<BTreeSet<ModuleId>>,
    pub rows_visited: usize,
    pub edges_visited: usize,
}

/// Allocation-free access to the canonical Gate constraints maintained by an
/// assignment index. Diagnostic evidence is materialized only when requested.
#[cfg(test)]
pub struct QuotientConstraints<'a> {
    pub imports: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    pub import_predecessors: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    pub constraining_pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    pub constraining_successors: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    pub constraining_predecessors: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

/// Entry imports every emitted module and evaluates last. A constraining edge
/// into entry is therefore a TDZ/order violation even if the source I-graph has
/// no return edge. Diagnose that two-node runtime cycle directly, without
/// inflating the maintained I-condensation with universal entry fan-out.
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

/// Pure-function form. Builds the canonical constraining edge set,
/// runs Tarjan, surfaces multi-module SCCs and cross-rebinds. The
/// correctness reference for the `RealizabilityIndex`'s incremental
/// backing.
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
        let Some((from, to)) = analysis::graph::assignment_endpoints(
            edge,
            assignment,
            analysis::graph::EndpointView::Gate,
        ) else {
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

/// Run the complete gate over the canonical module projection. The pure
/// dependency graph path and the incremental hypothetical-move path both use this
/// body, so they cannot drift on remote SCCs or ESM evaluation order.
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

/// Touching-filtered form of [`check_realizability`]: the same pure
/// from-scratch verdict, restricted to diagnoses involving `module`.
///
/// This is the gate ladder's **reference predicate**
/// for a speculative merge: a post-merge module `M` is acceptable iff
/// `check_realizability_touching(dependency_graph, post_assignment, M)`
/// `.is_realizable()`. Pre-existing violations not touching `M` are
/// intentionally ignored, matching
/// [`RealizabilityIndex::verdict_after_moving_source_units_touching`]'s
/// semantics on both the hot and diagnostic paths.
///
/// Differential-harness / oracle use only — `O(N + M)` per call, far
/// too slow for the proposer's per-pop gate.
#[cfg(test)]
pub fn check_realizability_touching(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module: ModuleId,
) -> RealizabilityVerdict {
    let full = check_realizability(dependency_graph, assignment);
    RealizabilityVerdict {
        unrealizable_sccs: full
            .unrealizable_sccs
            .into_iter()
            .filter(|scc| scc.core.modules.contains(&module))
            .collect(),
        cross_rebinds: full
            .cross_rebinds
            .into_iter()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .collect(),
    }
}

/// Mutable index over a working assignment: the incremental form of the
/// predicate `check_realizability` computes (docs/design.md
/// "Realizability primitive").
///
/// Each `apply` updates only quotient edge buckets incident to the
/// moved source units. `verdict()` reads the maintained quotient graph
/// instead of rebuilding it from source unit edges.
///
/// The index does NOT hold a borrow of `DependencyGraph`. `apply` and every
/// `*_after_moving_source_units*` verdict query take the graph as a
/// parameter. Passing it per call keeps the caller's ownership flat.
#[derive(Debug, Clone)]
#[cfg(test)]
pub struct RealizabilityIndex {
    assignment: ModuleAssignment,
    quotient: IncrementalQuotient,
}

#[cfg(test)]
impl RealizabilityIndex {
    pub fn from_assignment(
        dependency_graph: &DependencyGraph,
        assignment: ModuleAssignment,
    ) -> Self {
        let quotient = IncrementalQuotient::new(dependency_graph, &assignment);
        Self {
            assignment,
            quotient,
        }
    }

    /// Borrow the current working assignment. Callers should treat this
    /// as read-only — mutation goes through [`Self::apply`] so the
    /// quotient stays consistent.
    pub fn assignment(&self) -> &ModuleAssignment {
        &self.assignment
    }

    pub fn constraints(&self) -> QuotientConstraints<'_> {
        QuotientConstraints {
            imports: self.quotient.base_i_successors(),
            import_predecessors: self.quotient.i_graph.reverse_adjacency(),
            constraining_pairs: self.quotient.base_constraining_pairs(),
            constraining_successors: self.quotient.constraining_graph.adjacency(),
            constraining_predecessors: self.quotient.constraining_graph.reverse_adjacency(),
        }
    }

    /// Register the fixed token arena on first use, then refresh only pending
    /// final topology changes. Query cost excludes singleton component scans.
    pub fn constraining_components_with_poll<E>(
        &self,
        nodes: &BTreeSet<ModuleId>,
        poll: impl FnMut() -> Result<(), E>,
    ) -> Result<ConstrainingComponents, E> {
        self.quotient.constraining_components_with_poll(nodes, poll)
    }

    /// Read the canonical Gate C/I projection of the current assignment without
    /// scheduling it or revisiting source edges. Constructive placement consumes
    /// these constraints directly; it does not use verdicts as a candidate oracle.
    pub fn canonical_projection(&self) -> ChunkConstrainingEdgeSet {
        self.quotient
            .canonical_with_overlay(&QuotientOverlay::default())
    }

    /// Canonical cross-owner rebindings, in source-edge order. They are equality
    /// constraints for placement and are maintained with the same batch updates.
    pub fn cross_rebinds(&self) -> Vec<CrossRebindEdge> {
        self.quotient
            .cross_rebinds_with_overlay(&QuotientOverlay::default())
    }

    /// Commit `delta` to the working assignment and quotient; it cannot
    /// be reverted. Hypothetical moves are answered without mutation by
    /// [`Self::verdict_after_moving_source_units_touching`] and
    /// [`Self::ladder_decision_after_moving_source_units_touching`].
    pub fn apply(&mut self, dependency_graph: &DependencyGraph, delta: ModuleAssignmentDelta) {
        self.apply_batch(dependency_graph, &ModuleAssignmentBatch::from(delta));
    }

    /// Commit a simultaneous source-unit mapping and update the quotient once
    /// from the old assignment to the final assignment. Each incident edge is
    /// removed and re-added at most once, including edges whose two endpoints
    /// both move.
    ///
    /// Every destination must be a canonical `ModuleId` in the fixed module
    /// universe used to build this index. This method does not allocate or
    /// renumber destinations; rebuild the index if module identities or the
    /// entry destination change.
    pub fn apply_batch(
        &mut self,
        dependency_graph: &DependencyGraph,
        batch: &ModuleAssignmentBatch,
    ) -> QuotientTopologyDelta {
        self.quotient
            .apply_batch(dependency_graph, &mut self.assignment, batch)
    }

    /// Verdict against the current working assignment. Reads the
    /// incrementally maintained quotient graph and evidence buckets.
    pub fn verdict(&self) -> RealizabilityVerdict {
        self.quotient.verdict()
    }

    /// Verdict filtered to SCCs and cross-rebinds touching `module`.
    /// Candidate evaluation uses this for the fresh hypothetical
    /// destination: unrelated pre-existing bad SCCs are intentionally
    /// ignored.
    pub fn verdict_touching(&self, module: ModuleId) -> RealizabilityVerdict {
        self.quotient.verdict_touching(module)
    }

    /// Full verdict for a hypothetical source unit move. The incident-edge overlay
    /// changes the canonical module projection without mutating the index;
    /// the shared gate body then checks every SCC, including SCCs remote from
    /// the destination whose ESM import order changes after this move.
    pub fn verdict_after_moving_source_units(
        &self,
        dependency_graph: &DependencyGraph,
        source_units: &[SourceUnitId],
        to: ModuleId,
    ) -> RealizabilityVerdict {
        let batch = ModuleAssignmentBatch::from(ModuleAssignmentDelta::MoveSourceUnits {
            source_units: source_units.to_vec(),
            to,
        });
        self.verdict_after_batch(dependency_graph, &batch)
    }

    /// Full canonical verdict for one simultaneous source-unit mapping.
    /// Unlike the candidate-oriented touching query, this checks every SCC
    /// and the global ESM import order, including remote TDZs whose order
    /// changes because of the batch.
    ///
    /// Destinations must be canonical `ModuleId`s from the fixed module
    /// universe used to build this index, and the entry destination stays
    /// fixed. If module paths are inserted or reordered, rebuild the index
    /// with the new canonical IDs before querying.
    pub fn verdict_after_batch(
        &self,
        dependency_graph: &DependencyGraph,
        batch: &ModuleAssignmentBatch,
    ) -> RealizabilityVerdict {
        let overlay = self
            .quotient
            .overlay_for_batch(dependency_graph, &self.assignment, batch);
        let verdict = verdict_from_canonical(
            self.quotient.canonical_with_overlay(&overlay),
            self.quotient.cross_rebinds_with_overlay(&overlay),
            self.assignment.entry_destination().module_id(),
        );
        if gate_oracle_enabled() {
            let mut post_assignment = self.assignment.clone();
            for (&source_unit, &to) in batch.destinations() {
                post_assignment.set(source_unit, to);
            }
            let reference = check_realizability(dependency_graph, &post_assignment);
            assert_eq!(
                verdict, reference,
                "DEBUNDLE_GATE_ORACLE: full overlay verdict diverges from the pure reference"
            );
        }
        verdict
    }

    /// Verdict for a hypothetical source unit move, filtered to the target
    /// module, without mutating the working assignment. This is the
    /// candidate-evaluation fast path: it builds a small quotient
    /// overlay for the moved source units' incident edges and runs directed
    /// reachability against the effective graph.
    pub fn verdict_after_moving_source_units_touching(
        &self,
        dependency_graph: &DependencyGraph,
        source_units: &[SourceUnitId],
        to: ModuleId,
    ) -> RealizabilityVerdict {
        let batch = ModuleAssignmentBatch::from(ModuleAssignmentDelta::MoveSourceUnits {
            source_units: source_units.to_vec(),
            to,
        });
        self.verdict_after_batch_touching(dependency_graph, &batch, to)
    }

    fn verdict_after_batch_touching(
        &self,
        dependency_graph: &DependencyGraph,
        batch: &ModuleAssignmentBatch,
        module: ModuleId,
    ) -> RealizabilityVerdict {
        let overlay = self
            .quotient
            .overlay_for_batch(dependency_graph, &self.assignment, batch);
        let verdict = self
            .quotient
            .verdict_with_overlay_touching(module, &overlay);
        if gate_oracle_enabled() {
            // Oracle mode: the boolean tier ladder must agree with
            // the evidence-producing verdict on every diagnostic-path
            // query.
            let decision = self.quotient.ladder_decide(module, &overlay);
            assert_eq!(
                decision.accepts(),
                verdict.is_realizable(),
                "DEBUNDLE_GATE_ORACLE: ladder {decision:?} diverges from the overlay \
                 verdict for {module:?}: {verdict:#?}",
            );
        }
        verdict
    }

    /// Tier-laddered decision for a hypothetical source unit move, filtered
    /// to the target module. Exactly equal to
    /// `verdict_after_moving_source_units_touching(..).is_realizable()` with
    /// evidence materialization elided: tiers 0–2 are short-circuits
    /// whose skip conditions are theorems about the predicate, tier 3
    /// runs the shared simulator path. With `DEBUNDLE_GATE_ORACLE`
    /// set, every query is additionally cross-checked against the
    /// pure touching-filtered reference and divergence panics.
    pub fn ladder_decision_after_moving_source_units_touching(
        &self,
        dependency_graph: &DependencyGraph,
        source_units: &[SourceUnitId],
        to: ModuleId,
    ) -> LadderDecision {
        let batch = ModuleAssignmentBatch::from(ModuleAssignmentDelta::MoveSourceUnits {
            source_units: source_units.to_vec(),
            to,
        });
        let overlay = self
            .quotient
            .overlay_for_batch(dependency_graph, &self.assignment, &batch);
        let decision = self.quotient.ladder_decide(to, &overlay);
        if gate_oracle_enabled() {
            let mut post_assignment = self.assignment.clone();
            for (&source_unit, &destination) in batch.destinations() {
                post_assignment.set(source_unit, destination);
            }
            let reference = check_realizability_touching(dependency_graph, &post_assignment, to);
            assert_eq!(
                decision.accepts(),
                reference.is_realizable(),
                "DEBUNDLE_GATE_ORACLE: ladder {decision:?} diverges from the pure \
                 reference for {to:?}: {reference:#?}",
            );
        }
        decision
    }

    /// `O(α)` DSU probe against the tier-1 condensation order: are
    /// `a` and `b` in the same multi-module constraining SCC? Backs
    /// the greedy's cycle-reduction sort key ("this merge dissolves
    /// part of an unrealizable SCC") without a cache that can drift.
    pub fn modules_share_constraining_multi_scc(&self, a: ModuleId, b: ModuleId) -> bool {
        self.quotient.modules_share_constraining_multi_scc(a, b)
    }
}

/// One-time cached `DEBUNDLE_GATE_ORACLE` probe: when set, every
/// legacy test query cross-checks against the schedule-existence reference and
/// panics on divergence. This oracle may perform exponential work; it is absent
/// from production builds.
#[cfg(test)]
fn gate_oracle_enabled() -> bool {
    static ORACLE_ENABLED: OnceLock<bool> = OnceLock::new();
    *ORACLE_ENABLED.get_or_init(|| std::env::var_os("DEBUNDLE_GATE_ORACLE").is_some())
}

/// True when `overlay` introduces no changes the ESM evaluation
/// simulator can observe — i.e. `i_delta` is empty AND no constraining
/// bucket is added/removed. Cross-rebind edits do not affect the
/// simulator (rebinds participate in the rebind verdict, not the
/// I-graph DFS or constraining-edge SCC). Used by
/// `IncrementalQuotient::build_simulator`'s fast path to reuse the
/// cached base simulator.
#[cfg(test)]
fn overlay_is_simulator_noop(overlay: Option<&QuotientOverlay>) -> bool {
    let Some(overlay) = overlay else {
        return true;
    };
    overlay.i_delta.is_empty()
        && overlay.constraining_added.is_empty()
        && overlay.constraining_removed.is_empty()
}

/// Edges whose gate-view contribution can change when `source units` move:
/// those incident to a moved source unit. The gate view reads only the
/// endpoints' modules and the entry, never a promoted edge's callee.
#[cfg(test)]
fn impacted_dependency_edges(
    dependency_graph: &DependencyGraph,
    source_units: &[SourceUnitId],
) -> Vec<DependencyEdgeId> {
    let mut impacted = BTreeSet::<DependencyEdgeId>::new();
    for source_unit in source_units {
        impacted.extend(dependency_graph.out_edges_of(*source_unit).iter().copied());
        impacted.extend(dependency_graph.in_edges_of(*source_unit).iter().copied());
    }
    impacted.into_iter().collect()
}

#[cfg(test)]
mod tests;
