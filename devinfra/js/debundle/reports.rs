use std::collections::{BTreeMap, BTreeSet};

use petgraph::algo::tarjan_scc;
use swc_ecma_ast::Id;

use crate::graph::OwnerEdge;
use crate::peelability::build_peelability_report;
use crate::{
    BindingReport, ChunkFactorization, DepKind, LogicalModuleIndex, ModuleId, ModuleReportRef,
    OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport,
    OwnerId, QuotientEdgeReport, QuotientSccReport,
};

#[derive(Debug, Clone, Default)]
struct QuotientEdgeAccumulator {
    kinds: BTreeSet<DepKind>,
    constrains_init_order: bool,
}

pub(crate) fn build_owner_graph_report(factorization: &ChunkFactorization) -> OwnerGraphReport {
    let owner_edges = &factorization.analysis.owner_graph.edges;
    let quotient_edges = build_quotient_edge_reports(factorization, owner_edges);
    let quotient_nodes = build_quotient_node_reports(factorization);
    let quotient_sccs = build_quotient_scc_reports(factorization, &quotient_edges);
    let peelability = build_peelability_report(factorization, owner_edges, &quotient_edges);
    let nodes = factorization
        .analysis
        .owner_graph
        .iter_nodes()
        .map(|node| OwnerGraphNodeReport {
            id: owner_key(node.id),
            statement_ordinal: node.statement_ordinal,
            source_location: node.source_location.clone(),
            declared_bindings: binding_reports(factorization, node.declared.iter()),
            statement_kind: node.kind,
            purity: node.purity.clone(),
            destination: module_report_ref(factorization, factorization.partition.of(node.id)),
        })
        .collect();
    let edges = owner_edges
        .iter()
        .map(|edge| OwnerGraphEdgeReport {
            id: edge.id.report_key(),
            source: owner_key(edge.from),
            target: owner_key(edge.to),
            edge_kind: edge.reason.kind,
            binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
            statement_ordinal: edge.reason.statement_ordinal,
            constrains_init_order: edge.reason.constrains_init_order(),
        })
        .collect();
    OwnerGraphReport {
        chunk_id: factorization.analysis.chunk_id.clone(),
        nodes,
        edges,
        quotient: OwnerGraphQuotientReport {
            nodes: quotient_nodes,
            edges: quotient_edges,
            sccs: quotient_sccs,
        },
        peelability,
        // Filled in by
        // `ChunkFactorization::owner_graph_report_with_factorize_options`
        // after this function returns. The default value is the
        // empty report (no cells); the factorization unconditionally
        // overwrites it with the real factorize verdict so JSON
        // consumers always see a populated `factorize` block.
        factorize: crate::FactorizeReport::default(),
    }
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
            export_name: factorization.analysis.export_name_for(&id.0),
        })
        .collect()
}

fn build_quotient_node_reports(factorization: &ChunkFactorization) -> Vec<ModuleReportRef> {
    let mut modules = BTreeSet::<ModuleId>::new();
    for idx in 0..factorization.analysis.logical_modules.len() {
        modules.insert(ModuleId(LogicalModuleIndex(idx)));
    }
    for (_, module) in factorization.partition.iter() {
        modules.insert(module);
    }
    for (from, to, _) in factorization.dep_graph.iter_edges() {
        modules.insert(from);
        modules.insert(to);
    }
    modules
        .into_iter()
        .map(|id| module_report_ref(factorization, id))
        .collect()
}

pub(crate) fn build_quotient_edge_reports(
    factorization: &ChunkFactorization,
    owner_edges: &[OwnerEdge],
) -> Vec<QuotientEdgeReport> {
    let partition = &factorization.partition;
    let mut accum = BTreeMap::<(ModuleId, ModuleId), QuotientEdgeAccumulator>::new();
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in owner_edges {
        let from = partition.of(edge.from);
        let to = partition.of(edge.to);
        if from == to {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        let entry = accum.entry((from, to)).or_default();
        entry.kinds.insert(edge.reason.kind);
        entry.constrains_init_order |= edge.reason.constrains_init_order();
    }
    accum
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

fn build_quotient_scc_reports(
    factorization: &ChunkFactorization,
    quotient_edges: &[QuotientEdgeReport],
) -> Vec<QuotientSccReport> {
    let quotient_edges_by_source = quotient_edge_indices_by_source(quotient_edges);
    let mut sccs = Vec::new();
    for scc in tarjan_scc(&factorization.dep_graph.graph) {
        let is_cycle = scc.len() > 1
            || (scc.len() == 1 && factorization.dep_graph.graph.contains_edge(scc[0], scc[0]));
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
        let mut modules: Vec<String> = in_scc.iter().copied().map(module_key).collect();
        modules.sort();
        let mut labels: Vec<String> = modules
            .iter()
            .map(|key| {
                module_id_from_key(key)
                    .map(|id| factorization.analysis.module_name(id))
                    .unwrap_or_else(|| key.clone())
            })
            .collect();
        labels.sort();
        module_edge_ids.sort();
        constraining_module_edge_ids.sort();
        sccs.push(QuotientSccReport {
            id: format!("scc:{}", sccs.len()),
            modules,
            labels,
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
/// `ChunkFactorization::build`. Used by peelability and the destination
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

pub(crate) fn module_key(id: ModuleId) -> String {
    let LogicalModuleIndex(idx) = id.0;
    format!("logical:{idx}")
}

pub(crate) fn module_id_from_key(key: &str) -> Option<ModuleId> {
    key.strip_prefix("logical:")
        .and_then(|idx| idx.parse::<usize>().ok())
        .map(|idx| ModuleId(LogicalModuleIndex(idx)))
}

pub(crate) fn module_report_ref(
    factorization: &ChunkFactorization,
    id: ModuleId,
) -> ModuleReportRef {
    let LogicalModuleIndex(idx) = id.0;
    let logical = factorization.analysis.logical_modules.get(idx);
    ModuleReportRef {
        id: module_key(id),
        label: factorization.analysis.module_name(id),
        residual: is_residual_destination(factorization, id),
        index: logical.map(|_| idx),
        target_file: logical.map(|module| module.target_file.clone()),
    }
}
