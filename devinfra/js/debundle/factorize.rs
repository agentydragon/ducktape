//! Algorithmic peel proposer that uses the materializer's
//! gate predicate (single source of truth) inline.
//!
//! Reads the in-memory `Schedule` directly so per-cell verdicts
//! come from the same `evaluate_residual_peel_candidate` predicate
//! the analyzer's peelability pass uses (cycle, lazy-rebind,
//! emit-resolvability). Cells that the predicate flags as blocked
//! get auto-grown by absorbing the specific owners the predicate
//! pointed at — adjacent anonymous side-effect statements, owners
//! of emit-blocked free-reference bindings, etc. The output cells
//! are therefore proposals the materializer will accept, including
//! cells whose members mix top-level bindings with the side-effect
//! statements they need to travel with.
//!
//! Compared to the JSON-only factorizer (`@ducktape//peel_factorize`),
//! this module:
//! - Calls into the analyzer's SSOT predicate instead of replicating
//!   the gate logic against the serialized owner-graph shape.
//! - Auto-grows cells via the same predicate when blockers point at
//!   addressable absorption targets.
//! - Emits its report as a side-channel of `OwnerGraphReport` so
//!   downstream consumers (the CLI) read pre-computed proposals
//!   instead of rebuilding them.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::peelability::{
    PeelCandidateEvaluation, PeelabilityContext, evaluate_residual_peel_candidate,
};
use crate::reports::{build_quotient_edge_reports, module_key, owner_key};
use crate::{
    BindingName, DepKind, FactorizeCell, FactorizeOptions, FactorizeReport, ModuleId, OwnerId,
    PeelCandidateStatus, Schedule,
};

pub fn build_factorize_report(schedule: &Schedule, options: &FactorizeOptions) -> FactorizeReport {
    let owner_edges = &schedule.owner_graph.edges;
    let quotient_edges = build_quotient_edge_reports(schedule, owner_edges);
    let context = PeelabilityContext::new(schedule, owner_edges, &quotient_edges);

    // Residual owners = those whose partition is `ModuleId::ResidualEntry`.
    // Matches the analyzer's SSOT residual definition.
    let residual: BTreeSet<OwnerId> = schedule
        .owner_graph
        .iter_nodes()
        .filter(|node| matches!(schedule.partition.of(node.id), ModuleId::ResidualEntry))
        .map(|node| node.id)
        .collect();
    let residual_owner_count = residual.len();

    // Owner → declared bindings, used to seed the predicate's `declared`
    // input and to compute the cell's binding membership list.
    let bindings_by_owner: HashMap<OwnerId, Vec<BindingName>> = schedule
        .owner_graph
        .iter_nodes()
        .map(|node| {
            let names: Vec<BindingName> = node
                .declared
                .iter()
                .map(|bid| schedule.binding_name(*bid).clone())
                .collect();
            (node.id, names)
        })
        .collect();

    let owner_for_binding: HashMap<BindingName, OwnerId> = bindings_by_owner
        .iter()
        .flat_map(|(&owner_id, names)| names.iter().cloned().map(move |name| (name, owner_id)))
        .collect();

    // Initial cell formation: SCC condensation on the constraining
    // edge subgraph between residual owners, then a rebind-edge
    // union to fold cross-cell rebind targets together.
    let sccs = strongly_connected_components(&residual, owner_edges);
    let mut cells: Vec<BTreeSet<OwnerId>> = sccs.into_iter().collect();
    apply_rebind_union(&mut cells, &residual, owner_edges);

    // Greedy agglomerative merge: pair cells with the strongest
    // constraining-edge connection, merge if combined lines fit
    // under the cap. Loop until no admissible merge remains.
    agglomerate(
        &mut cells,
        &residual,
        owner_edges,
        schedule,
        options.size_cap_lines,
    );

    // Per-cell evaluation + auto-grow.
    let mut emitted: Vec<FactorizeCell> = Vec::with_capacity(cells.len());
    for (idx, cell_owners) in cells.iter().enumerate() {
        let (final_owners, verdict, iterations) = evaluate_and_grow(
            schedule,
            &context,
            cell_owners.clone(),
            &bindings_by_owner,
            &owner_for_binding,
            &residual,
            options,
        );
        let proposed_module_id = format!("auto_partition_{idx:04}");
        emitted.push(make_cell(
            proposed_module_id,
            final_owners,
            &verdict,
            &bindings_by_owner,
            schedule,
            iterations,
            options.size_cap_lines,
        ));
    }

    // Stable sort: landable cells first, then by source line.
    emitted.sort_by(|a, b| {
        b.landable_today.cmp(&a.landable_today).then_with(|| {
            let al = a.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            let bl = b.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            al.cmp(&bl)
        })
    });
    // Renumber after sort.
    for (idx, cell) in emitted.iter_mut().enumerate() {
        cell.proposed_module_id = format!("auto_partition_{idx:04}");
    }

    FactorizeReport {
        size_cap_lines: options.size_cap_lines,
        residual_owner_count,
        cells: emitted,
    }
}

/// Per-cell evaluate-and-grow loop. Calls the SSOT predicate; if
/// blocked by an addressable category (emit-resolvability, cycle
/// through another residual cell), tries absorbing the owners the
/// blocker pointed at and re-evaluates. Bounded by
/// `options.max_grow_iterations`.
fn evaluate_and_grow(
    schedule: &Schedule,
    context: &PeelabilityContext<'_>,
    mut cell: BTreeSet<OwnerId>,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    owner_for_binding: &HashMap<BindingName, OwnerId>,
    residual: &BTreeSet<OwnerId>,
    options: &FactorizeOptions,
) -> (BTreeSet<OwnerId>, PeelCandidateEvaluation, usize) {
    let mut iterations = 0;
    loop {
        let owners: Vec<OwnerId> = cell.iter().copied().collect();
        let declared: Vec<BindingName> = owners
            .iter()
            .flat_map(|o| bindings_by_owner.get(o).cloned().unwrap_or_default())
            .collect();
        let verdict = evaluate_residual_peel_candidate(schedule, context, &owners, declared);
        if iterations >= options.max_grow_iterations {
            return (cell, verdict, iterations);
        }
        match verdict.status {
            PeelCandidateStatus::PeelableNow => return (cell, verdict, iterations),
            PeelCandidateStatus::BlockedEmitResolvability => {
                // Absorb the owners declaring each emit-blocked binding.
                let mut grew = false;
                for binding in &verdict.emit_blocked_residual_bindings {
                    if let Some(&owner) = owner_for_binding.get(binding)
                        && residual.contains(&owner)
                        && !cell.contains(&owner)
                    {
                        cell.insert(owner);
                        grew = true;
                    }
                }
                if !grew {
                    return (cell, verdict, iterations);
                }
            }
            PeelCandidateStatus::BlockedCycle | PeelCandidateStatus::BlockedResidualDependency => {
                // Absorb residual neighbors flagged as blockers.
                let mut grew = false;
                for owner in &verdict.residual_dependency_blocker_owner_ids {
                    if residual.contains(owner) && !cell.contains(owner) {
                        cell.insert(*owner);
                        grew = true;
                    }
                }
                // Cycle blockers — absorb the constraining-edge endpoints
                // not in this cell.
                for &edge_idx in &verdict.constraining_owner_edge_indices {
                    let edge = &schedule.owner_graph.edges[edge_idx];
                    for endpoint in [edge.from, edge.to] {
                        if residual.contains(&endpoint) && !cell.contains(&endpoint) {
                            cell.insert(endpoint);
                            grew = true;
                        }
                    }
                }
                if !grew {
                    return (cell, verdict, iterations);
                }
            }
        }
        iterations += 1;
    }
}

fn make_cell(
    proposed_module_id: String,
    owners: BTreeSet<OwnerId>,
    verdict: &PeelCandidateEvaluation,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    schedule: &Schedule,
    auto_grow_iterations: usize,
    size_cap_lines: usize,
) -> FactorizeCell {
    let mut owner_ids: Vec<String> = owners.iter().copied().map(owner_key).collect();
    owner_ids.sort();

    let mut anonymous_statement_owner_ids: Vec<String> = Vec::new();
    let mut binding_ids_set: BTreeSet<BindingName> = BTreeSet::new();
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
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }
    anonymous_statement_owner_ids.sort();

    let cycle_blocker_owner_ids: Vec<String> = verdict
        .constraining_owner_edge_indices
        .iter()
        .flat_map(|&idx| {
            let edge = &schedule.owner_graph.edges[idx];
            [edge.from, edge.to]
        })
        .filter(|o| !owners.contains(o))
        .map(owner_key)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();

    // Outgoing constraining edges to non-cell owners that landed in
    // non-residual destinations — these are the "active modules
    // referenced" the cell would import from after promotion.
    let mut active_modules_referenced: BTreeSet<String> = BTreeSet::new();
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
            let dest = schedule.partition.of(edge.to);
            if matches!(dest, ModuleId::ResidualEntry) {
                continue;
            }
            active_modules_referenced.insert(module_key(dest));
        }
    }

    let landable_today = matches!(verdict.status, PeelCandidateStatus::PeelableNow);
    FactorizeCell {
        proposed_module_id,
        owner_ids,
        binding_ids: binding_ids_set.into_iter().collect(),
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
        oversize: size_lines > size_cap_lines,
        emit_blocked_residual_bindings: verdict.emit_blocked_residual_bindings.clone(),
        cycle_blocker_owner_ids,
        active_modules_referenced: active_modules_referenced.into_iter().collect(),
        auto_grow_iterations,
    }
}

fn strongly_connected_components(
    residual: &BTreeSet<OwnerId>,
    edges: &[crate::graph::OwnerEdge],
) -> Vec<BTreeSet<OwnerId>> {
    let mut graph = DiGraphMap::<OwnerId, ()>::new();
    for &owner in residual {
        graph.add_node(owner);
    }
    for edge in edges {
        if !residual.contains(&edge.from) || !residual.contains(&edge.to) {
            continue;
        }
        if !edge.reason.constrains_init_order() {
            continue;
        }
        if edge.from == edge.to {
            continue;
        }
        graph.add_edge(edge.from, edge.to, ());
    }
    tarjan_scc(&graph)
        .into_iter()
        .map(|scc| scc.into_iter().collect())
        .collect()
}

fn apply_rebind_union(
    cells: &mut Vec<BTreeSet<OwnerId>>,
    residual: &BTreeSet<OwnerId>,
    edges: &[crate::graph::OwnerEdge],
) {
    // Build owner → cell index map.
    let mut cell_of: HashMap<OwnerId, usize> = HashMap::new();
    for (idx, cell) in cells.iter().enumerate() {
        for &o in cell {
            cell_of.insert(o, idx);
        }
    }
    let mut parent: Vec<usize> = (0..cells.len()).collect();
    fn find(parent: &mut [usize], i: usize) -> usize {
        if parent[i] != i {
            let r = find(parent, parent[i]);
            parent[i] = r;
        }
        parent[i]
    }
    for edge in edges {
        if !matches!(edge.reason.kind, DepKind::EagerRebind | DepKind::LazyRebind) {
            continue;
        }
        if !residual.contains(&edge.from) || !residual.contains(&edge.to) {
            continue;
        }
        let (Some(&ci), Some(&cj)) = (cell_of.get(&edge.from), cell_of.get(&edge.to)) else {
            continue;
        };
        let ri = find(&mut parent, ci);
        let rj = find(&mut parent, cj);
        if ri != rj {
            parent[rj] = ri;
        }
    }
    // Coalesce by root.
    let mut grouped: BTreeMap<usize, BTreeSet<OwnerId>> = BTreeMap::new();
    for (idx, cell) in cells.drain(..).enumerate() {
        let root = find(&mut parent, idx);
        grouped.entry(root).or_default().extend(cell);
    }
    cells.extend(grouped.into_values());
}

fn agglomerate(
    cells: &mut Vec<BTreeSet<OwnerId>>,
    residual: &BTreeSet<OwnerId>,
    edges: &[crate::graph::OwnerEdge],
    schedule: &Schedule,
    size_cap_lines: usize,
) {
    let line_count = |cell: &BTreeSet<OwnerId>| -> usize {
        cell.iter()
            .filter_map(|o| schedule.owner_graph.node(*o))
            .filter_map(|n| n.source_location.as_ref())
            .map(|l| l.end_line + 1 - l.start_line)
            .sum()
    };
    loop {
        let mut cell_of: HashMap<OwnerId, usize> = HashMap::new();
        for (idx, cell) in cells.iter().enumerate() {
            for &o in cell {
                cell_of.insert(o, idx);
            }
        }
        // Count inter-cell constraining edges per pair.
        let mut pair_edges: BTreeMap<(usize, usize), usize> = BTreeMap::new();
        for edge in edges {
            if !edge.reason.constrains_init_order() {
                continue;
            }
            if !residual.contains(&edge.from) || !residual.contains(&edge.to) {
                continue;
            }
            let (Some(&ci), Some(&cj)) = (cell_of.get(&edge.from), cell_of.get(&edge.to)) else {
                continue;
            };
            if ci == cj {
                continue;
            }
            let key = if ci < cj { (ci, cj) } else { (cj, ci) };
            *pair_edges.entry(key).or_insert(0) += 1;
        }
        // Find the pair with the most shared edges whose merged size
        // still fits the cap.
        let mut best: Option<((usize, usize), usize)> = None;
        for (&pair, &count) in &pair_edges {
            let merged_lines = line_count(&cells[pair.0]) + line_count(&cells[pair.1]);
            if merged_lines > size_cap_lines {
                continue;
            }
            match best {
                Some((_, best_count)) if best_count >= count => {}
                _ => best = Some((pair, count)),
            }
        }
        let Some(((i, j), _)) = best else {
            return;
        };
        // Remove the higher-index cell first so the lower index stays
        // valid; merge its members into the lower-index cell.
        let (lo, hi) = if i < j { (i, j) } else { (j, i) };
        let other = cells.swap_remove(hi);
        cells[lo].extend(other);
    }
}
