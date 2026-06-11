pub mod schema;

use std::collections::{BTreeMap, BTreeSet};

use rayon::prelude::*;
use swc_ecma_ast::Id;

use crate::graph::EdgeRole;
use crate::reports::schema::LineRange;
use crate::{
    AtomicGraphReport, AtomicUnitEdgeReport, AtomicUnitReport, BindingReport, ChunkFactorization,
    DepKind, EdgeRoleReport, LogicalModuleIndex, ModuleEntry, ModuleId, ModuleKey,
    OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport,
    OwnerId, QuotientEdgeReport, QuotientSccReport,
};

#[derive(Debug, Clone, Default)]
struct AtomicEdgeAccumulator {
    kinds: BTreeSet<DepKind>,
    owner_edge_ids: Vec<String>,
    constrains_init_order: bool,
}

pub(crate) fn build_owner_graph_report(factorization: &ChunkFactorization) -> OwnerGraphReport {
    // The three sub-reports below — `(quotient_nodes, quotient_edges)`,
    // `nodes` + `edges`, and `atomic_graph` — share no mutable state
    // and individually account for tens of seconds on chunks with
    // thousands of owners. Run them concurrently via `rayon::join`
    // so wall-clock collapses to roughly the slowest one. SCC
    // construction depends on `quotient_edges`, so it runs after the
    // join with the now-ready edge list.
    let (quotient_pair, (nodes_edges, atomic_graph)) = rayon::join(
        || {
            rayon::join(
                || build_quotient_node_reports(factorization),
                || build_quotient_edge_reports(factorization),
            )
        },
        || {
            rayon::join(
                || build_owner_nodes_and_edges(factorization),
                || build_atomic_graph_report(factorization),
            )
        },
    );
    let (quotient_nodes, quotient_edges) = quotient_pair;
    let (nodes, edges) = nodes_edges;
    let quotient_sccs = build_quotient_scc_reports(factorization, &quotient_edges);
    OwnerGraphReport {
        chunk_id: factorization.analysis.chunk_id.clone(),
        nodes,
        edges,
        quotient: OwnerGraphQuotientReport {
            nodes: quotient_nodes,
            edges: quotient_edges,
            sccs: quotient_sccs,
        },
        atomic_graph,
    }
}

/// Build the per-owner node report and the per-edge edge report
/// together. Both walks are independent, but bundling them lets the
/// rayon `join` split four ways without an explicit `scope` and keeps
/// the partition-of()/export-name-for()/etc.\ touches paired in cache.
fn build_owner_nodes_and_edges(
    factorization: &ChunkFactorization,
) -> (Vec<OwnerGraphNodeReport>, Vec<OwnerGraphEdgeReport>) {
    let partition = &factorization.partition;
    let owner_graph = &factorization.analysis.owner_graph;
    let nodes = owner_graph
        .iter_nodes()
        .map(|node| OwnerGraphNodeReport {
            id: owner_key(node.id),
            statement_ordinal: node.statement_ordinal,
            source_location: node.source_location.clone(),
            declared_bindings: binding_reports(factorization, node.declared.iter()),
            statement_kind: node.kind,
            purity: node.purity.clone(),
            destination: module_key(partition.of(node.id)),
        })
        .collect();
    let edges = owner_graph
        .iter_edges()
        .map(|edge| OwnerGraphEdgeReport {
            id: edge.id.report_key(),
            source: owner_key(edge.from),
            target: owner_key(edge.to),
            edge_kind: edge.reason.kind,
            binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
            statement_ordinal: edge.reason.statement_ordinal,
            constrains_init_order: edge.reason.constrains_init_order(),
            role: edge_role_report(edge.reason.role()),
        })
        .collect();
    (nodes, edges)
}

pub(crate) fn binding_reports<'a, I>(
    factorization: &ChunkFactorization,
    bindings: I,
) -> Vec<BindingReport>
where
    I: IntoIterator<Item = &'a Id>,
{
    bindings
        .into_iter()
        .map(|id| BindingReport {
            binding: id.0.clone(),
            export_name: factorization.analysis.export_name_for(id),
        })
        .collect()
}

fn build_quotient_node_reports(factorization: &ChunkFactorization) -> Vec<ModuleEntry> {
    let mut modules = BTreeSet::<ModuleId>::new();
    for idx in 0..factorization.analysis.logical_modules.len() {
        modules.insert(ModuleId(LogicalModuleIndex(idx)));
    }
    for (_, module) in factorization.partition.iter() {
        modules.insert(module);
    }
    for (from, to, _) in factorization.dep_graph.all_edges() {
        modules.insert(from);
        modules.insert(to);
    }
    modules
        .into_iter()
        .map(|id| module_entry(factorization, id))
        .collect()
}

pub(crate) fn build_quotient_edge_reports(
    factorization: &ChunkFactorization,
) -> Vec<QuotientEdgeReport> {
    // Use a `HashMap` for the per-(from,to) accumulator — quotient
    // edges count in the tens of thousands and `BTreeMap::entry` is
    // `O(log n)` per insert. The output ordering still has to match
    // the previous `BTreeMap` traversal (ascending `(from, to)`), so
    // we sort once at the end. `kinds` likewise: build into a
    // `BTreeSet` per pair (small — at most one entry per `DepKind`
    // variant) so the final `edge_kinds: Vec<DepKind>` stays sorted
    // without a per-pair sort.
    let partition = &factorization.partition;
    let owner_graph = &factorization.analysis.owner_graph;
    let mut accum: std::collections::HashMap<(ModuleId, ModuleId), QuotientEdgeAccumulator> =
        std::collections::HashMap::with_capacity(owner_graph.num_edges());
    let mut seen_side_effect_module_pairs: std::collections::HashSet<(ModuleId, ModuleId)> =
        std::collections::HashSet::new();
    for edge in owner_graph.iter_edges() {
        // Same partition view every other quotient consumer uses; see
        // `partition_endpoints` invariant doc.
        let Some((from, to)) =
            crate::graph::partition_endpoints(edge, partition, crate::graph::EndpointView::Lenient)
        else {
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

fn build_atomic_unit_report(
    factorization: &ChunkFactorization,
    idx: usize,
    unit: &crate::AtomicUnit,
) -> AtomicUnitReport {
    let mut owner_ids = Vec::with_capacity(unit.members.len());
    let mut members = Vec::new();
    let mut anonymous_statement_owner_ids = Vec::new();
    // Destinations dedup-by-key: tiny set per unit (rare to exceed
    // a handful), so a Vec + linear-scan dedup is cheaper than a
    // `BTreeMap` allocation per unit.
    let mut destinations: Vec<ModuleKey> = Vec::new();
    // `unit.causes` is a `BTreeSet<DepKind>` — iteration is already
    // `DepKind`-`Ord`-stable so no post-collection sort is needed.
    let causes: Vec<DepKind> = unit.causes.iter().copied().collect();
    let mut line_range = LineRange::new();
    let mut min_ordinal = usize::MAX;
    let mut max_ordinal = 0usize;
    for owner_id in &unit.members {
        owner_ids.push(owner_key(*owner_id));
        if let Some(node) = factorization.analysis.owner_graph.node(*owner_id) {
            if node.declared.is_empty() {
                anonymous_statement_owner_ids.push(owner_key(*owner_id));
            }
            members.extend(binding_reports(factorization, node.declared.iter()));
            if let Some(location) = &node.source_location {
                line_range.expand(location);
            }
            min_ordinal = min_ordinal.min(node.statement_ordinal.0);
            max_ordinal = max_ordinal.max(node.statement_ordinal.0);
            let destination = module_key(factorization.partition.of(*owner_id));
            if !destinations.contains(&destination) {
                destinations.push(destination);
            }
        }
    }
    members.sort();
    members.dedup();
    anonymous_statement_owner_ids.sort();
    destinations.sort();
    AtomicUnitReport {
        id: atomic_unit_key(idx),
        owner_ids,
        members,
        anonymous_statement_owner_ids,
        destinations,
        causes,
        size_lines_estimate: line_range.size_estimate(),
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
    }
}

fn build_atomic_graph_report(factorization: &ChunkFactorization) -> AtomicGraphReport {
    let owner_graph = &factorization.analysis.owner_graph;
    let mut units = factorization.atomic_units.clone();
    units.sort_by_key(|unit| unit.members.iter().copied().min().map(|owner| owner.0));
    // `unit_by_owner` is lookup-only after construction — a `HashMap`
    // is O(1) per get vs `BTreeMap`'s O(log n) and the per-edge loop
    // below hits it 2× per edge across ~26K edges on big chunks.
    let owner_count = owner_graph.num_nodes();
    let mut unit_by_owner: Vec<Option<usize>> = vec![None; owner_count];
    for (unit_idx, unit) in units.iter().enumerate() {
        for owner in &unit.members {
            if let Some(slot) = unit_by_owner.get_mut(owner.0) {
                *slot = Some(unit_idx);
            }
        }
    }

    // Per-unit reports are independent; parallelise via rayon so the
    // big-chunk case (thousands of units) saturates worker threads.
    let nodes = units
        .par_iter()
        .enumerate()
        .map(|(idx, unit)| build_atomic_unit_report(factorization, idx, unit))
        .collect();

    // Per-(from_unit, to_unit) accumulator: HashMap keyed by the
    // unit-pair (cheaper than BTreeMap for the ~tens-of-thousands
    // edges across thousands of units). Owner-edge IDs accumulate
    // into a Vec — dedup is unnecessary because each `OwnerEdgeId`
    // is unique per owner edge. Order is restored by a single final
    // sort.
    let mut accum: std::collections::HashMap<(usize, usize), AtomicEdgeAccumulator> =
        std::collections::HashMap::new();
    for edge in owner_graph.iter_edges() {
        if edge.reason.kind == DepKind::LazyUse {
            continue;
        }
        let (Some(from_unit), Some(to_unit)) = (
            unit_by_owner.get(edge.from.0).copied().flatten(),
            unit_by_owner.get(edge.to.0).copied().flatten(),
        ) else {
            continue;
        };
        if from_unit == to_unit {
            continue;
        }
        let entry = accum.entry((from_unit, to_unit)).or_default();
        entry.kinds.insert(edge.reason.kind);
        entry.owner_edge_ids.push(edge.id.report_key());
        entry.constrains_init_order |= edge.reason.constrains_init_order();
    }
    let mut accum: Vec<((usize, usize), AtomicEdgeAccumulator)> = accum.into_iter().collect();
    accum.sort_by_key(|((from, to), _)| (*from, *to));
    for (_, entry) in accum.iter_mut() {
        entry.owner_edge_ids.sort();
    }
    let edges = accum
        .into_iter()
        .enumerate()
        .map(|(idx, ((from, to), entry))| AtomicUnitEdgeReport {
            id: format!("atomic_edge:{idx}"),
            source: atomic_unit_key(from),
            target: atomic_unit_key(to),
            edge_kinds: entry.kinds.into_iter().collect(),
            owner_edge_ids: entry.owner_edge_ids.into_iter().collect(),
            constrains_init_order: entry.constrains_init_order,
        })
        .collect();

    AtomicGraphReport { nodes, edges }
}

fn build_quotient_scc_reports(
    factorization: &ChunkFactorization,
    quotient_edges: &[QuotientEdgeReport],
) -> Vec<QuotientSccReport> {
    let quotient_edges_by_source = quotient_edge_indices_by_source(quotient_edges);
    let mut sccs = Vec::new();
    for scc in factorization.dep_graph_sccs() {
        let is_cycle = scc.len() > 1
            || (scc.len() == 1 && factorization.dep_graph.contains_edge(scc[0], scc[0]));
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

/// True iff `id` refers to a logical module whose `residual` flag is
/// set — the chunk's catch-all destination synthesized before
/// `ChunkFactorization::build`. Used by the destination
/// projection in reports to gate residual-only predicates without
/// string-matching module ids or labels.
pub(crate) fn is_residual_destination(factorization: &ChunkFactorization, id: ModuleId) -> bool {
    let LogicalModuleIndex(idx) = id.0;
    factorization
        .analysis
        .logical_modules
        .get(idx)
        .is_some_and(|module| module.residual)
}

pub(crate) fn owner_key(id: OwnerId) -> String {
    format!("owner:{}", id.0)
}

/// Map a typed [`EdgeRole`] to its wire-format projection. `None`
/// is shorthand for `EdgeRole::Direct` (skipped from the JSON wire
/// shape via `skip_serializing_if = Option::is_none`); promoted
/// edges round-trip through [`EdgeRoleReport::PromotedAtInit`].
fn edge_role_report(role: EdgeRole) -> Option<EdgeRoleReport> {
    match role {
        EdgeRole::Direct => None,
        EdgeRole::PromotedAtInit { callee_owner } => Some(EdgeRoleReport::PromotedAtInit {
            callee_owner: owner_key(callee_owner),
        }),
    }
}

pub(crate) fn module_key(id: ModuleId) -> ModuleKey {
    let LogicalModuleIndex(idx) = id.0;
    ModuleKey(format!("logical:{idx}"))
}

pub(crate) fn atomic_unit_key(idx: usize) -> String {
    format!("atomic:{idx}")
}

pub(crate) fn module_id_from_key(key: &ModuleKey) -> Option<ModuleId> {
    key.as_str()
        .strip_prefix("logical:")
        .and_then(|idx| idx.parse::<usize>().ok())
        .map(|idx| ModuleId(LogicalModuleIndex(idx)))
}

/// Build the module-table entry for `id`: the single place its
/// canonical [`spec::ModulePath`] and residual flag are recorded. The
/// path comes from the module's `LogicalModule.id` (the production
/// `<chunk>::<path>` spelling), normalized through `ModulePath::parse`.
pub(crate) fn module_entry(factorization: &ChunkFactorization, id: ModuleId) -> ModuleEntry {
    let chunk_id = &factorization.analysis.chunk_id;
    let raw = factorization.analysis.module_name(id);
    let path = spec::ModulePath::parse(&raw, chunk_id).unwrap_or_else(|e| {
        panic!(
            "module {} has unparseable identity {raw:?}: {e}",
            module_key(id)
        )
    });
    ModuleEntry {
        key: module_key(id),
        path,
        residual: is_residual_destination(factorization, id),
    }
}
