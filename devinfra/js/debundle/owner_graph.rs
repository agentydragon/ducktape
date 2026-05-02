use std::collections::HashMap;

use petgraph::graph::DiGraph;
use serde::Serialize;

use analysis_summary::AnalysisSummary;
use ast_ir::{ModuleIr, ProgramIr};
use module_path::resolve_dep;

#[derive(Debug, Clone, Copy, Serialize)]
pub enum EdgeKind {
    Import,
    SideEffectOrder,
}

#[derive(Debug, Clone)]
pub struct OwnerGraph {
    pub graph: DiGraph<String, EdgeKind>,
}

pub fn build_program_ir(analysis: &AnalysisSummary) -> ProgramIr {
    ProgramIr {
        modules: analysis
            .modules
            .iter()
            .map(|m| ModuleIr {
                id: m.source_path.clone(),
                import_specifiers: m.import_specifiers.clone(),
                export_count: m.export_count,
                has_top_level_effects: m.has_top_level_effects,
            })
            .collect(),
    }
}

pub fn build_owner_graph(ir: &ProgramIr) -> OwnerGraph {
    let mut graph = DiGraph::new();
    let mut node_by_id = HashMap::new();
    for module in &ir.modules {
        let idx = graph.add_node(module.id.clone());
        node_by_id.insert(module.id.clone(), idx);
    }

    for module in &ir.modules {
        let from = node_by_id[&module.id];
        for spec in &module.import_specifiers {
            if let Some(dep) = resolve_dep(&module.id, spec) {
                if let Some(to) = node_by_id.get(&dep).copied() {
                    graph.add_edge(from, to, EdgeKind::Import);
                }
            }
        }
    }

    let ordered: Vec<_> = ir
        .modules
        .iter()
        .filter(|m| m.has_top_level_effects)
        .map(|m| node_by_id[&m.id])
        .collect();
    for pair in ordered.windows(2) {
        graph.add_edge(pair[0], pair[1], EdgeKind::SideEffectOrder);
    }

    OwnerGraph { graph }
}
