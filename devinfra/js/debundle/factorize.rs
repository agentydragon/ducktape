//! Closure-based factorize: one Tarjan SCC over the must-co-locate
//! digraph, instead of a per-cell predicate-driven absorption loop.
//!
//! Reads the in-memory [`Schedule`] and emits cells that — by
//! construction — satisfy emit-resolvability, LazyRebind, and
//! source-order (Sequenced) gates. The cycle gate is reported per
//! cell via the SSOT [`evaluate_residual_peel_candidate`] predicate
//! used as a verifier, run exactly once per cell.
//!
//! # Why one pass instead of grow-to-fixed-point
//!
//! An earlier version ran the predicate inside a per-cell absorption
//! loop. On the gaffer chunk that ballooned to thousands of cells
//! each running ~N rounds of the full predicate, with overall cost
//! around O(N³). The information the loop was trying to extract is
//! the same information the closure rules below capture statically:
//! "u in S forces v in S because emit-blocked / rebind / source-
//! order". Encoding those rules once as forced edges in a digraph
//! and reading off SCCs gives the same answer in O(V + E).
//!
//! # Closure rules
//!
//! Each residual constraining or use edge `e: u → v` (both endpoints
//! residual) contributes forced edges to a digraph H over residual
//! owners. An edge `a → b` in H means "if a ∈ cell, b must be in cell".
//!
//! * `Sequenced` (anonymous source-order edge, no binding):
//!   Emits `v → u`. (Promoting v alone forces residual_entry → cell(v)
//!   in linker order, which inverts the original `u then v` source
//!   order — must absorb u.)
//! * `EagerUse` / `LazyUse` with binding `b`:
//!   If `b ∈ entry_exported_binding_names` (entry already re-exports
//!   b), no H edge — the cross-cell read resolves through entry.
//!   Otherwise emits `u → v` (consumer absorbs declarer to satisfy
//!   emit-resolvability).
//! * `EagerRebind` / `LazyRebind`:
//!   Emits both `u → v` and `v → u`. LazyRebind gate is unconditional:
//!   declarer and assigner of a mutable binding must materialize in
//!   the same destination.
//!
//! Tarjan SCC on H gives each cell. Cycles in the must-co-move
//! relation collapse into single SCCs; otherwise the inter-SCC DAG
//! captures dependencies between cells (forward use edges on
//! pre-existing entry exports remain as cell-to-cell references).
//!
//! # Per-cell verdict
//!
//! After SCC, we run [`evaluate_residual_peel_candidate`] once per
//! cell as a sanity check. The construction guarantees emit-
//! resolvability and LazyRebind pass; the cycle gate may still flag
//! cells whose modules would form `cell ↔ residual_entry` cycles
//! through the cell-level DAG. Those cells report
//! `BlockedResidualDependency` honestly — they're valid proposals
//! that aren't `landable_today` on their own.

use std::collections::{BTreeSet, HashMap, HashSet};

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

    let residual: BTreeSet<OwnerId> = schedule
        .owner_graph
        .iter_nodes()
        .filter(|node| matches!(schedule.partition.of(node.id), ModuleId::ResidualEntry))
        .map(|node| node.id)
        .collect();
    let residual_owner_count = residual.len();

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

    let empty_exports = HashSet::<BindingName>::new();
    let entry_exports = schedule
        .entry_exported_binding_names()
        .unwrap_or(&empty_exports);

    let closure_graph = build_closure_graph(schedule, &residual, entry_exports);
    let sccs: Vec<Vec<OwnerId>> = tarjan_scc(&closure_graph);

    let mut emitted: Vec<FactorizeCell> = Vec::with_capacity(sccs.len());
    for (idx, scc) in sccs.into_iter().enumerate() {
        let owners: BTreeSet<OwnerId> = scc.into_iter().collect();
        let owners_vec: Vec<OwnerId> = owners.iter().copied().collect();
        let declared: Vec<BindingName> = owners_vec
            .iter()
            .flat_map(|o| bindings_by_owner.get(o).cloned().unwrap_or_default())
            .collect();
        let verdict = evaluate_residual_peel_candidate(schedule, &context, &owners_vec, declared);
        let id = format!("auto_partition_{idx:04}");
        emitted.push(make_cell(
            id,
            owners,
            &verdict,
            &bindings_by_owner,
            schedule,
            options.size_cap_lines,
        ));
    }

    emitted.sort_by(|a, b| {
        b.landable_today.cmp(&a.landable_today).then_with(|| {
            let al = a.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            let bl = b.source_line_range.map(|r| r[0]).unwrap_or(usize::MAX);
            al.cmp(&bl)
        })
    });
    for (idx, cell) in emitted.iter_mut().enumerate() {
        cell.proposed_module_id = format!("auto_partition_{idx:04}");
    }

    FactorizeReport {
        size_cap_lines: options.size_cap_lines,
        residual_owner_count,
        cells: emitted,
    }
}

/// Build the must-co-locate digraph H. An edge `a → b` means "if a
/// is in a residual cell, then b must be in that same cell."
fn build_closure_graph(
    schedule: &Schedule,
    residual: &BTreeSet<OwnerId>,
    entry_exports: &HashSet<BindingName>,
) -> DiGraphMap<OwnerId, ()> {
    let mut h = DiGraphMap::<OwnerId, ()>::new();
    for &owner in residual {
        h.add_node(owner);
    }
    for edge in &schedule.owner_graph.edges {
        if !residual.contains(&edge.from) || !residual.contains(&edge.to) {
            continue;
        }
        if edge.from == edge.to {
            continue;
        }
        match edge.reason.kind {
            DepKind::Sequenced => {
                h.add_edge(edge.to, edge.from, ());
            }
            DepKind::EagerUse | DepKind::LazyUse => {
                if let Some(bid) = edge.reason.binding {
                    let bname = schedule.binding_name(bid);
                    if !entry_exports.contains(bname) {
                        h.add_edge(edge.from, edge.to, ());
                    }
                }
            }
            DepKind::EagerRebind | DepKind::LazyRebind => {
                h.add_edge(edge.from, edge.to, ());
                h.add_edge(edge.to, edge.from, ());
            }
        }
    }
    h
}

fn make_cell(
    proposed_module_id: String,
    owners: BTreeSet<OwnerId>,
    verdict: &PeelCandidateEvaluation,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    schedule: &Schedule,
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
    }
}
