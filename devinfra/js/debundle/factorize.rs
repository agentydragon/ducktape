//! Stage 4 of the factorize architecture: supernode-aware proposal
//! emitter. Reads the in-memory [`Schedule`] (whose partition reflects
//! every YAML claim) and emits cells that represent advisory
//! proposals to the spec author.
//!
//! # The factorize graph H
//!
//! H is a digraph over factorize nodes, where:
//!
//! * Every YAML-claimed [`ModuleId::Logical(idx)`] becomes a single
//!   **supernode** — its internal structure is hidden from H.
//!   Residual placeholder modules (those whose `LogicalModule.residual`
//!   flag is set) are not supernodes; their owners stay loose.
//! * Every owner whose partition destination is residual (either
//!   [`ModuleId::ResidualEntry`] or a residual placeholder logical
//!   module) is a **loose node**.
//!
//! Every owner-graph edge `u → v` projects onto a pair of nodes
//! `proj(u), proj(v)`. Edges whose projection is the same node (i.e.
//! both endpoints sit inside one supernode) disappear; everything else
//! contributes forced edges to H using the same closure rules the
//! earlier residual-only pass used:
//!
//! * `Sequenced` (anonymous source-order edge, no binding):
//!   adds `proj(v) → proj(u)` (promoting v alone would invert the
//!   original `u then v` source order; force the absorption).
//! * `EagerUse` / `LazyUse` with binding `b`:
//!   if `b ∈ entry_exported_binding_names` no edge — entry mediates
//!   the cross-cell read. Otherwise add `proj(u) → proj(v)`
//!   (consumer absorbs declarer to satisfy emit-resolvability).
//! * `EagerRebind` / `LazyRebind`:
//!   bidirectional in H — declarer and assigner of a mutable binding
//!   must co-locate.
//!
//! Tarjan-SCC on H produces **proposal cells**. Each cell's contents
//! decode to a proposal:
//!
//! * Cell contains a supernode `S_M` plus `N ≥ 1` loose nodes:
//!   "extend module M with those N loose owners". The cell's
//!   `proposed_module_id` is M's stable key (see [`module_key`]);
//!   `extension_owner_ids` lists the loose owners.
//! * Cell contains only loose nodes: today's "fresh module proposal"
//!   path.
//! * Cell contains a supernode and no loose nodes: stable module —
//!   no proposal is emitted.
//! * Cell with ≥2 supernodes: still emitted (status reflects whatever
//!   the predicate says about the loose subset, which may be empty);
//!   surfaces a structural conflict to the author. Today this would
//!   already be flagged by `factor_assembly::AtomicUnitConflict` for
//!   any cell that contains members of the same atomic unit; the
//!   proposal here is a higher-level cross-module conflict view.
//!
//! # Per-cell verdict
//!
//! Each cell's verdict comes from the SSOT
//! [`evaluate_peel_candidate`] predicate. The "moved owners" passed
//! to the predicate are the cell's **loose** owners — they're the
//! ones whose destination would change if the proposal landed. For a
//! supernode-only cell there are no loose owners and no proposal.
//! For mixed cells the destination context comes from the loose
//! owners (residual today), matching the materializer's mental model
//! of "move these residual owners into the supernode's module".
//!
//! See `FACTORIZE.md` for the broader architecture.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::peelability::{PeelCandidateEvaluation, PeelabilityContext, evaluate_peel_candidate};
use crate::reports::{build_quotient_edge_reports, is_residual_destination, module_key, owner_key};
use crate::{
    BindingName, DepKind, FactorizeCell, FactorizeOptions, FactorizeReport, ModuleId, OwnerId,
    PeelCandidateStatus, Schedule,
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
    let owners_by_supernode: BTreeMap<ModuleId, Vec<OwnerId>> = {
        let mut acc = BTreeMap::<ModuleId, Vec<OwnerId>>::new();
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

    let closure_graph = build_closure_graph(schedule, &node_by_owner, entry_exports);
    let sccs: Vec<Vec<FactorizeNode>> = tarjan_scc(&closure_graph);

    let mut emitted: Vec<FactorizeCell> = Vec::with_capacity(sccs.len());
    for (idx, scc) in sccs.into_iter().enumerate() {
        let mut supernodes: Vec<ModuleId> = Vec::new();
        let mut loose: Vec<OwnerId> = Vec::new();
        for node in &scc {
            match node {
                FactorizeNode::Supernode(m) => supernodes.push(*m),
                FactorizeNode::Loose(o) => loose.push(*o),
            }
        }
        supernodes.sort();
        loose.sort();
        if supernodes.len() == 1 && loose.is_empty() {
            // Stable module: no proposal. Skip.
            continue;
        }
        let extension_target = supernodes.first().copied();
        // For predicate evaluation pick the residual subset (loose
        // owners). When the cell is supernode-only with no loose
        // owners we already skipped above; for an extension proposal
        // (one supernode + ≥1 loose owners) the moved set is the
        // loose owners. For a fresh-module proposal (loose only) it's
        // again the loose set. For pathological multi-supernode
        // cells the loose subset may be empty — fall back to the
        // first supernode's owners so the predicate has something
        // to report on.
        let moved_owners: Vec<OwnerId> = if !loose.is_empty() {
            loose.clone()
        } else {
            owners_by_supernode
                .get(&supernodes[0])
                .cloned()
                .unwrap_or_default()
        };
        if moved_owners.is_empty() {
            continue;
        }
        let declared: Vec<BindingName> = moved_owners
            .iter()
            .flat_map(|o| bindings_by_owner.get(o).cloned().unwrap_or_default())
            .collect();
        let verdict = evaluate_peel_candidate(schedule, &context, &moved_owners, declared);
        // Cell owners enumerate the proposal members visible to
        // downstream tooling. For a fresh-module proposal that's the
        // loose owners. For an extension proposal we surface the
        // supernode's existing owners alongside the loose owners so
        // consumers can see the full post-extension owner set in
        // `owner_ids`; the `extension_owner_ids` field separately
        // pinpoints what would be NEW.
        let mut cell_owners: BTreeSet<OwnerId> = loose.iter().copied().collect();
        if let Some(module) = extension_target {
            if let Some(existing) = owners_by_supernode.get(&module) {
                cell_owners.extend(existing.iter().copied());
            }
        }
        let proposal_id = match extension_target {
            Some(module) => format!("extend:{}", module_key(module)),
            None => format!("auto_partition_{idx:04}"),
        };
        // Only extension proposals carry the loose subset as
        // `extension_owner_ids`. Fresh-module proposals have no
        // existing module to extend, so the field stays empty —
        // every owner in the cell is part of the proposal itself
        // (already in `owner_ids`).
        let extension_owners: BTreeSet<OwnerId> = if extension_target.is_some() {
            loose.iter().copied().collect()
        } else {
            BTreeSet::new()
        };
        emitted.push(make_cell(
            proposal_id,
            cell_owners,
            extension_owners,
            extension_target,
            &node_by_owner,
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
    }
}

/// Build the must-co-locate digraph H over factorize nodes. An edge
/// `a → b` means "if a is in a cell, b must be in that same cell."
/// Edges whose endpoints project to the same node (e.g. both inside
/// one supernode) are dropped.
fn build_closure_graph(
    schedule: &Schedule,
    node_by_owner: &[FactorizeNode],
    entry_exports: &HashSet<BindingName>,
) -> DiGraphMap<FactorizeNode, ()> {
    let mut h = DiGraphMap::<FactorizeNode, ()>::new();
    // Seed every projected node so cells without any incident edges
    // still show up as singleton SCCs.
    for node in node_by_owner {
        h.add_node(*node);
    }
    for edge in &schedule.owner_graph.edges {
        if edge.from == edge.to {
            continue;
        }
        let from = node_by_owner[edge.from.0];
        let to = node_by_owner[edge.to.0];
        if from == to {
            // Internal supernode edge: hidden by collapse.
            continue;
        }
        match edge.reason.kind {
            DepKind::Sequenced => {
                h.add_edge(to, from, ());
            }
            DepKind::EagerUse | DepKind::LazyUse => {
                if let Some(bid) = edge.reason.binding {
                    let bname = schedule.binding_name(bid);
                    if !entry_exports.contains(bname) {
                        h.add_edge(from, to, ());
                    }
                }
            }
            DepKind::EagerRebind | DepKind::LazyRebind => {
                h.add_edge(from, to, ());
                h.add_edge(to, from, ());
            }
        }
    }
    h
}

#[allow(clippy::too_many_arguments)]
fn make_cell(
    proposed_module_id: String,
    owners: BTreeSet<OwnerId>,
    extension_owners: BTreeSet<OwnerId>,
    extension_target: Option<ModuleId>,
    node_by_owner: &[FactorizeNode],
    verdict: &PeelCandidateEvaluation,
    bindings_by_owner: &HashMap<OwnerId, Vec<BindingName>>,
    schedule: &Schedule,
    size_cap_lines: usize,
) -> FactorizeCell {
    let mut owner_ids: Vec<String> = owners.iter().copied().map(owner_key).collect();
    owner_ids.sort();
    let mut extension_owner_ids: Vec<String> =
        extension_owners.iter().copied().map(owner_key).collect();
    extension_owner_ids.sort();
    let extends_module_id: Option<String> = extension_target.map(module_key);

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

    // `active_modules_referenced` walks outgoing constraining edges
    // from this cell in the collapsed graph: any neighbor supernode
    // whose edge survives projection counts. Inter-cell edges that
    // land on a different loose node end up on a different cell; we
    // don't surface them here (the proposal layer handles cell-to-
    // cell relationships in `peel_factorize.rs`).
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
        extends_module_id,
        extension_owner_ids,
    }
}
