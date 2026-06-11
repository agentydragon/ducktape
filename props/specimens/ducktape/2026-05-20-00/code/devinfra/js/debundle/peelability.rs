use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashSet};

use petgraph::algo::tarjan_scc;
use petgraph::graph::DiGraph;

use crate::graph::OwnerEdge;
use crate::realizability::{PartitionDelta, RealizabilityIndex};
use crate::reports::{
    binding_reports, is_residual_destination, module_id_from_key, module_report_ref, owner_key,
};
use swc_ecma_ast::Id;

use crate::{
    AtomicUnit, BindingKind, ChunkFactorization, DepKind, EvaluatedPeelCandidateReport,
    LogicalModuleIndex, ModuleId, OwnerGraphPeelSetReport, OwnerGraphPeelabilityReport, OwnerId,
    OwnerNode, PeelCandidateStatus, QuotientEdgeReport, ResidualOwnerCompanionOptionReport,
    ResidualOwnerPeelHorizonReport, ResidualOwnerPeelStatus, compute_atomic_units,
};

pub(crate) struct PeelabilityContext<'a> {
    owner_edges: &'a [OwnerEdge],
    owner_out_edges: Vec<Vec<usize>>,
    /// Atomic units of the factorization's owner graph (SCCs of the
    /// constraining-edge subgraph `G_atomic`). Used by
    /// `residual_atomic_unit_candidates` to enumerate multi-owner
    /// candidates.
    atomic_units: Vec<AtomicUnit>,
    /// Single shared implementation of the validity predicate
    /// (DESIGN.md "Realizability primitive"). Candidate evaluation
    /// applies and rolls back a fresh-destination move against this
    /// index, using the same quotient facts the validator runs against
    /// the actual partition.
    realizability: RefCell<RealizabilityIndex<'a>>,
    /// Sentinel `ModuleId` reserved for the candidate's hypothetical
    /// destination. Picked above any logical-module index that
    /// currently appears in the chunk's partition or its logical
    /// module list, so pushing `MoveOwners { to: fresh_destination }`
    /// guarantees a brand-new node in the post-peel quotient.
    fresh_destination: ModuleId,
}

#[derive(Debug, Clone)]
pub(crate) struct PeelCandidateEvaluation {
    pub(crate) id: String,
    pub(crate) status: PeelCandidateStatus,
    pub(crate) owner_ids: Vec<OwnerId>,
    pub(crate) members: Vec<Id>,
    pub(crate) constraining_owner_edge_indices: BTreeSet<usize>,
    /// Owner ids whose residual dependency forced the candidate into
    /// `BlockedResidualDependency`. Empty for other statuses. Surfaces
    /// in `evaluated_owner_sets[].residual_dependency_blockers` so
    /// callers can pinpoint which residual neighbor blocked the peel.
    pub(crate) residual_dependency_blocker_owner_ids: Vec<OwnerId>,
}

pub(crate) fn build_peelability_report(
    factorization: &ChunkFactorization,
    owner_edges: &[OwnerEdge],
    quotient_edges: &[QuotientEdgeReport],
) -> OwnerGraphPeelabilityReport {
    let residual_destinations: BTreeSet<ModuleId> = factorization
        .analysis
        .owner_graph
        .iter_nodes()
        .filter_map(|node| {
            let module = factorization.partition.of(node.id);
            is_residual_destination(factorization, module).then_some(module)
        })
        .collect();

    let context = PeelabilityContext::new(factorization, owner_edges, quotient_edges);
    let mut declared_by_owner = BTreeMap::<OwnerId, Vec<Id>>::new();
    for node in factorization.analysis.owner_graph.iter_nodes() {
        if !is_residual_destination(factorization, factorization.partition.of(node.id)) {
            continue;
        }
        let declared = residual_declared_for_owner(factorization, node);
        if declared.is_empty() {
            continue;
        }
        declared_by_owner.insert(node.id, declared);
    }

    let mut singleton_candidates = Vec::<(OwnerId, PeelCandidateEvaluation)>::new();
    for (&owner_id, declared) in &declared_by_owner {
        singleton_candidates.push((
            owner_id,
            evaluate_peel_candidate(factorization, &context, &[owner_id], declared.clone()),
        ));
    }

    let pair_owner_sets = residual_pair_candidates_from_singleton_blockers(
        factorization,
        &singleton_candidates,
        owner_edges,
        &declared_by_owner,
    );

    let mut pair_candidates = Vec::new();
    for (left, right) in pair_owner_sets {
        let mut declared = declared_by_owner.get(&left).cloned().unwrap_or_default();
        declared.extend(declared_by_owner.get(&right).into_iter().flatten().cloned());
        declared.sort();
        declared.dedup();

        let candidate = evaluate_peel_candidate(factorization, &context, &[left, right], declared);
        if candidate.status == PeelCandidateStatus::PeelableNow {
            pair_candidates.push(candidate);
        }
    }

    let dependency_closure_candidates = residual_dependency_closure_candidates(
        factorization,
        &context,
        &singleton_candidates,
        &declared_by_owner,
    );

    let atomic_unit_candidates =
        residual_atomic_unit_candidates(factorization, &context, &declared_by_owner);

    let mut candidates: Vec<PeelCandidateEvaluation> = singleton_candidates
        .into_iter()
        .map(|(_, candidate)| candidate)
        .collect();
    let mut seen_candidate_ids: HashSet<String> = candidates.iter().map(|c| c.id.clone()).collect();
    for candidate in pair_candidates
        .into_iter()
        .chain(dependency_closure_candidates)
        .chain(atomic_unit_candidates)
    {
        if seen_candidate_ids.insert(candidate.id.clone()) {
            candidates.push(candidate);
        }
    }

    let (residual_owner_horizon, minimal_peel_set_ids) =
        build_residual_owner_horizon(factorization, &declared_by_owner, &candidates);
    let minimal_peel_sets = candidates
        .iter()
        .filter(|candidate| minimal_peel_set_ids.contains(&candidate.id))
        .map(|candidate| OwnerGraphPeelSetReport {
            candidate_id: candidate.id.clone(),
            owner_ids: candidate.owner_ids.iter().copied().map(owner_key).collect(),
            members: binding_reports(factorization, candidate.members.iter()),
        })
        .collect();

    let evaluated_owner_sets = candidates
        .iter()
        .map(|candidate| EvaluatedPeelCandidateReport {
            candidate_id: candidate.id.clone(),
            owner_ids: candidate.owner_ids.iter().copied().map(owner_key).collect(),
            members: binding_reports(factorization, candidate.members.iter()),
            status: candidate.status,
            cycle_blockers: candidate
                .constraining_owner_edge_indices
                .iter()
                .filter_map(|idx| owner_edges.get(*idx).map(|edge| edge.id.report_key()))
                .collect(),
            residual_dependency_blockers: candidate
                .residual_dependency_blocker_owner_ids
                .iter()
                .copied()
                .map(owner_key)
                .collect(),
        })
        .collect();

    OwnerGraphPeelabilityReport {
        residual_destinations: residual_destinations
            .into_iter()
            .map(|id| module_report_ref(factorization, id))
            .collect(),
        minimal_peel_sets,
        residual_owner_horizon,
        evaluated_owner_sets,
    }
}

fn build_residual_owner_horizon(
    factorization: &ChunkFactorization,
    declared_by_owner: &BTreeMap<OwnerId, Vec<Id>>,
    candidates: &[PeelCandidateEvaluation],
) -> (Vec<ResidualOwnerPeelHorizonReport>, BTreeSet<String>) {
    let candidate_owner_sets = build_candidate_owner_sets(candidates);
    let mut peelable_candidate_indices_by_owner = BTreeMap::<OwnerId, Vec<usize>>::new();
    for (idx, candidate) in candidates.iter().enumerate() {
        if candidate.status != PeelCandidateStatus::PeelableNow {
            continue;
        }
        for owner_id in &candidate_owner_sets[idx] {
            peelable_candidate_indices_by_owner
                .entry(*owner_id)
                .or_default()
                .push(idx);
        }
    }

    let mut rows = Vec::new();
    let mut minimal_peel_set_ids = BTreeSet::new();
    for (owner_id, bindings) in declared_by_owner {
        let owner_report_id = owner_key(*owner_id);
        let owner_bindings: BTreeSet<&Id> = bindings.iter().collect();
        let mut containing_indices = peelable_candidate_indices_by_owner
            .get(owner_id)
            .cloned()
            .unwrap_or_default();
        containing_indices.sort_by(|a, b| {
            let a = &candidates[*a];
            let b = &candidates[*b];
            (
                a.owner_ids.len(),
                a.members.len(),
                a.members.as_slice(),
                a.id.as_str(),
            )
                .cmp(&(
                    b.owner_ids.len(),
                    b.members.len(),
                    b.members.as_slice(),
                    b.id.as_str(),
                ))
        });
        let mut minimal_options = Vec::<usize>::new();
        for candidate_idx in containing_indices {
            let candidate_owners = &candidate_owner_sets[candidate_idx];
            let has_smaller_containing_set = minimal_options.iter().any(|other_idx| {
                let other_owners = &candidate_owner_sets[*other_idx];
                other_owners.len() < candidate_owners.len()
                    && other_owners.is_subset(candidate_owners)
            });
            if !has_smaller_containing_set {
                minimal_options.push(candidate_idx);
            }
        }

        let status = if minimal_options
            .iter()
            .any(|candidate_idx| candidates[*candidate_idx].owner_ids.len() == 1)
        {
            ResidualOwnerPeelStatus::Direct
        } else if minimal_options.is_empty() {
            ResidualOwnerPeelStatus::Blocked
        } else {
            ResidualOwnerPeelStatus::WithCompanions
        };

        let mut peel_set_ids = Vec::new();
        let mut companion_options = Vec::new();
        for candidate_idx in minimal_options {
            let candidate = &candidates[candidate_idx];
            minimal_peel_set_ids.insert(candidate.id.clone());
            peel_set_ids.push(candidate.id.clone());
            if candidate.owner_ids.len() == 1 {
                continue;
            }
            companion_options.push(ResidualOwnerCompanionOptionReport {
                peel_set_id: candidate.id.clone(),
                companion_owner_ids: candidate
                    .owner_ids
                    .iter()
                    .copied()
                    .map(owner_key)
                    .filter(|id| id != &owner_report_id)
                    .collect(),
                companion_members: binding_reports(
                    factorization,
                    candidate
                        .members
                        .iter()
                        .filter(|member| !owner_bindings.contains(member)),
                ),
            });
        }

        let node = factorization
            .analysis
            .owner_graph
            .node(*owner_id)
            .expect("residual owner horizon should reference an existing owner");
        rows.push(ResidualOwnerPeelHorizonReport {
            owner_id: owner_report_id,
            statement_ordinal: node.statement_ordinal,
            source_location: node.source_location.clone(),
            statement_kind: node.kind,
            purity: node.purity.clone(),
            current_destination: module_report_ref(
                factorization,
                factorization.partition.of(node.id),
            ),
            members: binding_reports(factorization, bindings.iter()),
            status,
            peel_set_ids,
            companion_options,
        });
    }
    (rows, minimal_peel_set_ids)
}

fn build_candidate_owner_sets(candidates: &[PeelCandidateEvaluation]) -> Vec<BTreeSet<OwnerId>> {
    candidates
        .iter()
        .map(|candidate| candidate.owner_ids.iter().copied().collect())
        .collect()
}

fn residual_declared_for_owner(factorization: &ChunkFactorization, node: &OwnerNode) -> Vec<Id> {
    node.declared
        .iter()
        .filter(|id| {
            !matches!(
                factorization.analysis.bindings.get(id),
                Some(BindingKind::Imported { .. })
            )
        })
        .cloned()
        .collect()
}

impl<'a> PeelabilityContext<'a> {
    pub(crate) fn new(
        factorization: &'a ChunkFactorization,
        owner_edges: &'a [OwnerEdge],
        quotient_edges: &[QuotientEdgeReport],
    ) -> Self {
        let owner_count = factorization.analysis.owner_graph.nodes.len();
        let mut owner_out_edges = vec![Vec::new(); owner_count];
        for (idx, edge) in owner_edges.iter().enumerate() {
            if let Some(indices) = owner_out_edges.get_mut(edge.from.0) {
                indices.push(idx);
            }
        }

        let atomic_units = compute_atomic_units(&factorization.analysis.owner_graph);

        // The realizability primitive is the single shared
        // implementation of clause 3 (and clause 2). Candidate
        // evaluation mutates a rollbackable quotient only for the
        // lexical scope of the hypothetical fresh-destination move.
        let realizability = RefCell::new(RealizabilityIndex::from_partition(
            &factorization.analysis.owner_graph,
            factorization.partition.clone(),
        ));

        // Reserve a module-id one past every index currently in use,
        // so the candidate's hypothetical destination is a fresh node
        // in the post-peel quotient. We include logical modules, the
        // partition's actual assignments, and any module-id that
        // appears in the quotient-edges report — covering synthesized
        // residual sentinels and external/vendor modules.
        let mut max_index = factorization.analysis.logical_modules.len();
        for (_, module) in factorization.partition.iter() {
            max_index = max_index.max(module.0.0 + 1);
        }
        for edge in quotient_edges {
            for module in [
                module_id_from_key(&edge.source),
                module_id_from_key(&edge.target),
            ]
            .into_iter()
            .flatten()
            {
                max_index = max_index.max(module.0.0 + 1);
            }
        }
        let fresh_destination = ModuleId(LogicalModuleIndex(max_index));

        Self {
            owner_edges,
            owner_out_edges,
            atomic_units,
            realizability,
            fresh_destination,
        }
    }

    fn owner_out_edge_indices(&self, owner_id: OwnerId) -> &[usize] {
        self.owner_out_edges
            .get(owner_id.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

fn residual_pair_candidates_from_singleton_blockers(
    factorization: &ChunkFactorization,
    singleton_candidates: &[(OwnerId, PeelCandidateEvaluation)],
    owner_edges: &[OwnerEdge],
    declared_by_owner: &BTreeMap<OwnerId, Vec<Id>>,
) -> BTreeSet<(OwnerId, OwnerId)> {
    let mut pair_owner_sets = BTreeSet::new();
    for (owner_id, candidate) in singleton_candidates {
        if candidate.status != PeelCandidateStatus::BlockedCycle {
            continue;
        }
        for &edge_idx in &candidate.constraining_owner_edge_indices {
            let Some(edge) = owner_edges.get(edge_idx) else {
                continue;
            };
            let other = if edge.from == *owner_id {
                edge.to
            } else if edge.to == *owner_id {
                edge.from
            } else {
                continue;
            };
            if declared_by_owner.contains_key(&other)
                && owners_share_residual_destination(factorization, *owner_id, other)
            {
                pair_owner_sets.insert(sorted_owner_pair(*owner_id, other));
            }
        }
    }
    pair_owner_sets
}

/// Emit each multi-owner atomic unit (constraining-edge SCC, per
/// `compute_atomic_units` in `atomic_units.rs`) as a peel-set
/// candidate.
///
/// The atomic unit is the analyzer's own "must move together" notion:
/// every owner inside a single SCC of `G_atomic` is forced to share a
/// destination, because the constraining edges between members make
/// any split unrealizable. Candidates emitted here pass through the
/// same `evaluate_peel_candidate` predicate as `direct`/pair/closure
/// candidates: realizability of the new SCC and residual-dependency
/// check. They show up as `PeelableNow` iff the unit has no outgoing
/// constraining edge into a residual non-member.
///
/// Size-1 units (the common case, where an owner has no constraining
/// peers) are already covered by the singleton `direct` candidates;
/// skipping them here avoids duplicate ids. Units that mix residual
/// owners with non-residual ones are skipped because a partial-unit
/// peel would split the unit by definition. Units whose aggregate
/// `declared` is empty (e.g. a closure of anonymous decorator
/// statements with no class binding) are skipped: there'd be nothing
/// to land in `members[]`.
fn residual_atomic_unit_candidates(
    factorization: &ChunkFactorization,
    context: &PeelabilityContext<'_>,
    declared_by_owner: &BTreeMap<OwnerId, Vec<Id>>,
) -> Vec<PeelCandidateEvaluation> {
    let mut candidates = Vec::new();
    for unit in &context.atomic_units {
        if unit.members.len() < 2 {
            continue;
        }
        if !unit
            .members
            .iter()
            .all(|m| owner_is_in_residual(factorization, *m))
        {
            continue;
        }

        let mut declared = Vec::new();
        for owner in &unit.members {
            if let Some(owner_declared) = declared_by_owner.get(owner) {
                declared.extend(owner_declared.iter().cloned());
            }
        }
        declared.sort();
        declared.dedup();
        if declared.is_empty() {
            continue;
        }

        let owner_ids: Vec<OwnerId> = unit.members.iter().copied().collect();
        let candidate = evaluate_peel_candidate(factorization, context, &owner_ids, declared);
        candidates.push(candidate);
    }
    candidates
}

fn owner_is_in_residual(factorization: &ChunkFactorization, owner_id: OwnerId) -> bool {
    if factorization.analysis.owner_graph.node(owner_id).is_none() {
        return false;
    }
    is_residual_destination(factorization, factorization.partition.of(owner_id))
}

fn residual_dependency_closure_candidates(
    factorization: &ChunkFactorization,
    context: &PeelabilityContext<'_>,
    singleton_candidates: &[(OwnerId, PeelCandidateEvaluation)],
    declared_by_owner: &BTreeMap<OwnerId, Vec<Id>>,
) -> Vec<PeelCandidateEvaluation> {
    let mut closure_index = ResidualDependencyClosureIndex::new(factorization, context);
    let mut seen_components = BTreeSet::<usize>::new();
    let mut candidates = Vec::new();
    for (owner_id, candidate) in singleton_candidates {
        if candidate.status != PeelCandidateStatus::BlockedResidualDependency {
            continue;
        }
        let Some(component_idx) = closure_index.component_for_owner(*owner_id) else {
            continue;
        };
        if !seen_components.insert(component_idx) {
            continue;
        }
        let closure = closure_index.closure_for_component(component_idx);
        if closure.len() <= 1 {
            continue;
        }

        // Aggregate declared bindings across the closure. Empty-declared
        // owners (top-level side-effect statements) are kept in the
        // owner set — moving them is necessary to satisfy
        // sequenced edges to/from named bindings — but they
        // contribute no bindings to the report's `members` list.
        let mut declared = Vec::new();
        for owner in &closure {
            if let Some(owner_declared) = declared_by_owner.get(owner) {
                declared.extend(owner_declared.iter().cloned());
            }
        }
        declared.sort();
        declared.dedup();
        if declared.is_empty() {
            // A closure that consists entirely of anonymous owners is
            // not actionable — there is nothing to land in `members`.
            // (Pure side-effect-only closures don't peel as a separate
            // module; they just stay in residual.)
            continue;
        }

        let candidate = evaluate_peel_candidate(factorization, context, &closure, declared);
        if candidate.status == PeelCandidateStatus::PeelableNow {
            candidates.push(candidate);
        }
    }
    candidates
}

struct ResidualDependencyClosureIndex {
    component_by_owner: Vec<Option<usize>>,
    component_members: Vec<Vec<OwnerId>>,
    component_successors: Vec<Vec<usize>>,
    component_closure_cache: Vec<Option<Vec<OwnerId>>>,
}

impl ResidualDependencyClosureIndex {
    fn new(factorization: &ChunkFactorization, context: &PeelabilityContext<'_>) -> Self {
        let mut graph = DiGraph::<OwnerId, ()>::new();
        let mut node_by_owner = vec![None; factorization.analysis.owner_graph.nodes.len()];
        for node in factorization.analysis.owner_graph.iter_nodes() {
            if !is_residual_destination(factorization, factorization.partition.of(node.id)) {
                continue;
            }
            if let Some(slot) = node_by_owner.get_mut(node.id.0) {
                *slot = Some(graph.add_node(node.id));
            }
        }
        for edge in context.owner_edges {
            let Some(from) = node_by_owner.get(edge.from.0).and_then(|node| *node) else {
                continue;
            };
            let Some(to) = node_by_owner.get(edge.to.0).and_then(|node| *node) else {
                continue;
            };
            graph.add_edge(from, to, ());
        }

        let sccs = tarjan_scc(&graph);
        let mut component_by_node = vec![0usize; graph.node_count()];
        let mut component_members = Vec::with_capacity(sccs.len());
        for (component_idx, scc) in sccs.iter().enumerate() {
            let mut members = Vec::with_capacity(scc.len());
            for &node_idx in scc {
                component_by_node[node_idx.index()] = component_idx;
                members.push(graph[node_idx]);
            }
            members.sort();
            component_members.push(members);
        }

        let component_by_owner = node_by_owner
            .iter()
            .map(|node_idx| node_idx.map(|idx| component_by_node[idx.index()]))
            .collect::<Vec<_>>();
        let mut component_successors = vec![Vec::<usize>::new(); component_members.len()];
        for edge in context.owner_edges {
            let Some(from_component) = component_by_owner.get(edge.from.0).copied().flatten()
            else {
                continue;
            };
            let Some(to_component) = component_by_owner.get(edge.to.0).copied().flatten() else {
                continue;
            };
            if from_component != to_component {
                component_successors[from_component].push(to_component);
            }
        }
        for successors in &mut component_successors {
            successors.sort();
            successors.dedup();
        }
        let component_closure_cache = vec![None; component_members.len()];
        Self {
            component_by_owner,
            component_members,
            component_successors,
            component_closure_cache,
        }
    }

    fn component_for_owner(&self, owner_id: OwnerId) -> Option<usize> {
        self.component_by_owner.get(owner_id.0).copied().flatten()
    }

    fn closure_for_component(&mut self, component_idx: usize) -> Vec<OwnerId> {
        if let Some(closure) = &self.component_closure_cache[component_idx] {
            return closure.clone();
        }
        let mut closure = self.component_members[component_idx].clone();
        for successor in self.component_successors[component_idx].clone() {
            closure.extend(self.closure_for_component(successor));
        }
        closure.sort();
        closure.dedup();
        self.component_closure_cache[component_idx] = Some(closure.clone());
        closure
    }
}

fn owners_share_residual_destination(
    factorization: &ChunkFactorization,
    left: OwnerId,
    right: OwnerId,
) -> bool {
    if factorization.analysis.owner_graph.node(left).is_none()
        || factorization.analysis.owner_graph.node(right).is_none()
    {
        return false;
    }
    let left_module = factorization.partition.of(left);
    let right_module = factorization.partition.of(right);
    left_module == right_module && is_residual_destination(factorization, left_module)
}

fn sorted_owner_pair(left: OwnerId, right: OwnerId) -> (OwnerId, OwnerId) {
    if left <= right {
        (left, right)
    } else {
        (right, left)
    }
}

/// Classify a candidate peel — its yes/no, plus the owner-edge
/// evidence consumers need for diagnostics. The verdict comes from
/// the realizability primitive (DESIGN.md "Realizability primitive"
/// and "Residual peel candidates"): apply the candidate's quotient
/// move to the rollbackable index, read the localized verdict, and
/// undo before returning. The same primitive backs the validator, so
/// a `PeelableNow` here is a `PeelableNow` at the gate.
pub(crate) fn evaluate_peel_candidate(
    factorization: &ChunkFactorization,
    context: &PeelabilityContext<'_>,
    owner_ids: &[OwnerId],
    declared: Vec<Id>,
) -> PeelCandidateEvaluation {
    let moved_owners: BTreeSet<OwnerId> = owner_ids.iter().copied().collect();
    let owner_id_keys: Vec<String> = owner_ids.iter().copied().map(owner_key).collect();
    let candidate_id = format!("peel_candidate:{}", owner_id_keys.join("+"));

    // Clause-1-shaped diagnostic: surface owners the candidate
    // at-init-reads that would remain in the source destination after
    // the peel. Layered on top of the realizability verdict because
    // the primitive does not predict the materializer's emit policy
    // (DESIGN.md "Emit-side responsibilities"); this stays the
    // proposer's stricter check that flags "the moved module would
    // need to import a same-destination at-init read", driving the
    // residual-dependency-closure candidate family.
    let residual_dependency_blocker_owner_ids =
        candidate_source_destination_blocker_owner_ids(factorization, context, &moved_owners);
    if !residual_dependency_blocker_owner_ids.is_empty() {
        return PeelCandidateEvaluation {
            id: candidate_id,
            status: PeelCandidateStatus::BlockedResidualDependency,
            owner_ids: owner_ids.to_vec(),
            members: declared,
            constraining_owner_edge_indices: BTreeSet::new(),
            residual_dependency_blocker_owner_ids,
        };
    }

    // Atomic-unit-split check: the materializer's
    // `assembly_conflicts` enforcement (per DESIGN.md
    // "Factorization proposals") rejects any spec that splits an
    // atomic unit across destination modules. `G_atomic`
    // (`compute_atomic_units`) symmetrizes `LocalEffect` and rebind
    // edges, so an SCC there does not always show up as a multi-
    // module SCC in the literal constraining-edge quotient the
    // realizability primitive walks. Layer this check on top of
    // the verdict so a candidate that would split a unit is
    // surfaced as `BlockedCycle` with the unit's intra-edges as
    // evidence, matching how the pre-unification proposer behaved.
    let atomic_unit_split = candidate_atomic_unit_split_edge_indices(context, &moved_owners);
    if !atomic_unit_split.is_empty() {
        return PeelCandidateEvaluation {
            id: candidate_id,
            status: PeelCandidateStatus::BlockedCycle,
            owner_ids: owner_ids.to_vec(),
            members: declared,
            constraining_owner_edge_indices: atomic_unit_split,
            residual_dependency_blocker_owner_ids: Vec::new(),
        };
    }

    let fresh = context.fresh_destination;
    let verdict = context.realizability.borrow_mut().scoped(
        PartitionDelta::MoveOwners {
            owners: owner_ids.to_vec(),
            to: fresh,
        },
        |index| index.verdict_touching(fresh),
    );

    // The localized verdict only returns SCCs and rebinds involving
    // the candidate's hypothetical destination. Other unrealizable
    // SCCs in the post-peel quotient are pre-existing problems
    // unrelated to the candidate (DESIGN.md: "intentionally ignores
    // unrelated pre-existing bad SCCs").
    let mut constraining_owner_edge_indices = BTreeSet::<usize>::new();
    let mut touches_candidate = false;
    for scc in &verdict.unrealizable_sccs {
        touches_candidate = true;
        for owner_edge_id in &scc.constraining_owner_edges {
            constraining_owner_edge_indices.insert(owner_edge_id.0);
        }
    }
    for rebind in &verdict.cross_rebinds {
        touches_candidate = true;
        constraining_owner_edge_indices.insert(rebind.owner_edge.0);
    }

    let status = if touches_candidate {
        PeelCandidateStatus::BlockedCycle
    } else {
        PeelCandidateStatus::PeelableNow
    };

    PeelCandidateEvaluation {
        id: candidate_id,
        status,
        owner_ids: owner_ids.to_vec(),
        members: declared,
        constraining_owner_edge_indices,
        residual_dependency_blocker_owner_ids: Vec::new(),
    }
}

/// Per-candidate atomic-unit-split check. Returns the constraining
/// owner-edge indices for every atomic unit the candidate would split
/// across modules (at least one member moved, at least one not).
///
/// Layered on top of the realizability primitive because `G_atomic`
/// symmetrizes `LocalEffect` and rebind edges (see
/// `compute_atomic_units`), so a split that the materializer's
/// `assembly_conflicts` enforcement would reject does not always
/// surface as a multi-module SCC in the literal constraining-edge
/// quotient the primitive walks. Mirrors the pre-unification proposer
/// behaviour exactly.
fn candidate_atomic_unit_split_edge_indices(
    context: &PeelabilityContext<'_>,
    moved_owners: &BTreeSet<OwnerId>,
) -> BTreeSet<usize> {
    let mut blocking_edges = BTreeSet::new();
    for unit in &context.atomic_units {
        let mut has_moved = false;
        let mut has_not_moved = false;
        for member in &unit.members {
            if moved_owners.contains(member) {
                has_moved = true;
            } else {
                has_not_moved = true;
            }
            if has_moved && has_not_moved {
                break;
            }
        }
        if !(has_moved && has_not_moved) {
            continue;
        }
        for (idx, edge) in context.owner_edges.iter().enumerate() {
            if edge.reason.kind == DepKind::LazyUse {
                continue;
            }
            if unit.members.contains(&edge.from) && unit.members.contains(&edge.to) {
                blocking_edges.insert(idx);
            }
        }
    }
    blocking_edges
}

/// Blockers are owners *outside* the moved set that the moved set
/// depends on through a `constrains_init_order` edge AND that
/// currently live in the same source destination the moved owners
/// are being peeled from. After the hypothetical peel, those
/// neighbors would be left behind — creating a back-pointer from
/// the new destination to the source. `source_destination` is
/// derived from `moved_owners` (all moved owners share one
/// destination by precondition).
fn candidate_source_destination_blocker_owner_ids(
    factorization: &ChunkFactorization,
    context: &PeelabilityContext<'_>,
    moved_owners: &BTreeSet<OwnerId>,
) -> Vec<OwnerId> {
    let Some(&first) = moved_owners.iter().next() else {
        return Vec::new();
    };
    let source_destination = factorization.partition.of(first);
    let mut blockers = BTreeSet::new();
    for owner_id in moved_owners {
        for &edge_idx in context.owner_out_edge_indices(*owner_id) {
            let edge = &context.owner_edges[edge_idx];
            if !edge.reason.constrains_init_order() {
                continue;
            }
            if moved_owners.contains(&edge.to) {
                continue;
            }
            if factorization.analysis.owner_graph.node(edge.to).is_none() {
                continue;
            }
            if factorization.partition.of(edge.to) != source_destination {
                continue;
            }
            blockers.insert(edge.to);
        }
    }
    blockers.into_iter().collect()
}
