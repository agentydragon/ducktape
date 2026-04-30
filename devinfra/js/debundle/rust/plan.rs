use std::collections::{BTreeSet, HashMap, HashSet};

use petgraph::algo::tarjan_scc;
use petgraph::graph::DiGraph;
use serde::Serialize;

use crate::owner_graph::OwnerGraph;
use crate::pipeline::AnalysisSummary;

#[derive(Debug, Clone, Serialize)]
pub struct PlannerDebugSnapshot {
    pub candidates: Vec<PlannerCandidateDebug>,
    pub selected: Vec<PlannerCandidateDebug>,
    pub ordered_init_state: PlannerOrderedInitStateDebug,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannerOrderedInitStateDebug {
    pub replayable_side_effect_ids_by_owner_id: HashMap<String, Vec<String>>,
    pub replayable_side_effect_state_by_id: HashMap<String, ReplayableSideEffectStateDebug>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReplayableSideEffectStateDebug {
    pub id: String,
    pub runtime_sensitive: bool,
    pub touched_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
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
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProgramItemKind {
    Declaration,
    NonDeclaration,
}

pub fn build_plan(owner_graph: &OwnerGraph, analysis: &AnalysisSummary) -> PlanSummaryV2 {
    let selected_modules = sorted_modules(owner_graph);
    let candidates = build_closure_candidates(owner_graph, analysis);
    let (extraction_groups, selected_candidates) = pack_candidates(candidates.clone());
    PlanSummaryV2 {
        selected_modules,
        extraction_groups,
        rationale: "selected-owner closure planning over dependency components with side-effect order constraints"
            .to_string(),
        debug: PlannerDebugSnapshot {
            candidates: candidates.iter().map(PlannerCandidateDebug::from).collect(),
            selected: selected_candidates.iter().map(PlannerCandidateDebug::from).collect(),
            ordered_init_state: planner_state_debug(analysis),
        },
    }
}

fn planner_state_debug(analysis: &AnalysisSummary) -> PlannerOrderedInitStateDebug {
    let state = build_ordered_init_planner_state(analysis);
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
) -> Vec<ClosureCandidate> {
    let planner_state = build_ordered_init_planner_state(analysis);
    let (owner_ordinal_by_id, program_item_kind_by_ordinal) = build_program_item_ordinals(analysis);
    let owner_records = build_owner_records(analysis, &owner_ordinal_by_id);
    let owner_by_id = owner_records
        .iter()
        .map(|record| (record.id.as_str(), record))
        .collect::<HashMap<_, _>>();

    let components = build_owner_components(&owner_records);
    let mut component_by_id = HashMap::new();
    let mut component_id_by_owner_id = HashMap::<String, String>::new();
    for component in &components {
        component_by_id.insert(component.id.as_str(), component);
        for owner_id in &component.owner_ids {
            component_id_by_owner_id.insert(owner_id.clone(), component.id.clone());
        }
    }
    let mut closure_ids_cache = HashMap::<String, Vec<String>>::new();
    let owner_by_ordinal = owner_records
        .iter()
        .map(|owner| (owner.ordinal, owner.id.clone()))
        .collect::<HashMap<_, _>>();

    let mut candidates = Vec::new();
    let mut seen_closure_signatures = BTreeSet::new();
    for seed_component in &components {
        let required_component_ids = collect_component_closure_ids(
            seed_component.id.as_str(),
            &component_by_id,
            &mut closure_ids_cache,
        );
        let closure_owner_ids = expand_contiguous_envelope_owner_ids(
            &required_component_ids,
            &component_by_id,
            &component_id_by_owner_id,
            &owner_by_ordinal,
            &program_item_kind_by_ordinal,
            &mut closure_ids_cache,
        );

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
        let closure_signature = owner_ids.join("\n");
        if !seen_closure_signatures.insert(closure_signature) {
            continue;
        }
        let semantic_blocking_reasons = derive_blocking_reasons(&owner_ids, &owner_by_id, analysis);
        let mut attached_item_ids = BTreeSet::new();
        let mut covered_module_ordinals = BTreeSet::new();
        let mut start_ordinal = usize::MAX;
        for owner_id in &owner_ids {
            if let Some(owner) = owner_by_id.get(owner_id.as_str()) {
                if let Some(module_index) = analysis
                    .modules
                    .iter()
                    .position(|m| m.source_path == owner.module_id)
                {
                    start_ordinal = start_ordinal.min(module_index);
                    covered_module_ordinals.insert(module_index);
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
        let shell_item_ids = build_shell_item_ids(&covered_module_ordinals, &owner_ids, analysis);
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
        let candidate_id = format!("selected_module_group_{:04}", candidates.len());
        candidates.push(ClosureCandidate {
            id: candidate_id.clone(),
            owner_ids: owner_ids.clone(),
            estimated_size: member_names.len(),
            blocking_reasons: blocking_reasons.clone(),
            member_names: member_names.into_iter().collect(),
            attached_item_ids: attached_item_ids_vec.clone(),
            start_ordinal: if start_ordinal == usize::MAX {
                analysis
                    .modules
                    .iter()
                    .position(|m| m.source_path == seed_component.module_id_hint)
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
    candidates
}

fn expand_contiguous_envelope_owner_ids(
    initial_component_ids: &[String],
    component_by_id: &HashMap<&str, &OwnerComponent>,
    component_id_by_owner_id: &HashMap<String, String>,
    owner_by_ordinal: &HashMap<usize, String>,
    program_item_kind_by_ordinal: &HashMap<usize, ProgramItemKind>,
    closure_ids_cache: &mut HashMap<String, Vec<String>>,
) -> Vec<String> {
    let mut selected_component_ids = initial_component_ids
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>();

    let mut changed = true;
    while changed {
        changed = false;
        let mut selected_owner_ids =
            owners_for_component_ids(&selected_component_ids, component_by_id);
        if selected_owner_ids.is_empty() {
            break;
        }
        selected_owner_ids.sort_by_key(|(_, ordinal)| *ordinal);
        let start_ordinal = selected_owner_ids.first().map(|(_, ord)| *ord).unwrap_or(0);
        let end_ordinal = selected_owner_ids.last().map(|(_, ord)| *ord).unwrap_or(0);
        for ordinal in start_ordinal..=end_ordinal {
            let Some(program_item_kind) = program_item_kind_by_ordinal.get(&ordinal).copied()
            else {
                // Missing-program-item barrier.
                changed = false;
                break;
            };
            if program_item_kind != ProgramItemKind::Declaration {
                // Non-declaration barrier.
                changed = false;
                break;
            }
            let Some(owner_id) = owner_by_ordinal.get(&ordinal) else {
                // Missing-declaration barrier.
                changed = false;
                break;
            };
            let Some(component_id) = component_id_by_owner_id.get(owner_id) else {
                // JS contiguous-envelope behavior treats missing component mapping
                // as envelope barrier.
                changed = false;
                break;
            };
            if selected_component_ids.insert(component_id.clone()) {
                changed = true;
                for dep_component_id in collect_component_closure_ids(
                    component_id.as_str(),
                    component_by_id,
                    closure_ids_cache,
                ) {
                    if selected_component_ids.insert(dep_component_id) {
                        changed = true;
                    }
                }
            }
        }
    }
    let mut owners = Vec::new();
    let mut seen = HashSet::new();
    for (owner_id, _) in owners_for_component_ids(&selected_component_ids, component_by_id) {
        if seen.insert(owner_id.clone()) {
            owners.push(owner_id);
        }
    }
    owners
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

#[derive(Debug, Clone)]
struct OwnerComponent {
    id: String,
    owner_ids: Vec<String>,
    owner_ordinals: Vec<usize>,
    module_id_hint: String,
    direct_dependency_component_ids: Vec<String>,
}

fn build_owner_components(owner_records: &[OwnerRecord]) -> Vec<OwnerComponent> {
    let mut graph = DiGraph::<String, ()>::new();
    let mut node_by_owner_id = HashMap::new();
    for owner in owner_records {
        let node = graph.add_node(owner.id.clone());
        node_by_owner_id.insert(owner.id.as_str(), node);
    }
    for owner in owner_records {
        let Some(from) = node_by_owner_id.get(owner.id.as_str()).copied() else {
            continue;
        };
        for dep in &owner.dep_owner_ids {
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
    let mut out = components
        .into_iter()
        .enumerate()
        .map(|(index, owners)| OwnerComponent {
            id: format!("owner_component_{index:04}"),
            owner_ids: owners.iter().map(|o| o.id.clone()).collect(),
            owner_ordinals: owners.iter().map(|o| o.ordinal).collect(),
            module_id_hint: owners[0].module_id.clone(),
            direct_dependency_component_ids: Vec::new(),
        })
        .collect::<Vec<_>>();
    for component in &out {
        for owner_id in &component.owner_ids {
            component_id_by_owner_id.insert(owner_id.clone(), component.id.clone());
        }
    }
    let deps_by_owner_id = owner_records
        .iter()
        .map(|owner| (owner.id.as_str(), owner.dep_owner_ids.clone()))
        .collect::<HashMap<_, _>>();
    for component in &mut out {
        let mut deps = BTreeSet::new();
        for owner_id in &component.owner_ids {
            for dep_owner_id in deps_by_owner_id
                .get(owner_id.as_str())
                .cloned()
                .unwrap_or_default()
            {
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
    let mut stack = vec![seed_component_id.to_string()];
    while let Some(component_id) = stack.pop() {
        if !closure.insert(component_id.clone()) {
            continue;
        }
        let Some(component) = component_by_id.get(component_id.as_str()) else {
            continue;
        };
        for dependency_component_id in &component.direct_dependency_component_ids {
            stack.push(dependency_component_id.clone());
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
    let owner_module_ord = owner_ids
        .iter()
        .filter_map(|owner_id| {
            let module_id = owner_id.split("::").next()?;
            analysis
                .modules
                .iter()
                .position(|m| m.source_path == module_id)
        })
        .collect::<Vec<_>>();
    let mut reasons = BTreeSet::new();
    for shell_item_id in shell_item_ids {
        let Some((prefix, module_path)) = shell_item_id.split_once(':') else {
            continue;
        };
        let Some(ord_text) = prefix.rsplit('_').next() else {
            continue;
        };
        let Ok(shell_ord) = ord_text.parse::<usize>() else {
            continue;
        };
        let later_owner_ids = owner_ids
            .iter()
            .zip(owner_module_ord.iter())
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
            "shell_item_eagerly_uses_later_owner:{prefix}:{module_path}:{}",
            later_owner_ids.join(",")
        ));
    }
    reasons.into_iter().collect()
}

fn build_shell_item_ids(
    covered_module_ordinals: &BTreeSet<usize>,
    owner_ids: &[String],
    analysis: &AnalysisSummary,
) -> Vec<String> {
    let Some(start) = covered_module_ordinals.iter().next().copied() else {
        return Vec::new();
    };
    let Some(end) = covered_module_ordinals.iter().next_back().copied() else {
        return Vec::new();
    };
    let selected_modules = owner_ids
        .iter()
        .filter_map(|owner_id| owner_id.split("::").next())
        .collect::<HashSet<_>>();
    (start..=end)
        .filter_map(|module_ord| {
            let module = analysis.modules.get(module_ord)?;
            if selected_modules.contains(module.source_path.as_str()) {
                return None;
            }
            Some(format!(
                "shell_item_module_{module_ord:05}:{}",
                module.source_path
            ))
        })
        .collect()
}

fn build_stage_runs(
    candidate_id: &str,
    owner_ids: &[String],
    attached_item_ids: &[String],
    analysis: &AnalysisSummary,
) -> Vec<PlannerStageRunDebug> {
    let owner_ord = owner_ids.iter().filter_map(|owner_id| {
        let module_id = owner_id.split("::").next()?;
        analysis
            .modules
            .iter()
            .position(|m| m.source_path == module_id)
    });
    let item_ord = attached_item_ids
        .iter()
        .filter_map(|id| id.rsplit('_').next()?.parse::<usize>().ok());
    let mut ordinals = owner_ord
        .map(|ord| (ord, format!("owner_ordinal_{ord:05}")))
        .chain(item_ord.map(|ord| (ord, format!("side_effect_ordinal_{ord:05}"))))
        .collect::<Vec<_>>();
    ordinals.sort_by(|l, r| l.0.cmp(&r.0).then_with(|| l.1.cmp(&r.1)));
    if ordinals.is_empty() {
        return Vec::new();
    }
    let mut runs = Vec::new();
    let mut current = PlannerStageRunDebug {
        id: format!("{candidate_id}_stage_0"),
        start_ordinal: ordinals[0].0,
        end_ordinal: ordinals[0].0,
        item_ids: vec![ordinals[0].1.clone()],
        owner_ids: owner_ids.to_vec(),
        member_names: owner_ids
            .iter()
            .filter_map(|owner_id| owner_id.rsplit("::").next().map(|s| s.to_string()))
            .collect(),
    };
    for (ord, item_id) in ordinals.into_iter().skip(1) {
        if current.end_ordinal + 1 == ord {
            current.end_ordinal = ord;
            current.item_ids.push(item_id);
        } else {
            runs.push(current);
            current = PlannerStageRunDebug {
                id: format!("{candidate_id}_stage_{}", runs.len()),
                start_ordinal: ord,
                end_ordinal: ord,
                item_ids: vec![item_id],
                owner_ids: owner_ids.to_vec(),
                member_names: owner_ids
                    .iter()
                    .filter_map(|owner_id| owner_id.rsplit("::").next().map(|s| s.to_string()))
                    .collect(),
            };
        }
    }
    runs.push(current);
    runs
}

fn build_program_item_ordinals(
    analysis: &AnalysisSummary,
) -> (HashMap<String, usize>, HashMap<usize, ProgramItemKind>) {
    let mut owner_ordinal_by_id = HashMap::<String, usize>::new();
    let mut program_item_kind_by_ordinal = HashMap::<usize, ProgramItemKind>::new();
    let mut ordinal = 0usize;
    for module in &analysis.modules {
        let owner_id_set = module.owner_ids.iter().collect::<HashSet<_>>();
        for program_item_id in &module.program_item_ids {
            let kind = if owner_id_set.contains(program_item_id) {
                owner_ordinal_by_id.insert(program_item_id.clone(), ordinal);
                ProgramItemKind::Declaration
            } else {
                ProgramItemKind::NonDeclaration
            };
            program_item_kind_by_ordinal.insert(ordinal, kind);
            ordinal += 1;
        }
    }
    (owner_ordinal_by_id, program_item_kind_by_ordinal)
}

fn build_owner_records(
    analysis: &AnalysisSummary,
    owner_ordinal_by_id: &HashMap<String, usize>,
) -> Vec<OwnerRecord> {
    let mut records = analysis
        .owners
        .iter()
        .map(|owner| OwnerRecord {
            id: owner.id.clone(),
            module_id: owner.module_id.clone(),
            member_name: owner.member_name.clone(),
            ordinal: owner_ordinal_by_id
                .get(owner.id.as_str())
                .copied()
                .unwrap_or(usize::MAX),
            dep_owner_ids: owner
                .accesses
                .iter()
                .filter_map(|access| {
                    if access.kind != "local_declaration"
                        || access.phase != "eager"
                        || !matches!(
                            access.access_kind.as_str(),
                            "read" | "write" | "member_write"
                        )
                    {
                        return None;
                    }
                    access.owner_id.clone()
                })
                .collect(),
            eager_dep_owner_ids: owner
                .accesses
                .iter()
                .filter_map(|access| {
                    if access.kind != "local_declaration"
                        || access.phase != "eager"
                        || !matches!(
                            access.access_kind.as_str(),
                            "read" | "write" | "member_write"
                        )
                    {
                        return None;
                    }
                    access.owner_id.clone()
                })
                .collect(),
        })
        .collect::<Vec<_>>();
    records.sort_by(|l, r| l.id.cmp(&r.id));
    records
}

fn derive_blocking_reasons(
    owner_ids: &[String],
    owner_by_id: &HashMap<&str, &OwnerRecord>,
    analysis: &AnalysisSummary,
) -> Vec<String> {
    let owner_set = owner_ids.iter().cloned().collect::<HashSet<_>>();
    let module_by_id = analysis
        .modules
        .iter()
        .map(|m| (m.source_path.as_str(), m))
        .collect::<HashMap<_, _>>();
    let module_ord = analysis
        .modules
        .iter()
        .enumerate()
        .map(|(idx, m)| (m.source_path.as_str(), idx))
        .collect::<HashMap<_, _>>();
    let mut unsupported_forward_eager_dependency = BTreeSet::new();
    let mut depends_on_outside_local_owner = BTreeSet::new();
    let mut written_by_outside_item = BTreeSet::new();
    let mut unsupported_owner = BTreeSet::new();
    let mut runtime_sensitive_owner = BTreeSet::new();
    let extractor_incompatible_owner = BTreeSet::new();
    let runtime_sensitive_side_effect = BTreeSet::new();
    let writes_runtime_import = BTreeSet::new();
    let mut used_eagerly_before_region = BTreeSet::new();

    for owner_id in owner_ids {
        let Some(owner) = owner_by_id.get(owner_id.as_str()) else {
            continue;
        };
        let Some(module) = module_by_id.get(owner.module_id.as_str()) else {
            continue;
        };
        if owner.member_name.contains('$') {
            unsupported_owner.insert(owner.id.clone());
        }
        if module.has_top_level_effects {
            runtime_sensitive_owner.insert(owner.id.clone());
        }
        if module.has_top_level_effects {
            for dep in &module.resolved_deps {
                let dep_owner_ids = owner_by_id
                    .values()
                    .filter(|owner_record| owner_record.module_id == *dep)
                    .map(|owner_record| owner_record.id.as_str())
                    .collect::<Vec<_>>();
                let dep_inside_closure = dep_owner_ids
                    .iter()
                    .any(|dep_owner_id| owner_set.contains(*dep_owner_id));
                if dep_inside_closure {
                    continue;
                }
                let dep_owner_count = dep_owner_ids.len();
                if dep_owner_count == 0 {
                    depends_on_outside_local_owner.insert(format!("{owner_id}:{dep}"));
                }
                if let Some(dep_module) = module_by_id.get(dep.as_str()) {
                    if dep_module.has_top_level_effects {
                        written_by_outside_item.insert(format!("{owner_id}:{dep}"));
                    }
                    if module.has_top_level_effects {
                        if let (Some(owner_ord), Some(dep_ord)) = (
                            module_ord.get(module.source_path.as_str()),
                            module_ord.get(dep_module.source_path.as_str()),
                        ) {
                            if dep_ord < owner_ord {
                                unsupported_forward_eager_dependency
                                    .insert(format!("{owner_id}:{dep}"));
                                for eager_dep_owner_id in &owner.eager_dep_owner_ids {
                                    if eager_dep_owner_id.starts_with(&format!("{dep}::")) {
                                        used_eagerly_before_region
                                            .insert(format!("{owner_id}:{eager_dep_owner_id}"));
                                    }
                                }
                            }
                        }
                    }
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

impl From<&ClosureCandidate> for PlannerCandidateDebug {
    fn from(value: &ClosureCandidate) -> Self {
        Self {
            id: value.id.clone(),
            owner_ids: value.owner_ids.clone(),
            member_names: value.member_names.clone(),
            estimated_size: value.estimated_size,
            blocking_reasons: value.blocking_reasons.clone(),
            attached_item_ids: value.attached_item_ids.clone(),
            semantic_owner_ids: value.owner_ids.clone(),
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
    use std::collections::HashSet;

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
        assert_eq!(
            groups,
            vec![vec!["c0".to_string()], vec!["c1".to_string()],]
        );
    }

    #[test]
    fn shell_blocking_reasons_emit_later_owner_class_payload() {
        let analysis = AnalysisSummary {
            modules: vec![
                ModuleAnalysis {
                    member_names: vec!["a".to_string()],
                    source_path: "a.js".to_string(),
                    import_specifiers: vec![],
                    resolved_deps: vec![],
                    export_count: 0,
                    has_top_level_effects: false,
                    owner_ids: vec![],
                    program_item_ids: vec![],
                    side_effect_ids: vec![],
                    replayable_side_effect_ids: vec![],
                    runtime_sensitive_effects: false,
                    side_effect_touched_owner_ids: vec![],
                    side_effect_records: vec![],
                },
                ModuleAnalysis {
                    member_names: vec!["b".to_string()],
                    source_path: "b.js".to_string(),
                    import_specifiers: vec![],
                    resolved_deps: vec![],
                    export_count: 0,
                    has_top_level_effects: false,
                    owner_ids: vec![],
                    program_item_ids: vec![],
                    side_effect_ids: vec![],
                    replayable_side_effect_ids: vec![],
                    runtime_sensitive_effects: false,
                    side_effect_touched_owner_ids: vec![],
                    side_effect_records: vec![],
                },
            ],
            owners: vec![],
        };
        let reasons = build_shell_blocking_reasons(
            &["shell_item_module_00000:a.js".to_string()],
            &["a.js::a".to_string(), "b.js::b".to_string()],
            &analysis,
        );
        assert_eq!(reasons.len(), 1);
        assert!(
            reasons[0].starts_with("shell_item_eagerly_uses_later_owner:shell_item_module_00000"),
            "unexpected shell blocking reason: {}",
            reasons[0]
        );
        assert!(reasons[0].contains("b.js::b"));
    }
}
