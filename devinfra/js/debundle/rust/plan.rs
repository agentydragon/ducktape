use serde::Serialize;

use crate::owner_graph::OwnerGraph;

#[derive(Debug, Clone, Serialize)]
pub struct PlanSummaryV2 {
    pub selected_modules: Vec<String>,
    pub extraction_groups: Vec<Vec<String>>,
    pub rationale: String,
}

pub fn build_plan(owner_graph: &OwnerGraph) -> PlanSummaryV2 {
    let mut selected_modules = owner_graph
        .graph
        .node_indices()
        .map(|n| owner_graph.graph[n].clone())
        .collect::<Vec<_>>();
    selected_modules.sort();
    let extraction_groups = selected_modules
        .iter()
        .map(|m| vec![m.clone()])
        .collect::<Vec<_>>();
    PlanSummaryV2 {
        selected_modules,
        extraction_groups,
        rationale: "owner-graph connected components with side-effect order constraints"
            .to_string(),
    }
}
