//! Certifying factorize proposal emitter. Reads the in-memory
//! [`Schedule`] and emits only owner sets that have already passed
//! the same peel predicate the materializer uses.
//!
//! The algorithm is a monotone closure over residual frontier starts:
//! begin with residual atomic units, grow through exact repair
//! obligations reported by [`evaluate_peel_candidate`] (split atomic
//! units, same-source residual blockers, private residual emit
//! blockers, and constraining cycle evidence), then emit a proposal
//! only after the closed owner set certifies as `PeelableNow`.
//! Unrepaired or size-capped frontiers are diagnostics, not proposals.
//!
//! Lazy reads are intentionally not atomic/init-order colocation
//! edges, but they still participate in emit-resolvability. A private
//! residual lazy provider is added to the frontier before emission;
//! an importable/exported lazy provider is not.
//!
//! See `FACTORIZE.md` for the broader architecture.

use std::collections::{BTreeSet, HashMap, HashSet};

use crate::atomic_units::compute_atomic_units;
use crate::peelability::{PeelCandidateEvaluation, PeelabilityContext, evaluate_peel_candidate};
use crate::reports::{build_quotient_edge_reports, is_residual_destination, module_key, owner_key};
use crate::{
    BindingName, FactorizeCell, FactorizeDiagnostic, FactorizeDiagnosticReason, FactorizeOptions,
    FactorizeReport, ModuleId, OwnerId, PeelCandidateStatus, Schedule,
};

/// One node of the projected factorize graph H.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum FactorizeNode {
    /// A YAML-claimed logical module, collapsed to a single node.
    /// Residual placeholder modules never appear here.
    Supernode(ModuleId),
    /// A residual owner that stays as its own node in H.
    Loose(OwnerId),
}

pub fn build_factorize_report(schedule: &Schedule, options: &FactorizeOptions) -> FactorizeReport {
    let owner_edges = &schedule.owner_graph.edges;
    let quotient_edges = build_quotient_edge_reports(schedule, owner_edges);
    let context = PeelabilityContext::new(schedule, owner_edges, &quotient_edges);
    let index = FactorizeIndex::new(schedule);

    let node_by_owner: Vec<FactorizeNode> = schedule
        .owner_graph
        .iter_nodes()
        .map(|node| {
            let dest = schedule.partition.of(node.id);
            if is_residual_destination(schedule, dest) {
                FactorizeNode::Loose(node.id)
            } else {
                FactorizeNode::Supernode(dest)
            }
        })
        .collect();
    let owners_by_supernode: HashMap<ModuleId, Vec<OwnerId>> = {
        let mut acc = HashMap::<ModuleId, Vec<OwnerId>>::new();
        for (idx, node) in node_by_owner.iter().enumerate() {
            if let FactorizeNode::Supernode(module) = node {
                acc.entry(*module).or_default().push(OwnerId(idx));
            }
        }
        acc
    };

    let residual_owner_count = node_by_owner
        .iter()
        .filter(|n| matches!(n, FactorizeNode::Loose(_)))
        .count();

    let mut proposals = Vec::<CertifiedProposal>::new();
    let mut diagnostics = Vec::<FactorizeDiagnostic>::new();
    let starts = build_frontier_starts(schedule, &index);
    // Dedup diagnostics across frontier starts that converge on the same
    // closure: each atomic unit grows independently, but different starts
    // can reach the same blocked or oversize set. Emit one row per
    // (reason, closure) equivalence class.
    let mut diagnostics_seen: HashSet<(FactorizeDiagnosticReason, Vec<OwnerId>)> = HashSet::new();
    // Owners that already appeared in an `ExceedsSizeCap` diagnostic. Growth
    // from a seed wholly inside such a closure is forward-only and stays in
    // that closure (blockers come from the seed's own dependency edges,
    // which are a subset of the closure's), so any diagnostic produced
    // there would be redundant. Skipping inside `close_frontier` avoids the
    // growth walk too, not just the duplicate report row.
    let mut oversize_owners = BTreeSet::<OwnerId>::new();
    for start in starts {
        match close_frontier(
            schedule,
            &context,
            &index,
            start,
            options.size_cap_lines,
            &oversize_owners,
        ) {
            FrontierOutcome::Certified(proposal) => proposals.push(proposal),
            FrontierOutcome::Diagnostic(diagnostic) => {
                let key = (
                    diagnostic.reason,
                    diagnostic.owners.iter().copied().collect::<Vec<_>>(),
                );
                if !diagnostics_seen.insert(key) {
                    continue;
                }
                if diagnostic.reason == FactorizeDiagnosticReason::ExceedsSizeCap {
                    oversize_owners.extend(diagnostic.owners.iter().copied());
                }
                diagnostics.push(make_diagnostic(
                    diagnostics.len(),
                    diagnostic.owners,
                    diagnostic.extension_target,
                    &node_by_owner,
                    &diagnostic.verdict,
                    diagnostic.reason,
                    &index.bindings_by_owner,
                    schedule,
                ));
            }
            FrontierOutcome::Empty => {}
        }
    }

    coalesce_certified_proposals(
        schedule,
        &context,
        &index,
        &mut proposals,
        options.size_cap_lines,
    );

    let mut emitted: Vec<FactorizeCell> = proposals
        .into_iter()
        .enumerate()
        .map(|(idx, proposal)| {
            let extension_target = proposal.extension_target;
            let mut cell_owners: HashSet<OwnerId> = proposal.owners.iter().copied().collect();
            if let Some(module) = extension_target {
                if let Some(existing) = owners_by_supernode.get(&module) {
                    cell_owners.extend(existing.iter().copied());
                }
            }
            let proposal_id = match extension_target {
                Some(module) => format!("extend:{}", module_key(module)),
                None => format!("auto_partition_{idx:04}"),
            };
            let extension_owners: HashSet<OwnerId> = if extension_target.is_some() {
                proposal.owners.iter().copied().collect()
            } else {
                HashSet::new()
            };
            make_cell(
                proposal_id,
                cell_owners,
                extension_owners,
                extension_target,
                &node_by_owner,
                &proposal.verdict,
                &index.bindings_by_owner,
                schedule,
            )
        })
        .collect();

    emitted.sort_by(|a, b| {
        b.landable_today.cmp(&a.landable_today).then_with(|| {
            let al = a.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            let bl = b.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            al.cmp(&bl)
        })
    });
    // Renumber fresh-module proposals (`auto_partition_NNNN`) in the
    // post-sort order so cell ids stay stable per sorted slot.
    // Extension proposals keep their `extend:<module>` id (which is
    // already stable across runs).
    let mut fresh_counter = 0usize;
    for cell in emitted.iter_mut() {
        if cell.proposed_module_id.starts_with("auto_partition_") {
            cell.proposed_module_id = format!("auto_partition_{fresh_counter:04}");
            fresh_counter += 1;
        }
    }

    FactorizeReport {
        size_cap_lines: options.size_cap_lines,
        residual_owner_count,
        cells: emitted,
        diagnostics,
    }
}

struct FactorizeIndex {
    unit_by_owner: Vec<usize>,
    owners_by_unit: Vec<Vec<OwnerId>>,
    bindings_by_owner: HashMap<OwnerId, Vec<BindingName>>,
    provider_by_binding: HashMap<BindingName, OwnerId>,
}

impl FactorizeIndex {
    fn new(schedule: &Schedule) -> Self {
        let atomic_units = compute_atomic_units(&schedule.owner_graph);
        let mut unit_by_owner = vec![0usize; schedule.owner_graph.nodes.len()];
        let owners_by_unit: Vec<Vec<OwnerId>> = atomic_units
            .into_iter()
            .enumerate()
            .map(|(unit_idx, unit)| {
                let members: Vec<OwnerId> = unit.members.into_iter().collect();
                for owner in &members {
                    unit_by_owner[owner.0] = unit_idx;
                }
                members
            })
            .collect();
        let mut provider_by_binding = HashMap::<BindingName, OwnerId>::new();
        let bindings_by_owner: HashMap<OwnerId, Vec<BindingName>> = schedule
            .owner_graph
            .iter_nodes()
            .map(|node| {
                let names: Vec<BindingName> = node
                    .declared
                    .iter()
                    .map(|bid| schedule.binding_name(*bid).clone())
                    .collect();
                for name in &names {
                    provider_by_binding.entry(name.clone()).or_insert(node.id);
                }
                (node.id, names)
            })
            .collect();
        Self {
            unit_by_owner,
            owners_by_unit,
            bindings_by_owner,
            provider_by_binding,
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct FrontierStart {
    owners: BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
}

#[derive(Debug, Clone)]
struct CertifiedProposal {
    owners: BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
    verdict: PeelCandidateEvaluation,
}

struct FrontierDiagnostic {
    owners: BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
    verdict: PeelCandidateEvaluation,
    reason: FactorizeDiagnosticReason,
}

enum FrontierOutcome {
    Certified(CertifiedProposal),
    Diagnostic(FrontierDiagnostic),
    Empty,
}

fn build_frontier_starts(schedule: &Schedule, index: &FactorizeIndex) -> Vec<FrontierStart> {
    let mut starts = BTreeSet::<FrontierStart>::new();
    for owners in &index.owners_by_unit {
        let mut residual = BTreeSet::<OwnerId>::new();
        let mut active_targets = BTreeSet::<ModuleId>::new();
        for &owner in owners {
            let dest = schedule.partition.of(owner);
            if is_residual_destination(schedule, dest) {
                residual.insert(owner);
            } else {
                active_targets.insert(dest);
            }
        }
        if residual.is_empty() {
            continue;
        }
        starts.insert(FrontierStart {
            owners: residual,
            extension_target: if active_targets.len() == 1 {
                active_targets.first().copied()
            } else {
                None
            },
        });
    }
    starts.into_iter().collect()
}

fn close_frontier(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    index: &FactorizeIndex,
    start: FrontierStart,
    size_cap_lines: usize,
    oversize_owners: &BTreeSet<OwnerId>,
) -> FrontierOutcome {
    if start.owners.is_empty() {
        return FrontierOutcome::Empty;
    }
    let mut owners = start.owners;
    let mut extension_target = start.extension_target;
    let mut seen = BTreeSet::<(Option<ModuleId>, Vec<OwnerId>)>::new();

    loop {
        if let Some(verdict) =
            close_atomic_units(schedule, index, &mut owners, &mut extension_target)
        {
            return FrontierOutcome::Diagnostic(FrontierDiagnostic {
                owners,
                extension_target,
                verdict,
                reason: FactorizeDiagnosticReason::ActiveModuleConflict,
            });
        }

        let key = (extension_target, owners.iter().copied().collect::<Vec<_>>());
        if !seen.insert(key) {
            let verdict = evaluate_current(schedule, context, index, &owners);
            return FrontierOutcome::Diagnostic(FrontierDiagnostic {
                owners,
                extension_target,
                verdict,
                reason: FactorizeDiagnosticReason::RepeatedFrontier,
            });
        }

        let verdict = evaluate_current(schedule, context, index, &owners);
        let size_lines = owners
            .iter()
            .map(|owner| owner_line_count(schedule, *owner))
            .sum::<usize>();
        if size_lines > size_cap_lines {
            return FrontierOutcome::Diagnostic(FrontierDiagnostic {
                owners,
                extension_target,
                verdict,
                reason: FactorizeDiagnosticReason::ExceedsSizeCap,
            });
        }

        if verdict.status == PeelCandidateStatus::PeelableNow
            || extension_cycle_is_internal_to_target(schedule, &owners, extension_target, &verdict)
        {
            return FrontierOutcome::Certified(CertifiedProposal {
                owners,
                extension_target,
                verdict: certified_verdict(verdict),
            });
        }

        // Bail out once the unrepaired set is wholly inside a previously
        // diagnosed oversize closure: blocker-driven growth from here walks
        // the same dependency edges as the original closure walked, so the
        // final closure is a subset and the diagnostic would duplicate. Run
        // *after* the PeelableNow check so a singleton sub-peel inside a
        // megaclass still certifies.
        if owners.iter().all(|owner| oversize_owners.contains(owner)) {
            return FrontierOutcome::Empty;
        }

        let before = owners.clone();
        let mut conflict = false;
        match verdict.status {
            PeelCandidateStatus::PeelableNow => {}
            PeelCandidateStatus::BlockedResidualDependency => {
                for owner in &verdict.residual_dependency_blocker_owner_ids {
                    if !add_repair_owner(schedule, *owner, &mut owners, &mut extension_target) {
                        conflict = true;
                    }
                }
            }
            PeelCandidateStatus::BlockedEmitResolvability => {
                for binding in &verdict.emit_blocked_residual_bindings {
                    let Some(&provider) = index.provider_by_binding.get(binding) else {
                        conflict = true;
                        continue;
                    };
                    if !add_repair_owner(schedule, provider, &mut owners, &mut extension_target) {
                        conflict = true;
                    }
                }
            }
            PeelCandidateStatus::BlockedCycle => {
                for &edge_idx in &verdict.constraining_owner_edge_indices {
                    let Some(edge) = schedule.owner_graph.edges.get(edge_idx) else {
                        continue;
                    };
                    for endpoint in [edge.from, edge.to] {
                        if owners.contains(&endpoint) {
                            continue;
                        }
                        if !add_repair_owner(schedule, endpoint, &mut owners, &mut extension_target)
                        {
                            conflict = true;
                        }
                    }
                }
            }
        }
        if conflict {
            return FrontierOutcome::Diagnostic(FrontierDiagnostic {
                owners,
                extension_target,
                verdict,
                reason: FactorizeDiagnosticReason::ActiveModuleConflict,
            });
        }
        if owners == before
            && !extension_cycle_is_internal_to_target(schedule, &owners, extension_target, &verdict)
        {
            return FrontierOutcome::Diagnostic(FrontierDiagnostic {
                owners,
                extension_target,
                verdict,
                reason: FactorizeDiagnosticReason::NoExactRepair,
            });
        }
    }
}

fn close_atomic_units(
    schedule: &Schedule,
    index: &FactorizeIndex,
    owners: &mut BTreeSet<OwnerId>,
    extension_target: &mut Option<ModuleId>,
) -> Option<PeelCandidateEvaluation> {
    let mut changed = true;
    while changed {
        changed = false;
        let snapshot: Vec<OwnerId> = owners.iter().copied().collect();
        for owner in snapshot {
            let Some(unit_idx) = index.unit_by_owner.get(owner.0).copied() else {
                continue;
            };
            for &member in &index.owners_by_unit[unit_idx] {
                let dest = schedule.partition.of(member);
                if is_residual_destination(schedule, dest) {
                    changed |= owners.insert(member);
                } else if let Some(target) = extension_target {
                    if *target != dest {
                        return Some(empty_blocked_cycle(owners));
                    }
                } else {
                    *extension_target = Some(dest);
                }
            }
        }
    }
    None
}

fn add_repair_owner(
    schedule: &Schedule,
    owner: OwnerId,
    owners: &mut BTreeSet<OwnerId>,
    extension_target: &mut Option<ModuleId>,
) -> bool {
    let dest = schedule.partition.of(owner);
    if is_residual_destination(schedule, dest) {
        owners.insert(owner);
        true
    } else if let Some(target) = extension_target {
        *target == dest
    } else {
        *extension_target = Some(dest);
        true
    }
}

fn evaluate_current(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    index: &FactorizeIndex,
    owners: &BTreeSet<OwnerId>,
) -> PeelCandidateEvaluation {
    let owner_vec: Vec<OwnerId> = owners.iter().copied().collect();
    let mut declared: Vec<BindingName> = owner_vec
        .iter()
        .flat_map(|o| index.bindings_by_owner.get(o).cloned().unwrap_or_default())
        .collect();
    declared.sort();
    declared.dedup();
    evaluate_peel_candidate(schedule, context, &owner_vec, declared)
}

fn empty_blocked_cycle(owners: &BTreeSet<OwnerId>) -> PeelCandidateEvaluation {
    PeelCandidateEvaluation {
        id: "factorize_conflict".to_string(),
        status: PeelCandidateStatus::BlockedCycle,
        owner_ids: owners.iter().copied().collect(),
        members: Vec::new(),
        constraining_owner_edge_indices: BTreeSet::new(),
        residual_dependency_blocker_owner_ids: Vec::new(),
        emit_blocked_residual_bindings: Vec::new(),
    }
}

fn certified_verdict(mut verdict: PeelCandidateEvaluation) -> PeelCandidateEvaluation {
    verdict.status = PeelCandidateStatus::PeelableNow;
    verdict.constraining_owner_edge_indices.clear();
    verdict.residual_dependency_blocker_owner_ids.clear();
    verdict.emit_blocked_residual_bindings.clear();
    verdict
}

fn extension_cycle_is_internal_to_target(
    schedule: &Schedule,
    owners: &BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
    verdict: &PeelCandidateEvaluation,
) -> bool {
    if verdict.status != PeelCandidateStatus::BlockedCycle {
        return false;
    }
    let Some(target) = extension_target else {
        return false;
    };
    if verdict.constraining_owner_edge_indices.is_empty() {
        return false;
    }
    verdict.constraining_owner_edge_indices.iter().all(|&idx| {
        let Some(edge) = schedule.owner_graph.edges.get(idx) else {
            return true;
        };
        [edge.from, edge.to].into_iter().all(|owner| {
            owners.contains(&owner)
                || schedule
                    .owner_graph
                    .node(owner)
                    .is_some_and(|_| schedule.partition.of(owner) == target)
        })
    })
}

fn coalesce_certified_proposals(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    index: &FactorizeIndex,
    proposals: &mut Vec<CertifiedProposal>,
    size_cap_lines: usize,
) {
    loop {
        let mut merged = false;
        'outer: for left in 0..proposals.len() {
            for right in (left + 1)..proposals.len() {
                if proposals[left].extension_target != proposals[right].extension_target {
                    continue;
                }
                if proposals[left].owners.is_disjoint(&proposals[right].owners) {
                    continue;
                }
                let union: BTreeSet<OwnerId> = proposals[left]
                    .owners
                    .union(&proposals[right].owners)
                    .copied()
                    .collect();
                let size_lines = union
                    .iter()
                    .map(|owner| owner_line_count(schedule, *owner))
                    .sum::<usize>();
                if size_lines > size_cap_lines {
                    continue;
                }
                let verdict = evaluate_current(schedule, context, index, &union);
                if verdict.status != PeelCandidateStatus::PeelableNow
                    && !extension_cycle_is_internal_to_target(
                        schedule,
                        &union,
                        proposals[left].extension_target,
                        &verdict,
                    )
                {
                    continue;
                }
                proposals[left] = CertifiedProposal {
                    owners: union,
                    extension_target: proposals[left].extension_target,
                    verdict: certified_verdict(verdict),
                };
                proposals.remove(right);
                merged = true;
                break 'outer;
            }
        }
        if !merged {
            break;
        }
    }

    proposals.sort_by(|a, b| {
        let a_start = a
            .owners
            .iter()
            .filter_map(|owner| schedule.owner_graph.node(*owner))
            .filter_map(|node| node.source_location.as_ref().map(|loc| loc.start_line))
            .min()
            .unwrap_or(usize::MAX);
        let b_start = b
            .owners
            .iter()
            .filter_map(|owner| schedule.owner_graph.node(*owner))
            .filter_map(|node| node.source_location.as_ref().map(|loc| loc.start_line))
            .min()
            .unwrap_or(usize::MAX);
        a_start
            .cmp(&b_start)
            .then_with(|| a.owners.len().cmp(&b.owners.len()))
    });
}

fn owner_line_count(schedule: &Schedule, owner: OwnerId) -> usize {
    schedule
        .owner_graph
        .node(owner)
        .and_then(|node| node.source_location.as_ref())
        .map(|loc| loc.end_line + 1 - loc.start_line)
        .unwrap_or(1)
}

#[allow(clippy::too_many_arguments)]
fn make_cell(
    proposed_module_id: String,
    owners: HashSet<OwnerId>,
    extension_owners: HashSet<OwnerId>,
    extension_target: Option<ModuleId>,
    node_by_owner: &[FactorizeNode],
    verdict: &PeelCandidateEvaluation,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    schedule: &Schedule,
) -> FactorizeCell {
    let mut owner_ids: Vec<String> = owners.iter().copied().map(owner_key).collect();
    owner_ids.sort();
    let mut extension_owner_ids: Vec<String> =
        extension_owners.iter().copied().map(owner_key).collect();
    extension_owner_ids.sort();
    let extends_module_id: Option<String> = extension_target.map(module_key);

    let mut anonymous_statement_owner_ids: Vec<String> = Vec::new();
    let mut binding_ids_set: HashSet<BindingName> = HashSet::new();
    let mut start_line = usize::MAX;
    let mut end_line: usize = 0;
    let mut have_loc = false;
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    let mut size_lines: usize = 0;
    let empty_vec: Vec<BindingName> = Vec::new();
    for owner_id in &owners {
        let Some(node) = schedule.owner_graph.node(*owner_id) else {
            continue;
        };
        let declared_bindings: &Vec<BindingName> =
            bindings_by_owner.get(owner_id).unwrap_or(&empty_vec);
        if declared_bindings.is_empty() {
            anonymous_statement_owner_ids.push(owner_key(*owner_id));
        }
        for b in declared_bindings {
            binding_ids_set.insert(b.clone());
        }
        if let Some(loc) = &node.source_location {
            have_loc = true;
            start_line = start_line.min(loc.start_line);
            end_line = end_line.max(loc.end_line);
            size_lines = size_lines.saturating_add(loc.end_line + 1 - loc.start_line);
        } else {
            size_lines = size_lines.saturating_add(1);
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }
    anonymous_statement_owner_ids.sort();

    let mut cycle_blocker_owner_ids: Vec<String> = verdict
        .constraining_owner_edge_indices
        .iter()
        .flat_map(|&idx| {
            let edge = &schedule.owner_graph.edges[idx];
            [edge.from, edge.to]
        })
        .filter(|o| !owners.contains(o))
        .map(owner_key)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    cycle_blocker_owner_ids.sort();

    // `active_modules_referenced` walks outgoing constraining edges
    // from this cell in the collapsed graph: any neighbor supernode
    // whose edge survives projection counts. Inter-cell edges that
    // land on a different loose node end up on a different cell; we
    // don't surface them here (the proposal layer handles cell-to-
    // cell relationships in `peel_factorize.rs`).
    let mut active_modules_referenced: HashSet<String> = HashSet::new();
    for &owner_id in &owners {
        for edge in schedule.owner_graph.edges.iter() {
            if edge.from != owner_id {
                continue;
            }
            if owners.contains(&edge.to) {
                continue;
            }
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let target_node = node_by_owner[edge.to.0];
            if let FactorizeNode::Supernode(module) = target_node {
                // Skip back-edges to our own supernode (extension
                // proposal pointing back at the module being
                // extended).
                if extension_target == Some(module) {
                    continue;
                }
                active_modules_referenced.insert(module_key(module));
            }
        }
    }

    let landable_today = matches!(verdict.status, PeelCandidateStatus::PeelableNow);
    let mut binding_ids: Vec<BindingName> = binding_ids_set.into_iter().collect();
    binding_ids.sort();
    let mut active_modules_referenced: Vec<String> =
        active_modules_referenced.into_iter().collect();
    active_modules_referenced.sort();
    FactorizeCell {
        proposed_module_id,
        owner_ids,
        binding_ids,
        anonymous_statement_owner_ids,
        size_lines_estimate: size_lines,
        size_members: owners.len(),
        source_line_range: if have_loc {
            Some([start_line, end_line])
        } else {
            None
        },
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        status: verdict.status,
        landable_today,
        emit_blocked_residual_bindings: verdict.emit_blocked_residual_bindings.clone(),
        cycle_blocker_owner_ids,
        active_modules_referenced,
        extends_module_id,
        extension_owner_ids,
    }
}

#[allow(clippy::too_many_arguments)]
fn make_diagnostic(
    idx: usize,
    owners: BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
    node_by_owner: &[FactorizeNode],
    verdict: &PeelCandidateEvaluation,
    reason: FactorizeDiagnosticReason,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    schedule: &Schedule,
) -> FactorizeDiagnostic {
    let mut owner_ids: Vec<String> = owners.iter().copied().map(owner_key).collect();
    owner_ids.sort();

    let mut binding_ids_set: HashSet<BindingName> = HashSet::new();
    let mut start_line = usize::MAX;
    let mut end_line = 0usize;
    let mut have_loc = false;
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    let mut size_lines = 0usize;
    for owner in &owners {
        if let Some(bindings) = bindings_by_owner.get(owner) {
            binding_ids_set.extend(bindings.iter().cloned());
        }
        let Some(node) = schedule.owner_graph.node(*owner) else {
            continue;
        };
        if let Some(loc) = &node.source_location {
            have_loc = true;
            start_line = start_line.min(loc.start_line);
            end_line = end_line.max(loc.end_line);
            size_lines = size_lines.saturating_add(loc.end_line + 1 - loc.start_line);
        } else {
            size_lines = size_lines.saturating_add(1);
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }

    let mut cycle_blocker_owner_ids: Vec<String> = verdict
        .constraining_owner_edge_indices
        .iter()
        .flat_map(|&edge_idx| {
            let edge = &schedule.owner_graph.edges[edge_idx];
            [edge.from, edge.to]
        })
        .filter(|owner| !owners.contains(owner))
        .map(owner_key)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    cycle_blocker_owner_ids.extend(
        verdict
            .residual_dependency_blocker_owner_ids
            .iter()
            .copied()
            .filter(|owner| !owners.contains(owner))
            .map(owner_key),
    );
    cycle_blocker_owner_ids.sort();
    cycle_blocker_owner_ids.dedup();

    let mut active_modules_referenced: HashSet<String> = HashSet::new();
    for &owner in &owners {
        for edge in schedule.owner_graph.edges.iter() {
            if edge.from != owner || owners.contains(&edge.to) {
                continue;
            }
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let target_node = node_by_owner[edge.to.0];
            if let FactorizeNode::Supernode(module) = target_node {
                if extension_target == Some(module) {
                    continue;
                }
                active_modules_referenced.insert(module_key(module));
            }
        }
    }

    let mut binding_ids: Vec<BindingName> = binding_ids_set.into_iter().collect();
    binding_ids.sort();
    let mut active_modules_referenced: Vec<String> =
        active_modules_referenced.into_iter().collect();
    active_modules_referenced.sort();
    FactorizeDiagnostic {
        diagnostic_id: format!("factorize_diagnostic_{idx:04}"),
        owner_ids,
        binding_ids,
        size_lines_estimate: size_lines,
        size_members: owners.len(),
        source_line_range: if have_loc {
            Some([start_line, end_line])
        } else {
            None
        },
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        status: verdict.status,
        reason,
        emit_blocked_residual_bindings: verdict.emit_blocked_residual_bindings.clone(),
        cycle_blocker_owner_ids,
        active_modules_referenced,
        extends_module_id: extension_target.map(module_key),
    }
}
