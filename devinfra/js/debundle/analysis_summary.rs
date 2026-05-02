//! Analysis-snapshot data shared between the analyzer (`program_analysis`),
//! consumers (`owner_graph`, `plan`), and the orchestrator (`pipeline`).
//! Lives in its own module so it can sit upstream of all four in the
//! Bazel/Cargo dependency graph.

use std::collections::HashMap;

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct AnalysisSummary {
    pub modules: Vec<ModuleAnalysis>,
    pub owners: Vec<OwnerAnalysis>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleAnalysis {
    pub member_names: Vec<String>,
    pub source_path: String,
    pub import_specifiers: Vec<String>,
    pub resolved_deps: Vec<String>,
    pub export_count: usize,
    pub has_top_level_effects: bool,
    pub owner_ids: Vec<String>,
    pub owner_semantic_id_by_member_name: HashMap<String, String>,
    pub program_item_ids: Vec<String>,
    pub side_effect_ids: Vec<String>,
    pub replayable_side_effect_ids: Vec<String>,
    pub runtime_sensitive_effects: bool,
    pub side_effect_touched_owner_ids: Vec<String>,
    pub side_effect_records: Vec<SideEffectAnalysis>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SideEffectAnalysis {
    pub id: String,
    pub replayable: bool,
    pub runtime_sensitive: bool,
    pub touched_names: Vec<String>,
    pub touched_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerAnalysis {
    pub id: String,
    pub module_id: String,
    pub member_name: String,
    pub line: usize,
    pub dep_edges: Vec<OwnerDependencyEdge>,
    pub accesses: Vec<OwnerAccessRecord>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerAccessRecord {
    pub name: String,
    pub access_kind: String,
    pub phase: String,
    pub owner_id: Option<String>,
    pub kind: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerDependencyEdge {
    pub to_owner_id: String,
    pub phase: String,
    pub access_kind: String,
}
