use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};

use petgraph::algo::tarjan_scc;
use petgraph::graph::DiGraph;

use crate::graph::{OwnerEdge, peel_emit_blocked_residual_bindings};
use crate::reports::{
    binding_reports, is_residual_destination, module_id_from_key, module_report_ref, owner_key,
};
use crate::{
    BindingKind, BindingName, EvaluatedPeelCandidateReport, LogicalModuleIndex, ModuleId,
    OwnerGraphPeelSetReport, OwnerGraphPeelabilityReport, OwnerId, OwnerNode, PeelCandidateStatus,
    QuotientEdgeReport, ResidualOwnerCompanionOptionReport, ResidualOwnerPeelHorizonReport,
    ResidualOwnerPeelStatus, Schedule,
};

#[derive(Debug, Clone, Default)]
struct CandidateEdgeAccumulator {
    constraining_owner_edge_indices: Vec<usize>,
    constrains_init_order: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum CandidateEdgeDirection {
    FromCandidate,
    ToCandidate,
}

#[derive(Debug, Clone)]
struct CandidateIncidentEdge {
    direction: CandidateEdgeDirection,
    module_idx: usize,
    constraining_owner_edge_indices: Vec<usize>,
    constrains_init_order: bool,
}

#[derive(Debug, Clone, Default)]
struct ModulePairTotals {
    reason_count: usize,
    constraining_reason_count: usize,
    constraining_owner_edge_indices: Vec<usize>,
}

#[derive(Debug, Clone)]
struct ModuleAdjEdge {
    pair: (ModuleId, ModuleId),
    target_idx: usize,
}

#[derive(Debug, Clone)]
struct ReverseModuleAdjEdge {
    pair: (ModuleId, ModuleId),
    source_idx: usize,
}

pub(crate) struct PeelabilityContext<'a> {
    owner_edges: &'a [OwnerEdge],
    owner_out_edges: Vec<Vec<usize>>,
    owner_in_edges: Vec<Vec<usize>>,
    module_index: HashMap<ModuleId, usize>,
    modules: Vec<ModuleId>,
    forward_edges: Vec<Vec<ModuleAdjEdge>>,
    reverse_edges: Vec<Vec<ReverseModuleAdjEdge>>,
    module_pair_totals: HashMap<(ModuleId, ModuleId), ModulePairTotals>,
}

#[derive(Debug, Clone, Default)]
struct CandidateGraphAdjustment {
    removed_reason_count: HashMap<(ModuleId, ModuleId), usize>,
    removed_constraining_reason_count: HashMap<(ModuleId, ModuleId), usize>,
    removed_owner_edge_indices: HashSet<usize>,
}

#[derive(Debug, Clone)]
pub(crate) struct PeelCandidateEvaluation {
    pub(crate) id: String,
    pub(crate) status: PeelCandidateStatus,
    pub(crate) owner_ids: Vec<OwnerId>,
    pub(crate) members: Vec<BindingName>,
    pub(crate) constraining_owner_edge_indices: BTreeSet<usize>,
    /// Owner ids whose residual dependency forced the candidate into
    /// `BlockedResidualDependency`. Empty for other statuses. Surfaces
    /// in `evaluated_owner_sets[].residual_dependency_blockers` so
    /// callers can pinpoint which residual neighbor blocked the peel.
    pub(crate) residual_dependency_blocker_owner_ids: Vec<OwnerId>,
    /// Residual binding names that the candidate's moved bodies
    /// reference but entry doesn't export — populated only when
    /// `status == BlockedEmitResolvability`. SSOT'd via
    /// [`peel_emit_blocked_residual_bindings`] in `graph.rs`.
    pub(crate) emit_blocked_residual_bindings: Vec<BindingName>,
}

pub(crate) fn build_peelability_report(
    schedule: &Schedule,
    owner_edges: &[OwnerEdge],
    quotient_edges: &[QuotientEdgeReport],
) -> OwnerGraphPeelabilityReport {
    let residual_destinations: BTreeSet<ModuleId> = schedule
        .owner_graph
        .iter_nodes()
        .filter_map(|node| {
            let module = schedule.partition.of(node.id);
            is_residual_destination(schedule, module).then_some(module)
        })
        .collect();

    let context = PeelabilityContext::new(schedule, owner_edges, quotient_edges);
    let mut declared_by_owner = BTreeMap::<OwnerId, Vec<BindingName>>::new();
    for node in schedule.owner_graph.iter_nodes() {
        if !is_residual_destination(schedule, schedule.partition.of(node.id)) {
            continue;
        }
        let declared = residual_declared_for_owner(schedule, node);
        if declared.is_empty() {
            continue;
        }
        declared_by_owner.insert(node.id, declared);
    }

    let mut singleton_candidates = Vec::<(OwnerId, PeelCandidateEvaluation)>::new();
    for (&owner_id, declared) in &declared_by_owner {
        singleton_candidates.push((
            owner_id,
            evaluate_residual_peel_candidate(schedule, &context, &[owner_id], declared.clone()),
        ));
    }

    let pair_owner_sets = residual_pair_candidates_from_singleton_blockers(
        schedule,
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

        let candidate =
            evaluate_residual_peel_candidate(schedule, &context, &[left, right], declared);
        if candidate.status == PeelCandidateStatus::PeelableNow {
            pair_candidates.push(candidate);
        }
    }

    let dependency_closure_candidates = residual_dependency_closure_candidates(
        schedule,
        &context,
        &singleton_candidates,
        &declared_by_owner,
    );

    let mut candidates: Vec<PeelCandidateEvaluation> = singleton_candidates
        .into_iter()
        .map(|(_, candidate)| candidate)
        .collect();
    candidates.extend(pair_candidates);
    candidates.extend(dependency_closure_candidates);

    let (residual_owner_horizon, minimal_peel_set_ids) =
        build_residual_owner_horizon(schedule, &declared_by_owner, &candidates);
    let minimal_peel_sets = candidates
        .iter()
        .filter(|candidate| minimal_peel_set_ids.contains(&candidate.id))
        .map(|candidate| OwnerGraphPeelSetReport {
            candidate_id: candidate.id.clone(),
            owner_ids: candidate.owner_ids.iter().copied().map(owner_key).collect(),
            members: binding_reports(schedule, candidate.members.iter()),
            emit_blocked_residual_bindings: candidate.emit_blocked_residual_bindings.clone(),
        })
        .collect();

    let evaluated_owner_sets = candidates
        .iter()
        .map(|candidate| EvaluatedPeelCandidateReport {
            candidate_id: candidate.id.clone(),
            owner_ids: candidate.owner_ids.iter().copied().map(owner_key).collect(),
            members: binding_reports(schedule, candidate.members.iter()),
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
            emit_blocked_residual_bindings: candidate.emit_blocked_residual_bindings.clone(),
        })
        .collect();

    OwnerGraphPeelabilityReport {
        residual_destinations: residual_destinations
            .into_iter()
            .map(|id| module_report_ref(schedule, id))
            .collect(),
        minimal_peel_sets,
        residual_owner_horizon,
        evaluated_owner_sets,
    }
}

fn build_residual_owner_horizon(
    schedule: &Schedule,
    declared_by_owner: &BTreeMap<OwnerId, Vec<BindingName>>,
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
        let owner_bindings: BTreeSet<&str> = bindings.iter().map(String::as_str).collect();
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
                    schedule,
                    candidate
                        .members
                        .iter()
                        .filter(|member| !owner_bindings.contains(member.as_str())),
                ),
            });
        }

        let node = schedule
            .owner_graph
            .node(*owner_id)
            .expect("residual owner horizon should reference an existing owner");
        rows.push(ResidualOwnerPeelHorizonReport {
            owner_id: owner_report_id,
            statement_ordinal: node.statement_ordinal,
            source_location: node.source_location.clone(),
            statement_kind: node.kind,
            purity: node.purity.clone(),
            current_destination: module_report_ref(schedule, schedule.partition.of(node.id)),
            members: binding_reports(schedule, bindings.iter()),
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

fn residual_declared_for_owner(schedule: &Schedule, node: &OwnerNode) -> Vec<BindingName> {
    node.declared
        .iter()
        .map(|binding| schedule.binding_name(*binding))
        .filter(|name| {
            !matches!(
                schedule.bindings.get(*name),
                Some(BindingKind::Imported { .. })
            )
        })
        .cloned()
        .collect()
}

impl<'a> PeelabilityContext<'a> {
    pub(crate) fn new(
        schedule: &Schedule,
        owner_edges: &'a [OwnerEdge],
        quotient_edges: &[QuotientEdgeReport],
    ) -> Self {
        let mut modules = BTreeSet::<ModuleId>::new();
        modules.insert(ModuleId::ResidualEntry);
        for idx in 0..schedule.logical_modules.len() {
            modules.insert(ModuleId::Logical(LogicalModuleIndex(idx)));
        }
        for (_, module) in schedule.partition.iter() {
            modules.insert(module);
        }
        for edge in quotient_edges {
            if let Some(source) = module_id_from_key(&edge.source) {
                modules.insert(source);
            }
            if let Some(target) = module_id_from_key(&edge.target) {
                modules.insert(target);
            }
        }
        let modules: Vec<ModuleId> = modules.into_iter().collect();
        let module_index: HashMap<ModuleId, usize> = modules
            .iter()
            .copied()
            .enumerate()
            .map(|(idx, id)| (id, idx))
            .collect();

        let owner_count = schedule.owner_graph.nodes.len();
        let mut owner_out_edges = vec![Vec::new(); owner_count];
        let mut owner_in_edges = vec![Vec::new(); owner_count];
        let mut module_pair_totals = HashMap::<(ModuleId, ModuleId), ModulePairTotals>::new();
        for (idx, edge) in owner_edges.iter().enumerate() {
            if let Some(indices) = owner_out_edges.get_mut(edge.from.0) {
                indices.push(idx);
            }
            if let Some(indices) = owner_in_edges.get_mut(edge.to.0) {
                indices.push(idx);
            }

            if schedule.owner_graph.node(edge.from).is_none()
                || schedule.owner_graph.node(edge.to).is_none()
            {
                continue;
            }
            let from = schedule.partition.of(edge.from);
            let to = schedule.partition.of(edge.to);
            if from == to {
                continue;
            }
            let totals = module_pair_totals.entry((from, to)).or_default();
            totals.reason_count += 1;
            if edge.reason.constrains_init_order() {
                totals.constraining_reason_count += 1;
                totals.constraining_owner_edge_indices.push(idx);
            }
        }

        let mut forward_edges = vec![Vec::new(); modules.len()];
        let mut reverse_edges = vec![Vec::new(); modules.len()];
        for &(source, target) in module_pair_totals.keys() {
            let Some(&source_idx) = module_index.get(&source) else {
                continue;
            };
            let Some(&target_idx) = module_index.get(&target) else {
                continue;
            };
            forward_edges[source_idx].push(ModuleAdjEdge {
                pair: (source, target),
                target_idx,
            });
            reverse_edges[target_idx].push(ReverseModuleAdjEdge {
                pair: (source, target),
                source_idx,
            });
        }

        Self {
            owner_edges,
            owner_out_edges,
            owner_in_edges,
            module_index,
            modules,
            forward_edges,
            reverse_edges,
            module_pair_totals,
        }
    }

    fn module_idx(&self, module: ModuleId) -> Option<usize> {
        self.module_index.get(&module).copied()
    }

    fn owner_out_edge_indices(&self, owner_id: OwnerId) -> &[usize] {
        self.owner_out_edges
            .get(owner_id.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    fn owner_in_edge_indices(&self, owner_id: OwnerId) -> &[usize] {
        self.owner_in_edges
            .get(owner_id.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    fn current_edge_remains(
        &self,
        pair: (ModuleId, ModuleId),
        adjustment: &CandidateGraphAdjustment,
    ) -> bool {
        let Some(totals) = self.module_pair_totals.get(&pair) else {
            return false;
        };
        let removed = adjustment
            .removed_reason_count
            .get(&pair)
            .copied()
            .unwrap_or(0);
        totals.reason_count > removed
    }

    fn current_edge_constrains(
        &self,
        pair: (ModuleId, ModuleId),
        adjustment: &CandidateGraphAdjustment,
    ) -> bool {
        let Some(totals) = self.module_pair_totals.get(&pair) else {
            return false;
        };
        let removed = adjustment
            .removed_constraining_reason_count
            .get(&pair)
            .copied()
            .unwrap_or(0);
        totals.constraining_reason_count > removed
    }
}

fn residual_pair_candidates_from_singleton_blockers(
    schedule: &Schedule,
    singleton_candidates: &[(OwnerId, PeelCandidateEvaluation)],
    owner_edges: &[OwnerEdge],
    declared_by_owner: &BTreeMap<OwnerId, Vec<BindingName>>,
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
                && owners_share_residual_destination(schedule, *owner_id, other)
            {
                pair_owner_sets.insert(sorted_owner_pair(*owner_id, other));
            }
        }
    }
    pair_owner_sets
}

fn residual_dependency_closure_candidates(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    singleton_candidates: &[(OwnerId, PeelCandidateEvaluation)],
    declared_by_owner: &BTreeMap<OwnerId, Vec<BindingName>>,
) -> Vec<PeelCandidateEvaluation> {
    let mut closure_index = ResidualDependencyClosureIndex::new(schedule, context);
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

        let candidate = evaluate_residual_peel_candidate(schedule, context, &closure, declared);
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
    fn new(schedule: &Schedule, context: &PeelabilityContext<'_>) -> Self {
        let mut graph = DiGraph::<OwnerId, ()>::new();
        let mut node_by_owner = vec![None; schedule.owner_graph.nodes.len()];
        for node in schedule.owner_graph.iter_nodes() {
            if !is_residual_destination(schedule, schedule.partition.of(node.id)) {
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

fn owners_share_residual_destination(schedule: &Schedule, left: OwnerId, right: OwnerId) -> bool {
    if schedule.owner_graph.node(left).is_none() || schedule.owner_graph.node(right).is_none() {
        return false;
    }
    let left_module = schedule.partition.of(left);
    let right_module = schedule.partition.of(right);
    left_module == right_module && is_residual_destination(schedule, left_module)
}

fn sorted_owner_pair(left: OwnerId, right: OwnerId) -> (OwnerId, OwnerId) {
    if left <= right {
        (left, right)
    } else {
        (right, left)
    }
}

pub(crate) fn evaluate_residual_peel_candidate(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    owner_ids: &[OwnerId],
    declared: Vec<BindingName>,
) -> PeelCandidateEvaluation {
    let moved_owners: BTreeSet<OwnerId> = owner_ids.iter().copied().collect();
    let owner_id_keys: Vec<String> = owner_ids.iter().copied().map(owner_key).collect();
    let candidate_id = format!("peel_candidate:{}", owner_id_keys.join("+"));
    let residual_dependency_blocker_owner_ids =
        candidate_residual_dependency_blocker_owner_ids(schedule, context, &moved_owners);
    let has_residual_dependency = !residual_dependency_blocker_owner_ids.is_empty();
    let cross_destination_write_edge_indices =
        candidate_cross_destination_write_edge_indices(context, &moved_owners);
    let constraining_owner_edge_indices = if has_residual_dependency {
        BTreeSet::new()
    } else if !cross_destination_write_edge_indices.is_empty() {
        cross_destination_write_edge_indices
    } else {
        let (candidate_edges, adjustment) =
            candidate_incident_edges(schedule, context, &moved_owners);
        candidate_blocking_scc_owner_edge_indices(context, &candidate_edges, &adjustment)
    };

    // Emit-resolvability projection: even if the candidate passes
    // cycle/realizability checks, `materialize_logical_modules` will
    // reject it when a moved body references a residual entry binding
    // that isn't on entry's export list. Compute that here using the
    // shared predicate so peelability and the materializer can't
    // drift (cf. `constrains_init_order` SSOT in f86e84b7e).
    //
    // Skipped when the schedule was built without AST analysis (no
    // `pre_existing_entry_exports` set) — that's the test-helper case
    // where there's no chunk source to derive exports from. Real
    // pipeline runs always populate the set.
    let blocks_via_cycle = !constraining_owner_edge_indices.is_empty();
    let emit_blocked_residual_bindings: Vec<BindingName> =
        if has_residual_dependency || blocks_via_cycle {
            // Already blocked for a stronger reason; no need to also
            // surface emit-resolvability blockers.
            Vec::new()
        } else {
            match schedule.entry_exported_binding_names() {
                Some(base_exports) => peel_emit_blocked_residual_bindings(
                    &schedule.owner_graph,
                    &schedule.partition,
                    &moved_owners,
                    base_exports,
                    &declared,
                )
                .into_iter()
                .collect(),
                None => Vec::new(),
            }
        };

    let status = if has_residual_dependency {
        PeelCandidateStatus::BlockedResidualDependency
    } else if blocks_via_cycle {
        PeelCandidateStatus::BlockedCycle
    } else if !emit_blocked_residual_bindings.is_empty() {
        PeelCandidateStatus::BlockedEmitResolvability
    } else {
        PeelCandidateStatus::PeelableNow
    };

    PeelCandidateEvaluation {
        id: candidate_id,
        status,
        owner_ids: owner_ids.to_vec(),
        members: declared,
        constraining_owner_edge_indices,
        residual_dependency_blocker_owner_ids,
        emit_blocked_residual_bindings,
    }
}

fn candidate_cross_destination_write_edge_indices(
    context: &PeelabilityContext<'_>,
    moved_owners: &BTreeSet<OwnerId>,
) -> BTreeSet<usize> {
    let mut edge_indices = BTreeSet::new();
    for owner_id in moved_owners {
        edge_indices.extend(context.owner_out_edge_indices(*owner_id).iter().copied());
        edge_indices.extend(context.owner_in_edge_indices(*owner_id).iter().copied());
    }
    edge_indices
        .into_iter()
        .filter(|edge_idx| {
            let edge = &context.owner_edges[*edge_idx];
            edge.reason.is_rebind()
                && moved_owners.contains(&edge.from) != moved_owners.contains(&edge.to)
        })
        .collect()
}

fn candidate_residual_dependency_blocker_owner_ids(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    moved_owners: &BTreeSet<OwnerId>,
) -> Vec<OwnerId> {
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
            if schedule.owner_graph.node(edge.to).is_none() {
                continue;
            }
            if schedule.partition.of(edge.to) != ModuleId::ResidualEntry {
                continue;
            }
            blockers.insert(edge.to);
        }
    }
    blockers.into_iter().collect()
}

fn candidate_incident_edges(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    moved_owners: &BTreeSet<OwnerId>,
) -> (Vec<CandidateIncidentEdge>, CandidateGraphAdjustment) {
    let mut edge_indices = BTreeSet::new();
    for owner_id in moved_owners {
        edge_indices.extend(context.owner_out_edge_indices(*owner_id).iter().copied());
        edge_indices.extend(context.owner_in_edge_indices(*owner_id).iter().copied());
    }

    let mut adjustment = CandidateGraphAdjustment::default();
    let mut accum = HashMap::<(CandidateEdgeDirection, ModuleId), CandidateEdgeAccumulator>::new();
    let mut seen_side_effect_candidate_pairs = HashSet::<(CandidateEdgeDirection, ModuleId)>::new();

    for edge_idx in edge_indices {
        let edge = &context.owner_edges[edge_idx];
        adjustment.removed_owner_edge_indices.insert(edge_idx);

        if schedule.owner_graph.node(edge.from).is_none()
            || schedule.owner_graph.node(edge.to).is_none()
        {
            continue;
        }
        let old_from = schedule.partition.of(edge.from);
        let old_to = schedule.partition.of(edge.to);
        if old_from != old_to {
            *adjustment
                .removed_reason_count
                .entry((old_from, old_to))
                .or_insert(0) += 1;
            if edge.reason.constrains_init_order() {
                *adjustment
                    .removed_constraining_reason_count
                    .entry((old_from, old_to))
                    .or_insert(0) += 1;
            }
        }

        let from_moved = moved_owners.contains(&edge.from);
        let to_moved = moved_owners.contains(&edge.to);
        if from_moved == to_moved {
            continue;
        }
        let (direction, module) = if from_moved {
            (CandidateEdgeDirection::FromCandidate, old_to)
        } else {
            (CandidateEdgeDirection::ToCandidate, old_from)
        };
        if edge.reason.is_sequenced()
            && !seen_side_effect_candidate_pairs.insert((direction, module))
        {
            continue;
        }
        let entry = accum.entry((direction, module)).or_default();
        if edge.reason.constrains_init_order() {
            entry.constraining_owner_edge_indices.push(edge_idx);
        }
        entry.constrains_init_order |= edge.reason.constrains_init_order();
    }

    let mut candidate_edges = Vec::new();
    for ((direction, module), entry) in accum {
        let Some(module_idx) = context.module_idx(module) else {
            continue;
        };
        candidate_edges.push(CandidateIncidentEdge {
            direction,
            module_idx,
            constraining_owner_edge_indices: entry.constraining_owner_edge_indices,
            constrains_init_order: entry.constrains_init_order,
        });
    }

    (candidate_edges, adjustment)
}

fn candidate_blocking_scc_owner_edge_indices(
    context: &PeelabilityContext<'_>,
    candidate_edges: &[CandidateIncidentEdge],
    adjustment: &CandidateGraphAdjustment,
) -> BTreeSet<usize> {
    let mut forward = vec![false; context.modules.len()];
    let mut backward = vec![false; context.modules.len()];
    let mut queue = VecDeque::new();
    for edge in candidate_edges
        .iter()
        .filter(|edge| edge.direction == CandidateEdgeDirection::FromCandidate)
    {
        if !forward[edge.module_idx] {
            forward[edge.module_idx] = true;
            queue.push_back(edge.module_idx);
        }
    }
    while let Some(source_idx) = queue.pop_front() {
        for edge in &context.forward_edges[source_idx] {
            if !context.current_edge_remains(edge.pair, adjustment) {
                continue;
            }
            if !forward[edge.target_idx] {
                forward[edge.target_idx] = true;
                queue.push_back(edge.target_idx);
            }
        }
    }

    queue.clear();
    for edge in candidate_edges
        .iter()
        .filter(|edge| edge.direction == CandidateEdgeDirection::ToCandidate)
    {
        if !backward[edge.module_idx] {
            backward[edge.module_idx] = true;
            queue.push_back(edge.module_idx);
        }
    }
    while let Some(target_idx) = queue.pop_front() {
        for edge in &context.reverse_edges[target_idx] {
            if !context.current_edge_remains(edge.pair, adjustment) {
                continue;
            }
            if !backward[edge.source_idx] {
                backward[edge.source_idx] = true;
                queue.push_back(edge.source_idx);
            }
        }
    }

    let mut in_scc = vec![false; context.modules.len()];
    let mut has_cycle = false;
    for idx in 0..context.modules.len() {
        in_scc[idx] = forward[idx] && backward[idx];
        has_cycle |= in_scc[idx];
    }
    if !has_cycle {
        return BTreeSet::new();
    }

    let mut constraining_owner_edge_ids = BTreeSet::new();

    for edge in candidate_edges {
        if !in_scc[edge.module_idx] {
            continue;
        }
        if edge.constrains_init_order {
            constraining_owner_edge_ids
                .extend(edge.constraining_owner_edge_indices.iter().copied());
        }
    }

    for (source_idx, module_edges) in context.forward_edges.iter().enumerate() {
        if !in_scc[source_idx] {
            continue;
        }
        for edge in module_edges {
            if !in_scc[edge.target_idx] || !context.current_edge_remains(edge.pair, adjustment) {
                continue;
            }
            if let Some(totals) = context.module_pair_totals.get(&edge.pair) {
                if context.current_edge_constrains(edge.pair, adjustment) {
                    for edge_idx in &totals.constraining_owner_edge_indices {
                        if adjustment.removed_owner_edge_indices.contains(edge_idx) {
                            continue;
                        }
                        constraining_owner_edge_ids.insert(*edge_idx);
                    }
                }
            }
        }
    }

    constraining_owner_edge_ids
}
