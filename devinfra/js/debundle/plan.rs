use std::collections::{BTreeSet, HashMap, HashSet};

use petgraph::algo::tarjan_scc;
use petgraph::graph::DiGraph;
use serde::Serialize;

use crate::owner_graph::OwnerGraph;
use crate::pipeline::{
    AnalysisSummary, ModuleAnalysis, OwnerAccessRecord, OwnerAnalysis, OwnerDependencyEdge,
    SideEffectAnalysis,
};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlannerDebugSnapshot {
    pub candidates: Vec<PlannerCandidateDebug>,
    pub selected: Vec<PlannerCandidateDebug>,
    pub ordered_init_state: PlannerOrderedInitStateDebug,
    pub frontier_traces: Vec<PlannerFrontierTraceDebug>,
    pub owner_analyses: Vec<PlannerOwnerAnalysisDebug>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannerOwnerAnalysisDebug {
    pub id: String,
    pub module_id: String,
    pub member_name: String,
    pub dep_owner_ids: Vec<String>,
    pub eager_dep_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlannerFrontierTraceDebug {
    pub seed_component_id: String,
    pub seed_component_owner_ids: Vec<String>,
    pub seed_component_member_names: Vec<String>,
    pub seed_component_dep_owner_ids: Vec<String>,
    pub seed_component_direct_dependency_component_ids: Vec<String>,
    pub required_component_ids: Vec<String>,
    pub required_closure_owner_ids: Vec<String>,
    pub contiguous_envelope_component_ids: Vec<String>,
    pub closure_owner_ids: Vec<String>,
    pub envelope_start_ordinal: usize,
    pub envelope_end_ordinal: usize,
    pub envelope_barrier_item_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlannerOrderedInitStateDebug {
    pub replayable_side_effect_ids_by_owner_id: HashMap<String, Vec<String>>,
    pub replayable_side_effect_state_by_id: HashMap<String, ReplayableSideEffectStateDebug>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReplayableSideEffectStateDebug {
    pub id: String,
    pub runtime_sensitive: bool,
    pub touched_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlannerCandidateDebug {
    pub id: String,
    pub owner_ids: Vec<String>,
    pub member_names: Vec<String>,
    pub estimated_size: usize,
    pub blocking_reasons: Vec<String>,
    pub attached_item_ids: Vec<String>,
    pub semantic_owner_ids: Vec<String>,
    pub semantic_member_names: Vec<String>,
    pub start_ordinal: usize,
    pub shell_item_ids: Vec<String>,
    pub semantic_blocking_reasons: Vec<String>,
    pub stage_runs: Vec<PlannerStageRunDebug>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlannerStageRunDebug {
    pub id: String,
    pub start_ordinal: usize,
    pub end_ordinal: usize,
    pub item_ids: Vec<String>,
    pub owner_ids: Vec<String>,
    pub member_names: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlanSummaryV2 {
    pub selected_modules: Vec<String>,
    pub extraction_groups: Vec<Vec<String>>,
    pub rationale: String,
    pub debug: PlannerDebugSnapshot,
}

#[derive(Debug, Clone)]
struct ClosureCandidate {
    id: String,
    owner_ids: Vec<String>,
    member_names: Vec<String>,
    estimated_size: usize,
    blocking_reasons: Vec<String>,
    attached_item_ids: Vec<String>,
    start_ordinal: usize,
    shell_item_ids: Vec<String>,
    semantic_blocking_reasons: Vec<String>,
    stage_runs: Vec<PlannerStageRunDebug>,
}

#[derive(Debug, Clone)]
struct OwnerRecord {
    id: String,
    module_id: String,
    member_name: String,
    ordinal: usize,
    dep_owner_ids: Vec<String>,
    eager_dep_owner_ids: Vec<String>,
    write_like_dep_owner_ids: Vec<String>,
}

pub fn build_plan(owner_graph: &OwnerGraph, analysis: &AnalysisSummary) -> PlanSummaryV2 {
    let selected_modules = sorted_modules(owner_graph);
    let mut candidates = Vec::new();
    let mut selected_candidates = Vec::new();
    let mut extraction_groups = Vec::new();
    let mut frontier_traces = Vec::new();

    for module in &analysis.modules {
        let module_analysis = module_scoped_analysis(analysis, module);
        if module_analysis.owners.is_empty() {
            continue;
        }
        let (module_candidates, module_frontier_traces) =
            build_closure_candidates(owner_graph, &module_analysis);
        let (module_extraction_groups, module_selected_candidates) =
            pack_candidates(module_candidates.clone());
        candidates.extend(module_candidates);
        selected_candidates.extend(module_selected_candidates);
        extraction_groups.extend(module_extraction_groups);
        frontier_traces.extend(module_frontier_traces);
    }

    PlanSummaryV2 {
        selected_modules,
        extraction_groups,
        rationale: "selected-owner closure planning over dependency components with side-effect order constraints"
            .to_string(),
        debug: PlannerDebugSnapshot {
            candidates: candidates
                .iter()
                .map(PlannerCandidateDebug::from_candidate)
                .collect(),
            selected: selected_candidates
                .iter()
                .map(PlannerCandidateDebug::from_candidate)
                .collect(),
            ordered_init_state: planner_state_debug(analysis),
            frontier_traces,
            owner_analyses: owner_analysis_debug(analysis),
        },
    }
}

fn owner_analysis_debug(analysis: &AnalysisSummary) -> Vec<PlannerOwnerAnalysisDebug> {
    analysis
        .owners
        .iter()
        .map(|owner| {
            let mut dep_owner_ids = owner
                .dep_edges
                .iter()
                .map(|edge| edge.to_owner_id.clone())
                .collect::<Vec<_>>();
            dep_owner_ids.sort();
            dep_owner_ids.dedup();
            let mut eager_dep_owner_ids = owner
                .dep_edges
                .iter()
                .filter(|edge| {
                    edge.phase == "eager"
                        && matches!(edge.access_kind.as_str(), "read" | "write" | "member_write")
                })
                .map(|edge| edge.to_owner_id.clone())
                .collect::<Vec<_>>();
            eager_dep_owner_ids.sort();
            eager_dep_owner_ids.dedup();
            PlannerOwnerAnalysisDebug {
                id: owner.id.clone(),
                module_id: owner.module_id.clone(),
                member_name: owner.member_name.clone(),
                dep_owner_ids,
                eager_dep_owner_ids,
            }
        })
        .collect()
}

fn module_scoped_analysis(analysis: &AnalysisSummary, module: &ModuleAnalysis) -> AnalysisSummary {
    let canonical_to_local = module
        .member_names
        .iter()
        .filter_map(|member_name| {
            let local = module
                .owner_semantic_id_by_member_name
                .get(member_name)
                .cloned()?;
            Some((format!("{}::{}", module.source_path, member_name), local))
        })
        .collect::<HashMap<_, _>>();
    let owners = analysis
        .owners
        .iter()
        .filter(|owner| owner.module_id == module.source_path)
        .filter_map(|owner| {
            let local_id = canonical_to_local.get(&owner.id).cloned()?;
            Some(OwnerAnalysis {
                id: local_id,
                module_id: owner.module_id.clone(),
                member_name: owner.member_name.clone(),
                line: owner.line,
                dep_edges: owner
                    .dep_edges
                    .iter()
                    .filter_map(|edge| {
                        Some(OwnerDependencyEdge {
                            to_owner_id: canonical_to_local.get(&edge.to_owner_id)?.clone(),
                            phase: edge.phase.clone(),
                            access_kind: edge.access_kind.clone(),
                        })
                    })
                    .collect(),
                accesses: owner
                    .accesses
                    .iter()
                    .map(|access| OwnerAccessRecord {
                        name: access.name.clone(),
                        access_kind: access.access_kind.clone(),
                        phase: access.phase.clone(),
                        owner_id: access
                            .owner_id
                            .as_ref()
                            .and_then(|owner_id| canonical_to_local.get(owner_id).cloned()),
                        kind: access.kind.clone(),
                    })
                    .collect(),
            })
        })
        .collect::<Vec<_>>();
    let mut local_module = module.clone();
    local_module.side_effect_records = module
        .side_effect_records
        .iter()
        .map(|record| SideEffectAnalysis {
            id: record.id.clone(),
            replayable: record.replayable,
            runtime_sensitive: record.runtime_sensitive,
            touched_names: record.touched_names.clone(),
            touched_owner_ids: record
                .touched_owner_ids
                .iter()
                .filter_map(|owner_id| canonical_to_local.get(owner_id).cloned())
                .collect(),
        })
        .collect();
    local_module.side_effect_touched_owner_ids = local_module
        .side_effect_records
        .iter()
        .flat_map(|record| record.touched_owner_ids.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    AnalysisSummary {
        modules: vec![local_module],
        owners,
    }
}

fn planner_state_debug(analysis: &AnalysisSummary) -> PlannerOrderedInitStateDebug {
    let state = build_merged_ordered_init_planner_state(analysis);
    PlannerOrderedInitStateDebug {
        replayable_side_effect_ids_by_owner_id: state.replayable_side_effect_ids_by_owner_id,
        replayable_side_effect_state_by_id: state
            .replayable_side_effect_state_by_id
            .into_iter()
            .map(|(id, record)| {
                (
                    id,
                    ReplayableSideEffectStateDebug {
                        id: record.id,
                        runtime_sensitive: record.runtime_sensitive,
                        touched_owner_ids: record.touched_owner_ids,
                    },
                )
            })
            .collect(),
    }
}

fn build_merged_ordered_init_planner_state(analysis: &AnalysisSummary) -> OrderedInitPlannerState {
    let mut merged = OrderedInitPlannerState {
        replayable_side_effect_ids_by_owner_id: HashMap::new(),
        replayable_side_effect_state_by_id: HashMap::new(),
    };
    for module in &analysis.modules {
        let module_analysis = module_scoped_analysis(analysis, module);
        let state = build_ordered_init_planner_state(&module_analysis);
        for (owner_id, side_effect_ids) in state.replayable_side_effect_ids_by_owner_id {
            let merged_ids = merged
                .replayable_side_effect_ids_by_owner_id
                .entry(owner_id)
                .or_default();
            merged_ids.extend(side_effect_ids);
            merged_ids.sort();
            merged_ids.dedup();
        }
        for (id, record) in state.replayable_side_effect_state_by_id {
            merged.replayable_side_effect_state_by_id.insert(id, record);
        }
    }
    merged
}

fn sorted_modules(owner_graph: &OwnerGraph) -> Vec<String> {
    let mut selected_modules = owner_graph
        .graph
        .node_indices()
        .map(|n| owner_graph.graph[n].clone())
        .collect::<Vec<_>>();
    selected_modules.sort();
    selected_modules
}

fn build_closure_candidates(
    _owner_graph: &OwnerGraph,
    analysis: &AnalysisSummary,
) -> (Vec<ClosureCandidate>, Vec<PlannerFrontierTraceDebug>) {
    let planner_state = build_ordered_init_planner_state(analysis);
    let owner_ordinal_by_id = build_owner_ordinals(analysis);
    let owner_records = build_owner_records(analysis, &owner_ordinal_by_id);
    let owner_by_id = owner_records
        .iter()
        .map(|record| (record.id.as_str(), record))
        .collect::<HashMap<_, _>>();
    let owner_adjacency = build_staged_shell_owner_adjacency(&owner_records);

    let components = build_owner_components(&owner_records);
    let mut component_by_id = HashMap::new();
    for component in &components {
        component_by_id.insert(component.id.as_str(), component);
    }
    let mut closure_ids_cache = HashMap::<String, Vec<String>>::new();
    let mut candidates = Vec::new();
    let mut frontier_traces = Vec::new();
    let mut seen_closure_signatures = BTreeSet::new();
    for seed_component in &components {
        let required_component_ids = collect_component_closure_ids(
            seed_component.id.as_str(),
            &component_by_id,
            &mut closure_ids_cache,
        );
        let required_component_debug_ids =
            component_debug_ids(&required_component_ids, &component_by_id);
        let semantic_closure_owner_ids =
            owners_for_component_ids_set(&required_component_ids, &component_by_id)
                .into_iter()
                .map(|(owner_id, _)| owner_id)
                .collect::<Vec<_>>();
        let closure_owner_ids = expand_staged_attached_owner_ids(
            &semantic_closure_owner_ids,
            &owner_adjacency,
            &owner_by_id,
        );
        frontier_traces.push(PlannerFrontierTraceDebug {
            seed_component_id: seed_component.debug_id.clone(),
            seed_component_owner_ids: seed_component.owner_ids.clone(),
            seed_component_member_names: seed_component
                .owner_ids
                .iter()
                .filter_map(|owner_id| {
                    owner_by_id
                        .get(owner_id.as_str())
                        .map(|owner| owner.member_name.clone())
                })
                .collect(),
            seed_component_dep_owner_ids: seed_component
                .owner_ids
                .iter()
                .flat_map(|owner_id| {
                    owner_by_id
                        .get(owner_id.as_str())
                        .map(|owner| {
                            let mut deps = owner.dep_owner_ids.clone();
                            deps.extend(owner.eager_dep_owner_ids.clone());
                            deps
                        })
                        .unwrap_or_default()
                })
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            seed_component_direct_dependency_component_ids: seed_component
                .direct_dependency_component_ids
                .iter()
                .filter_map(|component_id| {
                    component_by_id
                        .get(component_id.as_str())
                        .map(|component| component.debug_id.clone())
                })
                .collect(),
            required_component_ids: required_component_debug_ids,
            // JS candidate batch plans do not expose the pre-expansion owner
            // closure by default; the parity harness therefore observes the
            // staged-shell owner set through this field.
            required_closure_owner_ids: closure_owner_ids.clone(),
            // JS only populates contiguous-envelope debug when report details
            // are requested. The default candidate batch debug surface leaves
            // these fields absent; emit the normalized empty/default shape.
            contiguous_envelope_component_ids: Vec::new(),
            closure_owner_ids: closure_owner_ids.clone(),
            envelope_start_ordinal: 0,
            envelope_end_ordinal: 0,
            envelope_barrier_item_ids: Vec::new(),
        });

        let mut member_names = BTreeSet::new();
        for owner_id in &closure_owner_ids {
            if let Some(owner) = owner_by_id.get(owner_id.as_str()) {
                member_names.insert(owner.member_name.clone());
            }
        }
        if member_names.is_empty() {
            continue;
        }
        let owner_ids = closure_owner_ids;
        let closure_signature = semantic_closure_owner_ids.join("\n");
        if !seen_closure_signatures.insert(closure_signature) {
            continue;
        }
        let semantic_blocking_reasons = derive_blocking_reasons(&owner_ids, &owner_by_id);
        let mut attached_item_ids = BTreeSet::new();
        let mut covered_item_ordinals = BTreeSet::new();
        let mut start_ordinal = usize::MAX;
        for owner_id in &owner_ids {
            if let Some(owner) = owner_by_id.get(owner_id.as_str()) {
                start_ordinal = start_ordinal.min(owner.ordinal);
                covered_item_ordinals.insert(owner.ordinal);
                for side_effect_id in planner_state
                    .replayable_side_effect_ids_by_owner_id
                    .get(owner.id.as_str())
                    .cloned()
                    .unwrap_or_default()
                {
                    attached_item_ids.insert(side_effect_id);
                }
            }
        }
        attached_item_ids.retain(|side_effect_id| {
            planner_state
                .replayable_side_effect_state_by_id
                .get(side_effect_id.as_str())
                .map(|state| {
                    !state.runtime_sensitive
                        && state
                            .touched_owner_ids
                            .iter()
                            .all(|owner_id| owner_ids.contains(owner_id))
                })
                .unwrap_or(false)
        });
        let shell_item_ids = build_shell_item_ids(&covered_item_ordinals, &owner_ids, analysis);
        let shell_blocking_reasons =
            build_shell_blocking_reasons(&shell_item_ids, &owner_ids, analysis);
        let blocking_reasons = {
            let mut out = BTreeSet::new();
            for reason in &semantic_blocking_reasons {
                out.insert(reason.clone());
            }
            for reason in &shell_blocking_reasons {
                out.insert(reason.clone());
            }
            out.into_iter().collect::<Vec<_>>()
        };
        let attached_item_ids_vec = attached_item_ids.iter().cloned().collect::<Vec<_>>();
        let estimated_size = structural_estimated_size(
            &covered_item_ordinals,
            &attached_item_ids_vec,
            owner_ids.len(),
            analysis,
        );
        let candidate_id = format!("selected_module_group_{:04}", candidates.len());
        candidates.push(ClosureCandidate {
            id: candidate_id.clone(),
            owner_ids: owner_ids.clone(),
            estimated_size,
            blocking_reasons: blocking_reasons.clone(),
            member_names: member_names.into_iter().collect(),
            attached_item_ids: attached_item_ids_vec.clone(),
            start_ordinal: if start_ordinal == usize::MAX {
                seed_component
                    .owner_ordinals
                    .iter()
                    .copied()
                    .min()
                    .unwrap_or(usize::MAX)
            } else {
                start_ordinal
            },
            shell_item_ids,
            semantic_blocking_reasons,
            stage_runs: build_stage_runs(
                &candidate_id,
                &owner_ids,
                &attached_item_ids_vec,
                analysis,
                &owner_by_id,
            ),
        });
    }
    candidates.sort_by(|left, right| {
        left.blocking_reasons
            .len()
            .cmp(&right.blocking_reasons.len())
            .then_with(|| right.estimated_size.cmp(&left.estimated_size))
            .then_with(|| right.owner_ids.len().cmp(&left.owner_ids.len()))
            .then_with(|| left.owner_ids.join("\n").cmp(&right.owner_ids.join("\n")))
    });
    (candidates, frontier_traces)
}

fn owners_for_component_ids_set(
    component_ids: &[String],
    component_by_id: &HashMap<&str, &OwnerComponent>,
) -> Vec<(String, usize)> {
    let component_ids = component_ids.iter().cloned().collect::<BTreeSet<_>>();
    owners_for_component_ids(&component_ids, component_by_id)
}

fn owners_for_component_ids(
    component_ids: &BTreeSet<String>,
    component_by_id: &HashMap<&str, &OwnerComponent>,
) -> Vec<(String, usize)> {
    let mut owners = component_ids
        .iter()
        .filter_map(|component_id| component_by_id.get(component_id.as_str()).copied())
        .flat_map(|component| {
            component
                .owner_ids
                .iter()
                .zip(component.owner_ordinals.iter())
                .map(|(owner_id, ordinal)| (owner_id.clone(), *ordinal))
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    owners.sort_by_key(|(_, ordinal)| *ordinal);
    owners
}

fn component_debug_ids(
    component_ids: &[String],
    component_by_id: &HashMap<&str, &OwnerComponent>,
) -> Vec<String> {
    component_ids
        .iter()
        .filter_map(|component_id| {
            component_by_id
                .get(component_id.as_str())
                .map(|component| component.debug_id.clone())
        })
        .collect()
}

fn build_staged_shell_owner_adjacency(
    owner_records: &[OwnerRecord],
) -> HashMap<String, BTreeSet<String>> {
    let owner_id_set = owner_records
        .iter()
        .map(|owner| owner.id.as_str())
        .collect::<HashSet<_>>();
    let mut adjacency = owner_records
        .iter()
        .map(|owner| (owner.id.clone(), BTreeSet::new()))
        .collect::<HashMap<_, _>>();
    for owner in owner_records {
        for dep_owner_id in &owner.dep_owner_ids {
            if owner_id_set.contains(dep_owner_id.as_str()) {
                adjacency
                    .entry(owner.id.clone())
                    .or_default()
                    .insert(dep_owner_id.clone());
            }
        }
        for dep_owner_id in &owner.write_like_dep_owner_ids {
            if owner_id_set.contains(dep_owner_id.as_str()) {
                adjacency
                    .entry(dep_owner_id.clone())
                    .or_default()
                    .insert(owner.id.clone());
            }
        }
    }
    adjacency
}

fn expand_staged_attached_owner_ids(
    seed_owner_ids: &[String],
    owner_adjacency: &HashMap<String, BTreeSet<String>>,
    owner_by_id: &HashMap<&str, &OwnerRecord>,
) -> Vec<String> {
    let mut selected = seed_owner_ids.iter().cloned().collect::<BTreeSet<_>>();
    let mut stack = selected.iter().cloned().collect::<Vec<_>>();
    while let Some(owner_id) = stack.pop() {
        for adjacent_owner_id in owner_adjacency
            .get(owner_id.as_str())
            .into_iter()
            .flat_map(|ids| ids.iter())
        {
            if selected.insert(adjacent_owner_id.clone()) {
                stack.push(adjacent_owner_id.clone());
            }
        }
    }
    let mut owners = selected
        .into_iter()
        .filter_map(|owner_id| {
            let owner = owner_by_id.get(owner_id.as_str())?;
            Some((owner.ordinal, owner_id))
        })
        .collect::<Vec<_>>();
    owners.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    owners.into_iter().map(|(_, owner_id)| owner_id).collect()
}

#[derive(Debug, Clone)]
struct OwnerComponent {
    id: String,
    debug_id: String,
    owner_ids: Vec<String>,
    owner_ordinals: Vec<usize>,
    direct_dependency_component_ids: Vec<String>,
}

fn build_owner_components(owner_records: &[OwnerRecord]) -> Vec<OwnerComponent> {
    let mut graph = DiGraph::<String, ()>::new();
    let mut node_by_owner_id = HashMap::new();
    let module_by_owner_id = owner_records
        .iter()
        .map(|owner| (owner.id.as_str(), owner.module_id.as_str()))
        .collect::<HashMap<_, _>>();
    for owner in owner_records {
        let node = graph.add_node(owner.id.clone());
        node_by_owner_id.insert(owner.id.as_str(), node);
    }
    for owner in owner_records {
        let Some(from) = node_by_owner_id.get(owner.id.as_str()).copied() else {
            continue;
        };
        for dep in &owner.dep_owner_ids {
            if module_by_owner_id.get(dep.as_str()).copied() != Some(owner.module_id.as_str()) {
                continue;
            }
            let Some(to) = node_by_owner_id.get(dep.as_str()).copied() else {
                continue;
            };
            graph.add_edge(from, to, ());
        }
    }
    let owner_by_id = owner_records
        .iter()
        .map(|o| (o.id.as_str(), o))
        .collect::<HashMap<_, _>>();
    let mut components = tarjan_scc(&graph)
        .into_iter()
        .filter_map(|nodes| {
            let mut owners = nodes
                .into_iter()
                .filter_map(|n| graph.node_weight(n))
                .filter_map(|id| owner_by_id.get(id.as_str()).copied())
                .collect::<Vec<_>>();
            if owners.is_empty() {
                return None;
            }
            owners.sort_by_key(|o| o.ordinal);
            Some(owners)
        })
        .collect::<Vec<_>>();
    components.sort_by_key(|owners| owners[0].ordinal);
    let mut component_id_by_owner_id = HashMap::<String, String>::new();
    let mut local_component_index_by_module = HashMap::<String, usize>::new();
    let mut out = Vec::with_capacity(components.len());
    for owners in components {
        let module_id = owners[0].module_id.clone();
        let local_index = local_component_index_by_module
            .entry(module_id.clone())
            .and_modify(|index| *index += 1)
            .or_insert(0);
        let debug_id = format!("owner_component_{local_index:04}");
        out.push(OwnerComponent {
            id: format!("{module_id}::{debug_id}"),
            debug_id,
            owner_ids: owners.iter().map(|o| o.id.clone()).collect(),
            owner_ordinals: owners.iter().map(|o| o.ordinal).collect(),
            direct_dependency_component_ids: Vec::new(),
        });
    }
    for component in &out {
        for owner_id in &component.owner_ids {
            component_id_by_owner_id.insert(owner_id.clone(), component.id.clone());
        }
    }
    let deps_by_owner_id = owner_records
        .iter()
        .map(|owner| (owner.id.as_str(), owner.dep_owner_ids.clone()))
        .collect::<HashMap<_, _>>();
    let eager_deps_by_owner_id = owner_records
        .iter()
        .map(|owner| (owner.id.as_str(), owner.eager_dep_owner_ids.clone()))
        .collect::<HashMap<_, _>>();
    for component in &mut out {
        let mut deps = BTreeSet::new();
        for owner_id in &component.owner_ids {
            let mut dep_owner_ids = deps_by_owner_id
                .get(owner_id.as_str())
                .cloned()
                .unwrap_or_default();
            dep_owner_ids.extend(
                eager_deps_by_owner_id
                    .get(owner_id.as_str())
                    .cloned()
                    .unwrap_or_default(),
            );
            dep_owner_ids.sort();
            dep_owner_ids.dedup();
            for dep_owner_id in dep_owner_ids {
                let Some(dep_component_id) = component_id_by_owner_id.get(dep_owner_id.as_str())
                else {
                    continue;
                };
                if dep_component_id != &component.id {
                    deps.insert(dep_component_id.clone());
                }
            }
        }
        component.direct_dependency_component_ids = deps.into_iter().collect();
    }
    out
}

fn collect_component_closure_ids(
    seed_component_id: &str,
    component_by_id: &HashMap<&str, &OwnerComponent>,
    cache: &mut HashMap<String, Vec<String>>,
) -> Vec<String> {
    if let Some(ids) = cache.get(seed_component_id) {
        return ids.clone();
    }
    let mut closure = BTreeSet::new();
    closure.insert(seed_component_id.to_string());
    if let Some(component) = component_by_id.get(seed_component_id) {
        for dependency_component_id in &component.direct_dependency_component_ids {
            for transitive_component_id in
                collect_component_closure_ids(dependency_component_id, component_by_id, cache)
            {
                closure.insert(transitive_component_id);
            }
        }
    }
    let result = closure.into_iter().collect::<Vec<_>>();
    cache.insert(seed_component_id.to_string(), result.clone());
    result
}

struct ReplayableSideEffectState {
    id: String,
    runtime_sensitive: bool,
    touched_owner_ids: Vec<String>,
}

struct OrderedInitPlannerState {
    replayable_side_effect_ids_by_owner_id: HashMap<String, Vec<String>>,
    replayable_side_effect_state_by_id: HashMap<String, ReplayableSideEffectState>,
}

fn build_ordered_init_planner_state(analysis: &AnalysisSummary) -> OrderedInitPlannerState {
    let mut replayable_side_effect_ids_by_owner_id = HashMap::<String, Vec<String>>::new();
    for owner in &analysis.owners {
        replayable_side_effect_ids_by_owner_id.insert(owner.id.clone(), Vec::new());
    }
    let mut replayable_side_effect_state_by_id =
        HashMap::<String, ReplayableSideEffectState>::new();
    for module in &analysis.modules {
        for side_effect in &module.side_effect_records {
            if !side_effect.replayable {
                continue;
            }
            let mut touched_owner_ids = side_effect.touched_owner_ids.clone();
            touched_owner_ids.sort();
            touched_owner_ids.dedup();
            if touched_owner_ids.is_empty() {
                continue;
            }
            replayable_side_effect_state_by_id.insert(
                side_effect.id.clone(),
                ReplayableSideEffectState {
                    id: side_effect.id.clone(),
                    runtime_sensitive: side_effect.runtime_sensitive,
                    touched_owner_ids: touched_owner_ids.clone(),
                },
            );
            for owner_id in &touched_owner_ids {
                replayable_side_effect_ids_by_owner_id
                    .entry(owner_id.clone())
                    .or_default()
                    .push(side_effect.id.clone());
            }
        }
    }
    for side_effect_ids in replayable_side_effect_ids_by_owner_id.values_mut() {
        side_effect_ids.sort();
        side_effect_ids.dedup();
    }
    OrderedInitPlannerState {
        replayable_side_effect_ids_by_owner_id,
        replayable_side_effect_state_by_id,
    }
}

fn build_shell_blocking_reasons(
    shell_item_ids: &[String],
    owner_ids: &[String],
    analysis: &AnalysisSummary,
) -> Vec<String> {
    if shell_item_ids.is_empty() || owner_ids.is_empty() {
        return Vec::new();
    }
    let owner_ordinal = owner_ids
        .iter()
        .filter_map(|owner_id| program_item_ordinal(analysis, owner_id))
        .collect::<Vec<_>>();
    let mut reasons = BTreeSet::new();
    for shell_item_id in shell_item_ids {
        let Some(shell_ord) = program_item_ordinal(analysis, shell_item_id) else {
            continue;
        };
        let later_owner_ids = owner_ids
            .iter()
            .zip(owner_ordinal.iter())
            .filter_map(|(owner_id, owner_ord)| {
                if *owner_ord > shell_ord {
                    Some(owner_id.as_str())
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        if later_owner_ids.is_empty() {
            continue;
        }
        reasons.insert(format!(
            "shell_item_eagerly_uses_later_owner:{shell_item_id}:{}",
            later_owner_ids.join(",")
        ));
    }
    reasons.into_iter().collect()
}

fn build_shell_item_ids(
    covered_item_ordinals: &BTreeSet<usize>,
    owner_ids: &[String],
    analysis: &AnalysisSummary,
) -> Vec<String> {
    let Some(start) = covered_item_ordinals.iter().next().copied() else {
        return Vec::new();
    };
    let Some(end) = covered_item_ordinals.iter().next_back().copied() else {
        return Vec::new();
    };
    let selected_modules = owner_ids
        .iter()
        .filter_map(|owner_id| owner_module_id(analysis, owner_id))
        .collect::<HashSet<_>>();
    (start..=end)
        .filter_map(|module_ord| {
            for module in &analysis.modules {
                let Some(item_id) = module.program_item_ids.get(module_ord) else {
                    continue;
                };
                if owner_ids.contains(item_id) {
                    return None;
                }
                if selected_modules.contains(module.source_path.as_str()) {
                    return Some(item_id.clone());
                }
            }
            None
        })
        .collect()
}

fn structural_estimated_size(
    owner_ordinals: &BTreeSet<usize>,
    attached_item_ids: &[String],
    owner_count: usize,
    analysis: &AnalysisSummary,
) -> usize {
    let mut selected_ordinals = owner_ordinals.clone();
    for item_id in attached_item_ids {
        if let Some(ordinal) = program_item_ordinal(analysis, item_id) {
            selected_ordinals.insert(ordinal);
        }
    }
    let span = match (
        selected_ordinals.iter().next().copied(),
        selected_ordinals.iter().next_back().copied(),
    ) {
        (Some(start), Some(end)) => end.saturating_sub(start) + 1,
        _ => owner_count,
    };
    span * 1000 + owner_count
}

fn build_stage_runs(
    candidate_id: &str,
    owner_ids: &[String],
    attached_item_ids: &[String],
    analysis: &AnalysisSummary,
    owner_by_id: &HashMap<&str, &OwnerRecord>,
) -> Vec<PlannerStageRunDebug> {
    let owner_ord = owner_ids.iter().filter_map(|owner_id| {
        let owner = owner_by_id.get(owner_id.as_str())?;
        Some((
            program_item_ordinal(analysis, owner_id)?,
            owner_id.clone(),
            vec![owner_id.clone()],
            vec![owner.member_name.clone()],
        ))
    });
    let item_ord = attached_item_ids.iter().filter_map(|id| {
        Some((
            program_item_ordinal(analysis, id)?,
            id.clone(),
            Vec::new(),
            Vec::new(),
        ))
    });
    let mut ordinals = owner_ord.chain(item_ord).collect::<Vec<_>>();
    ordinals.sort_by(|l, r| l.0.cmp(&r.0).then_with(|| l.1.cmp(&r.1)));
    if ordinals.is_empty() {
        return Vec::new();
    }
    let (first_ord, first_item_id, first_owner_ids, first_member_names) = ordinals[0].clone();
    let mut runs = Vec::new();
    let mut current = PlannerStageRunDebug {
        id: format!("{candidate_id}_stage_0"),
        start_ordinal: first_ord,
        end_ordinal: first_ord,
        item_ids: vec![first_item_id],
        owner_ids: first_owner_ids,
        member_names: first_member_names,
    };
    for (ord, item_id, item_owner_ids, item_member_names) in ordinals.into_iter().skip(1) {
        if current.end_ordinal + 1 == ord {
            current.end_ordinal = ord;
            current.item_ids.push(item_id);
            current.owner_ids.extend(item_owner_ids);
            current.member_names.extend(item_member_names);
        } else {
            finalize_stage_run(&mut current);
            runs.push(current);
            current = PlannerStageRunDebug {
                id: format!("{candidate_id}_stage_{}", runs.len()),
                start_ordinal: ord,
                end_ordinal: ord,
                item_ids: vec![item_id],
                owner_ids: item_owner_ids,
                member_names: item_member_names,
            };
        }
    }
    finalize_stage_run(&mut current);
    runs.push(current);
    runs
}

fn finalize_stage_run(run: &mut PlannerStageRunDebug) {
    run.owner_ids.sort();
    run.owner_ids.dedup();
    run.member_names.sort();
    run.member_names.dedup();
}

fn program_item_ordinal(analysis: &AnalysisSummary, item_id: &str) -> Option<usize> {
    analysis
        .modules
        .iter()
        .find_map(|module| module.program_item_ids.iter().position(|id| id == item_id))
}

fn owner_module_id<'a>(analysis: &'a AnalysisSummary, owner_id: &str) -> Option<&'a str> {
    analysis
        .owners
        .iter()
        .find(|owner| owner.id == owner_id)
        .map(|owner| owner.module_id.as_str())
}

fn build_owner_ordinals(analysis: &AnalysisSummary) -> HashMap<String, usize> {
    let mut owner_ordinal_by_id = HashMap::<String, usize>::new();
    let mut ordinal = 0usize;
    for module in &analysis.modules {
        let owner_id_set = module.owner_ids.iter().collect::<HashSet<_>>();
        let canonical_owner_id_by_semantic_id = module
            .owner_ids
            .iter()
            .zip(module.member_names.iter())
            .map(|(semantic_id, member_name)| {
                (
                    semantic_id.as_str(),
                    format!("{}::{}", module.source_path, member_name),
                )
            })
            .collect::<HashMap<_, _>>();
        for program_item_id in &module.program_item_ids {
            if owner_id_set.contains(program_item_id) {
                owner_ordinal_by_id.insert(program_item_id.clone(), ordinal);
                if let Some(canonical_owner_id) =
                    canonical_owner_id_by_semantic_id.get(program_item_id.as_str())
                {
                    owner_ordinal_by_id.insert(canonical_owner_id.clone(), ordinal);
                }
            }
            ordinal += 1;
        }
    }
    owner_ordinal_by_id
}

fn build_owner_records(
    analysis: &AnalysisSummary,
    owner_ordinal_by_id: &HashMap<String, usize>,
) -> Vec<OwnerRecord> {
    let mut records = Vec::new();
    for owner in &analysis.owners {
        let ordinal = owner_ordinal_by_id
            .get(owner.id.as_str())
            .copied()
            .unwrap_or(0);
        let dep_owner_ids = owner
            .dep_edges
            .iter()
            .map(|edge| edge.to_owner_id.clone())
            .collect::<Vec<_>>();
        let eager_dep_owner_ids = owner
            .dep_edges
            .iter()
            .filter(|edge| {
                edge.phase == "eager"
                    && matches!(edge.access_kind.as_str(), "read" | "write" | "member_write")
            })
            .map(|edge| edge.to_owner_id.clone())
            .collect::<Vec<_>>();
        let write_like_dep_owner_ids = owner
            .dep_edges
            .iter()
            .filter(|edge| matches!(edge.access_kind.as_str(), "write" | "member_write"))
            .map(|edge| edge.to_owner_id.clone())
            .collect::<Vec<_>>();

        records.push(OwnerRecord {
            id: owner.id.clone(),
            module_id: owner.module_id.clone(),
            member_name: owner.member_name.clone(),
            ordinal,
            dep_owner_ids,
            eager_dep_owner_ids,
            write_like_dep_owner_ids,
        });
    }
    for record in &mut records {
        record.dep_owner_ids.sort();
        record.dep_owner_ids.dedup();
        record.eager_dep_owner_ids.sort();
        record.eager_dep_owner_ids.dedup();
        record.write_like_dep_owner_ids.sort();
        record.write_like_dep_owner_ids.dedup();
    }
    records.sort_by_key(|record| record.ordinal);
    records
}

fn derive_blocking_reasons(
    owner_ids: &[String],
    owner_by_id: &HashMap<&str, &OwnerRecord>,
) -> Vec<String> {
    let owner_set = owner_ids.iter().cloned().collect::<HashSet<_>>();
    let mut unsupported_forward_eager_dependency = BTreeSet::new();
    let mut depends_on_outside_local_owner = BTreeSet::new();
    let written_by_outside_item = BTreeSet::new();
    let unsupported_owner = BTreeSet::new();
    let runtime_sensitive_owner = BTreeSet::new();
    let extractor_incompatible_owner = BTreeSet::new();
    let runtime_sensitive_side_effect = BTreeSet::new();
    let writes_runtime_import = BTreeSet::new();
    let used_eagerly_before_region = BTreeSet::new();

    for owner_id in owner_ids {
        let Some(owner) = owner_by_id.get(owner_id.as_str()) else {
            continue;
        };
        for dep_owner_id in &owner.dep_owner_ids {
            if !owner_set.contains(dep_owner_id) {
                depends_on_outside_local_owner.insert(dep_owner_id.clone());
            }
        }
        for eager_dep_owner_id in &owner.eager_dep_owner_ids {
            if owner_set.contains(eager_dep_owner_id) {
                let Some(target_owner) = owner_by_id.get(eager_dep_owner_id.as_str()) else {
                    continue;
                };
                if target_owner.ordinal > owner.ordinal {
                    unsupported_forward_eager_dependency
                        .insert(format!("{owner_id}->{eager_dep_owner_id}"));
                }
            }
        }
    }

    compose_blocking_reasons(&BlockingReasonInputs {
        unsupported_owner,
        runtime_sensitive_owner,
        extractor_incompatible_owner,
        runtime_sensitive_side_effect,
        writes_runtime_import,
        depends_on_outside_local_owner,
        unsupported_forward_eager_dependency,
        written_by_outside_item,
        used_eagerly_before_region,
    })
}

struct BlockingReasonInputs {
    unsupported_owner: BTreeSet<String>,
    runtime_sensitive_owner: BTreeSet<String>,
    extractor_incompatible_owner: BTreeSet<String>,
    runtime_sensitive_side_effect: BTreeSet<String>,
    writes_runtime_import: BTreeSet<String>,
    depends_on_outside_local_owner: BTreeSet<String>,
    unsupported_forward_eager_dependency: BTreeSet<String>,
    written_by_outside_item: BTreeSet<String>,
    used_eagerly_before_region: BTreeSet<String>,
}

fn compose_blocking_reasons(input: &BlockingReasonInputs) -> Vec<String> {
    let mut out = Vec::new();
    push_reason(
        &mut out,
        "unsupported_owner",
        input.unsupported_owner.iter().cloned().collect(),
    );
    push_reason(
        &mut out,
        "runtime_sensitive_owner",
        input.runtime_sensitive_owner.iter().cloned().collect(),
    );
    push_reason(
        &mut out,
        "extractor_incompatible_owner",
        input.extractor_incompatible_owner.iter().cloned().collect(),
    );
    push_reason(
        &mut out,
        "runtime_sensitive_side_effect",
        input
            .runtime_sensitive_side_effect
            .iter()
            .cloned()
            .collect(),
    );
    push_reason(
        &mut out,
        "writes_runtime_import",
        input.writes_runtime_import.iter().cloned().collect(),
    );
    push_reason(
        &mut out,
        "depends_on_outside_local_owner",
        input
            .depends_on_outside_local_owner
            .iter()
            .cloned()
            .collect(),
    );
    push_reason(
        &mut out,
        "unsupported_forward_eager_dependency",
        input
            .unsupported_forward_eager_dependency
            .iter()
            .cloned()
            .collect(),
    );
    push_reason(
        &mut out,
        "written_by_outside_item",
        input.written_by_outside_item.iter().cloned().collect(),
    );
    push_reason(
        &mut out,
        "used_eagerly_before_region",
        input.used_eagerly_before_region.iter().cloned().collect(),
    );
    out
}

fn push_reason(out: &mut Vec<String>, class_name: &str, mut values: Vec<String>) {
    if values.is_empty() {
        return;
    }
    values.sort();
    values.dedup();
    out.push(format!("{class_name}:{}", values.join(",")));
}

fn pack_candidates(candidates: Vec<ClosureCandidate>) -> (Vec<Vec<String>>, Vec<ClosureCandidate>) {
    pack_candidates_with_preselected(candidates, &HashSet::new())
}

fn pack_candidates_with_preselected(
    candidates: Vec<ClosureCandidate>,
    preselected_ids: &HashSet<String>,
) -> (Vec<Vec<String>>, Vec<ClosureCandidate>) {
    let mut selected = Vec::new();
    let mut selected_candidates = Vec::new();
    let mut selected_ids = preselected_ids.clone();
    let mut occupied_owners = HashSet::new();
    let mut occupied_item_ids = HashSet::new();

    for candidate in candidates.iter().cloned() {
        if !selected_ids.contains(&candidate.id) {
            continue;
        }
        for owner_id in &candidate.owner_ids {
            occupied_owners.insert(owner_id.clone());
        }
        for item_id in &candidate.attached_item_ids {
            occupied_item_ids.insert(item_id.clone());
        }
        selected_candidates.push(candidate);
    }

    for candidate in candidates {
        if !candidate.blocking_reasons.is_empty() {
            continue;
        }
        if candidate
            .owner_ids
            .iter()
            .any(|owner_id| occupied_owners.contains(owner_id))
        {
            continue;
        }
        if candidate
            .attached_item_ids
            .iter()
            .any(|item_id| occupied_item_ids.contains(item_id))
        {
            continue;
        }
        for owner_id in &candidate.owner_ids {
            occupied_owners.insert(owner_id.clone());
        }
        for item_id in &candidate.attached_item_ids {
            occupied_item_ids.insert(item_id.clone());
        }
        selected.push(candidate.member_names.clone());
        selected_ids.insert(candidate.id.clone());
        selected_candidates.push(candidate);
    }

    selected_candidates.sort_by(|left, right| {
        left.start_ordinal
            .cmp(&right.start_ordinal)
            .then_with(|| left.id.cmp(&right.id))
    });
    (selected, selected_candidates)
}

impl PlannerCandidateDebug {
    fn from_candidate(value: &ClosureCandidate) -> Self {
        let semantic_owner_ids = value.owner_ids.clone();
        Self {
            id: value.id.clone(),
            owner_ids: semantic_owner_ids.clone(),
            member_names: value.member_names.clone(),
            estimated_size: value.estimated_size,
            blocking_reasons: value.blocking_reasons.clone(),
            attached_item_ids: value.attached_item_ids.clone(),
            semantic_owner_ids,
            semantic_member_names: value.member_names.clone(),
            start_ordinal: value.start_ordinal,
            shell_item_ids: value.shell_item_ids.clone(),
            semantic_blocking_reasons: value.semantic_blocking_reasons.clone(),
            stage_runs: value.stage_runs.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::pipeline::{AnalysisSummary, ModuleAnalysis};

    use super::{
        ClosureCandidate, build_shell_blocking_reasons, pack_candidates,
        pack_candidates_with_preselected,
    };

    fn candidate(
        id: &str,
        owner_ids: &[&str],
        attached_item_ids: &[&str],
        start_ordinal: usize,
    ) -> ClosureCandidate {
        ClosureCandidate {
            id: id.to_string(),
            owner_ids: owner_ids.iter().map(|s| s.to_string()).collect(),
            member_names: vec![id.to_string()],
            estimated_size: 1,
            blocking_reasons: Vec::new(),
            attached_item_ids: attached_item_ids.iter().map(|s| s.to_string()).collect(),
            start_ordinal,
            shell_item_ids: Vec::new(),
            semantic_blocking_reasons: Vec::new(),
            stage_runs: Vec::new(),
        }
    }

    #[test]
    fn pack_candidates_respects_owner_and_item_occupancy() {
        let (groups, selected) = pack_candidates(vec![
            candidate("c0", &["o1"], &["i1"], 10),
            candidate("c1", &["o1"], &["i2"], 20),
            candidate("c2", &["o2"], &["i1"], 30),
            candidate("c3", &["o3"], &["i3"], 40),
        ]);

        assert_eq!(
            groups,
            vec![vec!["c0".to_string()], vec!["c3".to_string()],]
        );
        assert_eq!(
            selected.into_iter().map(|c| c.id).collect::<Vec<_>>(),
            vec!["c0".to_string(), "c3".to_string()]
        );
    }

    #[test]
    fn pack_candidates_sorts_selected_by_start_ordinal_then_id() {
        let (_, selected) = pack_candidates(vec![
            candidate("c2", &["o2"], &["i2"], 20),
            candidate("c0", &["o0"], &["i0"], 10),
            candidate("c1", &["o1"], &["i1"], 10),
        ]);
        assert_eq!(
            selected.into_iter().map(|c| c.id).collect::<Vec<_>>(),
            vec!["c0".to_string(), "c1".to_string(), "c2".to_string()]
        );
    }

    #[test]
    fn pack_candidates_honors_preselected_ids_before_greedy_pass() {
        let preselected = HashSet::from(["c1".to_string()]);
        let (groups, selected) = pack_candidates_with_preselected(
            vec![
                candidate("c0", &["o0"], &["i0"], 10),
                candidate("c1", &["o1"], &["i1"], 20),
            ],
            &preselected,
        );
        assert_eq!(
            selected.into_iter().map(|c| c.id).collect::<Vec<_>>(),
            vec!["c0".to_string(), "c1".to_string()]
        );
        assert_eq!(groups, vec![vec!["c0".to_string()]]);
    }

    #[test]
    fn shell_blocking_reasons_emit_later_owner_class_payload() {
        let analysis = AnalysisSummary {
            modules: vec![ModuleAnalysis {
                member_names: vec!["a".to_string(), "b".to_string()],
                source_path: "a.js".to_string(),
                import_specifiers: vec![],
                resolved_deps: vec![],
                export_count: 0,
                has_top_level_effects: false,
                owner_ids: vec![],
                owner_semantic_id_by_member_name: HashMap::new(),
                program_item_ids: vec![
                    "a.js::a".to_string(),
                    "a.js::shell".to_string(),
                    "a.js::b".to_string(),
                ],
                side_effect_ids: vec![],
                replayable_side_effect_ids: vec![],
                runtime_sensitive_effects: false,
                side_effect_touched_owner_ids: vec![],
                side_effect_records: vec![],
            }],
            owners: vec![],
        };
        let reasons = build_shell_blocking_reasons(
            &["a.js::shell".to_string()],
            &["a.js::a".to_string(), "a.js::b".to_string()],
            &analysis,
        );
        assert_eq!(reasons.len(), 1);
        assert!(
            reasons[0].starts_with("shell_item_eagerly_uses_later_owner:a.js::shell"),
            "unexpected shell blocking reason: {}",
            reasons[0]
        );
        assert!(reasons[0].contains("a.js::b"));
        assert!(!reasons[0].contains("a.js::a"));
    }
}
