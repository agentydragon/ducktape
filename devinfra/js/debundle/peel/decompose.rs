//! Read-only module decomposition: splits existing spec modules into the
//! same-module classes that survive the gated seed pass, then optionally
//! coarsens atomic conflicts, keeps selector anchors together and rejoins
//! fragments of one original home. Proposal ranking over residual source
//! units lives in `propose`.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{
    DependencyGraphInput, DependencyGraphReport, ModuleAssignment, ModuleDestinationKind, ModuleId,
    StatementKind,
};
use anonymous_resolution::{
    ModuleNeutralReferenceMeaning, ResolvedLocalMemberOrigin, ResolvedLocalMemberReference,
    ResolvedSourceClaims, SourceClaimSet, resolve_source_claims,
};
use spec::{MemberSelectorSpec, ModulePath};
use spec_modules::{
    ModuleClaims, collect_module_files, module_claims, module_path_from_file, read_module_file,
};
use spec_tree::ModuleTreeScope;

use crate::propose::{ProposalOptions, active_claims_from_options, active_module_label};
use crate::quotient::{
    ClassId, QuotientGraph, SourceUnitIndex, SpecModuleGroup, build_same_module_seed_quotient,
};

/// Read-only candidate formed by splitting existing modules into the
/// same-module classes that survived the gated seed pass.
#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionReport {
    pub strategy: ModuleDecompositionStrategy,
    /// True only when the candidate also keeps resolved local selector
    /// anchors with the members that reference them.
    #[serde(skip_serializing_if = "is_false")]
    pub preserve_selector_anchors: bool,
    /// Resolved local anchor relationships when the opt-in mode is enabled.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selector_anchor_relations: Option<Vec<ModuleDecompositionSelectorAnchor>>,
    /// True only when the candidate preserves whole structural atoms and
    /// passes the shared assignment gate over the complete dependency graph.
    pub candidate_is_realizable: bool,
    pub coverage_complete: bool,
    pub source_unit_count: usize,
    pub assigned_source_unit_count: usize,
    pub unchanged_module_count: usize,
    /// Existing modules that would split into two or more suggested parts.
    pub modules: Vec<ModuleDecomposition>,
    /// Structural atoms that the candidate would assign to multiple modules.
    pub atomic_group_conflicts: Vec<ModuleDecompositionAtomicConflict>,
    /// The exact cycle and rebinding result from the shared assignment gate.
    pub cycles: Vec<gate::CycleReport>,
    pub cross_rebinds: Vec<String>,
    /// Present only when the caller explicitly requested SCC coarsening.
    /// `modules` remains the initial decomposition; this records the final
    /// candidate and the gate-reported merges used to reach it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub coarsening: Option<ModuleDecompositionCoarsening>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModuleDecompositionCoarseningMode {
    None,
    MergeBlockingSccs,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModuleDecompositionCoarseningStopReason {
    Accepted,
    AtomicGroupConflicts,
    CrossModuleRebinds,
    NoProgress,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionCoarsening {
    pub stop_reason: ModuleDecompositionCoarseningStopReason,
    /// Whole candidate-group merges required by reported structural atoms.
    /// Empty unless the caller opted in to coarsening atomic conflicts.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub atomic_conflict_merges: Vec<ModuleDecompositionAtomicConflictMerge>,
    pub rounds: Vec<ModuleDecompositionCoarseningRound>,
    /// Final ownership under the names checked by the last full gate run.
    /// These names are suggestions, and can combine distinct source homes.
    pub groups: Vec<ModuleDecompositionCoarsenedGroup>,
    /// Source-resolved readable export names that would collide if a final
    /// group were materialized. Only available with selector-anchor resolution.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub naming_collisions: Option<Vec<ModuleDecompositionNamingCollision>>,
    /// Present only when the caller asked to safely rejoin fragments from
    /// their original semantic homes after SCC coarsening.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub original_home_rejoining: Option<ModuleDecompositionHomeRejoining>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionAtomicConflictMerge {
    pub atomic_group_ids: Vec<String>,
    pub merged_candidate_paths: Vec<ModulePath>,
    pub suggested_module_path: ModulePath,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionHomeRejoining {
    pub stop_reason: ModuleDecompositionHomeRejoiningStopReason,
    pub attempted_contractions: usize,
    pub accepted_contractions: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pair_attempt_limit: Option<usize>,
    pub whole_home_pass_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub whole_home_pass_limit: Option<usize>,
    pub budget_limited_home_count: usize,
    pub budget_limited_home_paths: Vec<ModulePath>,
    pub original_module_count: usize,
    pub fully_restored_module_count: usize,
    pub fully_restored_module_paths: Vec<ModulePath>,
    pub unrestored_module_paths: Vec<ModulePath>,
    pub final_group_count: usize,
    pub largest_group_source_unit_count: usize,
    pub largest_group_origin_module_count: usize,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModuleDecompositionHomeRejoiningStopReason {
    CandidateNotRealizable,
    Converged,
    PairAttemptLimitReached,
    WholeHomePassLimitReached,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionCoarseningRound {
    /// The exact blocking SCCs consumed from the shared gate report.
    pub blocking_sccs: Vec<Vec<ModulePath>>,
    pub suggested_merge_paths: Vec<ModulePath>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionCoarsenedGroup {
    pub suggested_module_path: ModulePath,
    /// Original source module labels represented by this candidate group.
    pub origin_module_paths: Vec<ModulePath>,
    pub source_unit_ids: Vec<String>,
    pub binding_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionNamingCollision {
    pub suggested_module_path: ModulePath,
    pub readable_name: String,
    pub claims: Vec<ModuleDecompositionNamedClaim>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionNamedClaim {
    pub original_module_path: ModulePath,
    pub binding_id: String,
    pub source_unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum ModuleDecompositionStrategy {
    Seed,
    Atomic,
    AtomicIncludingResidual,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecomposition {
    pub module_id: ModulePath,
    pub fragments: Vec<ModuleDecompositionFragment>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionFragment {
    /// Suggested output path. The command never edits the spec.
    pub suggested_module_path: ModulePath,
    pub source_unit_ids: Vec<String>,
    pub binding_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionSelectorAnchor {
    pub kind: ModuleDecompositionSelectorAnchorKind,
    pub module_path: ModulePath,
    pub source: ModuleDecompositionSelectorAnchorSource,
    pub referenced_by: String,
    pub source_unit_ids: Vec<String>,
    pub anchor_source_unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ModuleDecompositionSelectorAnchorSource {
    Member { name: String },
    AnonymousStatement { index: usize },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModuleDecompositionSelectorAnchorKind {
    IntrinsicAlias,
    SourceMatchReference,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionAtomicConflict {
    pub atomic_group_id: String,
    pub source_unit_ids: Vec<String>,
    pub candidate_modules: Vec<ModulePath>,
}
#[derive(Debug, Clone, Copy)]
pub struct ModuleDecompositionOptions {
    pub strategy: ModuleDecompositionStrategy,
    pub coarsening: ModuleDecompositionCoarseningMode,
    pub preserve_selector_anchors: bool,
    /// Include intact current named modules as one-fragment report rows.
    pub include_unchanged: bool,
    pub coarsen_atomic_conflicts: bool,
    pub rejoin_original_modules: bool,
    pub rejoin_pair_attempt_limit: Option<usize>,
    pub whole_home_pass_limit: Option<usize>,
}

pub fn decompose_modules_from_files_with_options(
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ModuleDecompositionReport> {
    let input = DependencyGraphInput::read(&options.dependency_graph_path)?;
    decompose_modules_from_graph_with_options(&input, options, decomposition, diagnostics)
}

pub fn decompose_modules_from_graph_with_options(
    input: &DependencyGraphInput,
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ModuleDecompositionReport> {
    let graph = input.report();
    let claims = active_claims_from_options(graph, options)?;
    let existing_module_paths = module_paths_from_options(graph, options)?;
    decompose_modules(
        input,
        &claims,
        existing_module_paths,
        options,
        decomposition,
        diagnostics,
    )
}

fn module_paths_from_options(
    graph: &DependencyGraphReport,
    options: &ProposalOptions,
) -> Result<BTreeSet<ModulePath>> {
    let roots = if let Some(config) = &options.tree_config {
        ModuleTreeScope::from_config(config, &graph.chunk_id)?
            .roots()
            .to_vec()
    } else {
        vec![PathBuf::new()]
    };
    let mut module_paths = BTreeSet::new();
    for root in roots {
        let modules_root = options.modules_root.join(root);
        if !modules_root.exists() {
            continue;
        }
        for file in collect_module_files(&modules_root)? {
            let raw_path = module_path_from_file(&file, &modules_root);
            module_paths.insert(
                ModulePath::parse(&raw_path, "")
                    .with_context(|| format!("module path from {}", file.display()))?,
            );
        }
    }
    Ok(module_paths)
}

fn decompose_modules(
    input: &DependencyGraphInput,
    active_claims: &BTreeMap<String, ModulePath>,
    existing_module_paths: BTreeSet<ModulePath>,
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ModuleDecompositionReport> {
    let graph = input.report();
    let strategy = decomposition.strategy;
    let source_unit_modules: Vec<ModulePath> = graph
        .nodes
        .iter()
        .map(|node| active_module_label(node, active_claims, graph))
        .collect::<Result<_>>()?;
    let residual_path = graph
        .quotient
        .nodes
        .iter()
        .find(|module| {
            matches!(
                module.kind,
                ModuleDestinationKind::Entry | ModuleDestinationKind::Unassigned
            )
        })
        .map(|module| module.path.clone())
        .context("dependency graph has no residual module for decomposition validation")?;
    let include_residual = strategy == ModuleDecompositionStrategy::AtomicIncludingResidual;

    let mut module_source_units: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for (source_unit_idx, node) in graph.nodes.iter().enumerate() {
        if include_residual || !graph.is_unassigned_or_entry(&node.destination) {
            module_source_units
                .entry(source_unit_modules[source_unit_idx].clone())
                .or_default()
                .push(source_unit_idx);
        }
    }
    let mut module_groups = match strategy {
        ModuleDecompositionStrategy::Seed => {
            let spec_modules: Vec<SpecModuleGroup> = module_source_units
                .iter()
                .map(|(module_id, source_unit_indices)| SpecModuleGroup {
                    module_id: module_id.to_string(),
                    source_unit_ids: source_unit_indices
                        .iter()
                        .map(|&source_unit_idx| graph.nodes[source_unit_idx].id.clone())
                        .collect(),
                })
                .collect();
            // Module size is not a decomposition constraint. Keep the gate's
            // structural and timing checks while allowing an existing large module
            // to be considered as a whole.
            let (quotient, _) = build_same_module_seed_quotient(
                input,
                &graph.atomic_graph.nodes,
                &spec_modules,
                usize::MAX,
            );
            module_source_units
                .iter()
                .map(|(module_id, source_unit_indices)| {
                    let mut fragments_by_class: BTreeMap<ClassId, Vec<usize>> = BTreeMap::new();
                    for &source_unit_idx in source_unit_indices {
                        let source_unit = quotient
                            .source_unit_index_of(&graph.nodes[source_unit_idx].id)
                            .with_context(|| {
                                format!(
                                    "source unit {} disappeared from seed quotient",
                                    graph.nodes[source_unit_idx].id
                                )
                            })?;
                        fragments_by_class
                            .entry(quotient.class_of(source_unit))
                            .or_default()
                            .push(source_unit_idx);
                    }
                    Ok((
                        module_id.clone(),
                        fragments_by_class
                            .into_values()
                            .map(|source_unit_indices| (source_unit_indices, false))
                            .collect::<Vec<_>>(),
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()?
        }
        ModuleDecompositionStrategy::Atomic => atomic_module_groups(
            input,
            &module_source_units,
            &source_unit_modules,
            &residual_path,
            false,
        )?,
        ModuleDecompositionStrategy::AtomicIncludingResidual => atomic_module_groups(
            input,
            &module_source_units,
            &source_unit_modules,
            &residual_path,
            true,
        )?,
    };

    let resolved_modules = if decomposition.preserve_selector_anchors {
        Some(resolve_decomposition_claims(graph, options, diagnostics)?)
    } else {
        None
    };
    let selector_anchor_relations = if let Some(resolved_modules) = &resolved_modules {
        let relations =
            resolve_selector_anchor_relations(graph, &source_unit_modules, resolved_modules)?;
        let included_modules = module_source_units.keys().cloned().collect();
        cohere_selector_anchor_groups(&mut module_groups, &relations, &included_modules)?;
        Some(
            relations
                .iter()
                .map(|relation| relation.report.clone())
                .collect(),
        )
    } else {
        None
    };

    let mut occupied_paths: BTreeSet<ModulePath> = source_unit_modules.iter().cloned().collect();
    occupied_paths.extend(existing_module_paths.iter().cloned());
    let mut candidate_source_unit_modules = source_unit_modules.clone();
    let mut refinements = Vec::new();
    let mut split_module_count = 0;
    for (module_id, groups) in module_groups {
        if groups.len() < 2 {
            if decomposition.include_unchanged
                && !groups.is_empty()
                && !(include_residual && module_id == residual_path)
            {
                let fragments = groups
                    .into_iter()
                    .map(|(source_unit_indices, _)| {
                        module_decomposition_fragment(graph, module_id.clone(), source_unit_indices)
                    })
                    .collect();
                refinements.push(ModuleDecomposition {
                    module_id,
                    fragments,
                });
            }
            continue;
        }

        let mut fragments = Vec::with_capacity(groups.len());
        for (source_unit_indices, preserve_module_path) in groups {
            let suggested_module_path = if preserve_module_path {
                module_id.clone()
            } else if include_residual && module_id == residual_path {
                next_residual_fragment_path(&mut occupied_paths)
            } else {
                next_fragment_path(&module_id, &mut occupied_paths)
            };
            for &source_unit_idx in &source_unit_indices {
                candidate_source_unit_modules[source_unit_idx] = suggested_module_path.clone();
            }
            fragments.push(module_decomposition_fragment(
                graph,
                suggested_module_path,
                source_unit_indices,
            ));
        }
        refinements.push(ModuleDecomposition {
            module_id,
            fragments,
        });
        split_module_count += 1;
    }

    let mut validation =
        validate_decomposition_candidate(input, &candidate_source_unit_modules, &residual_path)?;
    let mut coarsening = None;
    anyhow::ensure!(
        !decomposition.coarsen_atomic_conflicts
            || decomposition.coarsening == ModuleDecompositionCoarseningMode::MergeBlockingSccs,
        "coarsening atomic conflicts requires SCC coarsening"
    );
    if decomposition.coarsening == ModuleDecompositionCoarseningMode::MergeBlockingSccs {
        let mut occupied_paths: BTreeSet<ModulePath> =
            candidate_source_unit_modules.iter().cloned().collect();
        occupied_paths.extend(existing_module_paths.iter().cloned());
        let atomic_conflict_merges = if decomposition.coarsen_atomic_conflicts {
            let merges = coarsen_atomic_conflicts(
                &validation.atomic_group_conflicts,
                &residual_path,
                &mut occupied_paths,
                &mut candidate_source_unit_modules,
            )?;
            if !merges.is_empty() {
                validation = validate_decomposition_candidate(
                    input,
                    &candidate_source_unit_modules,
                    &residual_path,
                )?;
                anyhow::ensure!(
                    validation.atomic_group_conflicts.is_empty(),
                    "native atomic-conflict coarsening left structural atomic groups split"
                );
            }
            merges
        } else {
            Vec::new()
        };
        let mut rounds = Vec::new();
        let stop_reason = loop {
            if !validation.atomic_group_conflicts.is_empty() {
                break ModuleDecompositionCoarseningStopReason::AtomicGroupConflicts;
            }
            if !validation.cross_rebind_edges.is_empty() {
                break ModuleDecompositionCoarseningStopReason::CrossModuleRebinds;
            }
            if validation.cycles.is_empty() {
                break ModuleDecompositionCoarseningStopReason::Accepted;
            }

            let mut remaps = Vec::new();
            let mut blocking_sccs = Vec::new();
            let mut suggested_merge_paths = Vec::new();
            let mut consumed_paths = BTreeSet::new();
            for cycle in &validation.cycles {
                let cycle_paths: BTreeSet<ModulePath> = cycle.modules.iter().cloned().collect();
                let mut represented_paths: BTreeSet<ModulePath> = candidate_source_unit_modules
                    .iter()
                    .filter(|path| cycle_paths.contains(*path))
                    .cloned()
                    .collect();
                if cycle_paths.contains(&residual_path) {
                    represented_paths.insert(residual_path.clone());
                }
                if represented_paths.len() < 2
                    || represented_paths
                        .iter()
                        .any(|path| consumed_paths.contains(path))
                {
                    continue;
                }

                let target = if represented_paths.contains(&residual_path) {
                    residual_path.clone()
                } else {
                    let first_path = represented_paths
                        .iter()
                        .next()
                        .expect("at least two represented paths")
                        .clone();
                    next_fragment_path(&first_path, &mut occupied_paths)
                };
                consumed_paths.extend(represented_paths.iter().cloned());
                blocking_sccs.push(cycle.modules.clone());
                suggested_merge_paths.push(target.clone());
                remaps.push((represented_paths, target));
            }
            if remaps.is_empty() {
                break ModuleDecompositionCoarseningStopReason::NoProgress;
            }

            for (paths, target) in &remaps {
                for source_unit_path in &mut candidate_source_unit_modules {
                    if paths.contains(source_unit_path) {
                        *source_unit_path = target.clone();
                    }
                }
            }
            rounds.push(ModuleDecompositionCoarseningRound {
                blocking_sccs,
                suggested_merge_paths,
            });
            validation = validate_decomposition_candidate(
                input,
                &candidate_source_unit_modules,
                &residual_path,
            )?;
        };
        coarsening = Some(ModuleDecompositionCoarsening {
            stop_reason,
            atomic_conflict_merges,
            rounds,
            groups: coarsened_groups(graph, &source_unit_modules, &candidate_source_unit_modules),
            naming_collisions: None,
            original_home_rejoining: None,
        });
    }

    if decomposition.rejoin_original_modules {
        anyhow::ensure!(
            decomposition.coarsening == ModuleDecompositionCoarseningMode::MergeBlockingSccs,
            "rejoining original modules requires SCC coarsening"
        );
        let coarsening_report = coarsening
            .as_mut()
            .context("SCC coarsening report is missing")?;
        let accepted_baseline =
            coarsening_report.stop_reason == ModuleDecompositionCoarseningStopReason::Accepted;
        let home_rejoining = if accepted_baseline {
            rejoin_original_home_fragments(
                input,
                &source_unit_modules,
                &residual_path,
                &mut candidate_source_unit_modules,
                decomposition.rejoin_pair_attempt_limit,
                decomposition.whole_home_pass_limit,
            )?
        } else {
            summarize_home_rejoining(
                ModuleDecompositionHomeRejoiningStopReason::CandidateNotRealizable,
                &source_unit_modules,
                &candidate_source_unit_modules,
                HomeRejoiningCounters::default(),
                decomposition.rejoin_pair_attempt_limit,
                decomposition.whole_home_pass_limit,
                Vec::new(),
            )
        };
        validation = validate_decomposition_candidate(
            input,
            &candidate_source_unit_modules,
            &residual_path,
        )?;
        anyhow::ensure!(
            !accepted_baseline
                || (validation.atomic_group_conflicts.is_empty()
                    && validation.cycles.is_empty()
                    && validation.cross_rebind_edges.is_empty()),
            "shared gate rejected the assignment after original-home rejoining"
        );
        coarsening_report.groups =
            coarsened_groups(graph, &source_unit_modules, &candidate_source_unit_modules);
        coarsening_report.original_home_rejoining = Some(home_rejoining);
    }

    if let (Some(coarsening_report), Some(resolved_modules)) =
        (coarsening.as_mut(), resolved_modules.as_ref())
    {
        coarsening_report.naming_collisions = Some(coarsened_naming_collisions(
            graph,
            &source_unit_modules,
            &candidate_source_unit_modules,
            resolved_modules,
        )?);
    }

    let covered_source_unit_ids: BTreeSet<&str> =
        graph.nodes.iter().map(|node| node.id.as_str()).collect();
    let coverage_complete = covered_source_unit_ids.len() == graph.nodes.len()
        && candidate_source_unit_modules.len() == graph.nodes.len();
    let candidate_is_realizable = coverage_complete
        && validation.atomic_group_conflicts.is_empty()
        && validation.cycles.is_empty()
        && validation.cross_rebind_edges.is_empty();
    let original_modules: BTreeSet<ModulePath> = source_unit_modules.into_iter().collect();
    let unchanged_module_count = original_modules.len().saturating_sub(split_module_count);

    Ok(ModuleDecompositionReport {
        strategy,
        preserve_selector_anchors: decomposition.preserve_selector_anchors,
        selector_anchor_relations,
        candidate_is_realizable,
        coverage_complete,
        source_unit_count: graph.nodes.len(),
        assigned_source_unit_count: candidate_source_unit_modules.len(),
        unchanged_module_count,
        modules: refinements,
        atomic_group_conflicts: validation.atomic_group_conflicts,
        cycles: validation.cycles,
        cross_rebinds: validation.cross_rebinds,
        coarsening,
    })
}

fn module_decomposition_fragment(
    graph: &DependencyGraphReport,
    suggested_module_path: ModulePath,
    source_unit_indices: Vec<usize>,
) -> ModuleDecompositionFragment {
    let mut source_unit_ids = Vec::with_capacity(source_unit_indices.len());
    let mut binding_ids = BTreeSet::new();
    for source_unit_idx in source_unit_indices {
        let node = &graph.nodes[source_unit_idx];
        source_unit_ids.push(node.id.clone());
        binding_ids.extend(
            node.declared_bindings
                .iter()
                .map(|binding| binding.binding.to_string()),
        );
    }
    source_unit_ids.sort();
    ModuleDecompositionFragment {
        suggested_module_path,
        source_unit_ids,
        binding_ids: binding_ids.into_iter().collect(),
    }
}

fn is_false(value: &bool) -> bool {
    !value
}

struct SelectorAnchorConstraint {
    module_path: ModulePath,
    source_unit_indices: Vec<usize>,
    anchor_source_unit_indices: Vec<usize>,
    report: ModuleDecompositionSelectorAnchor,
}

struct ResolvedDecompositionModule {
    claims: ModuleClaims,
    resolved: ResolvedSourceClaims,
}

fn resolve_decomposition_claims(
    graph: &DependencyGraphReport,
    options: &ProposalOptions,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<Vec<ResolvedDecompositionModule>> {
    let source_root = options
        .source_root
        .as_deref()
        .context("--preserve-selector-anchors requires --source-root")?;
    let roots = if let Some(config) = &options.tree_config {
        ModuleTreeScope::from_config(config, &graph.chunk_id)?
            .roots()
            .to_vec()
    } else {
        vec![PathBuf::new()]
    };
    let mut modules = Vec::<(PathBuf, ModuleClaims)>::new();
    for root in roots {
        let root = options.modules_root.join(root);
        if !root.exists() {
            continue;
        }
        for path in collect_module_files(&root)? {
            let claims = module_claims(read_module_file(&path)?)?;
            modules.push((path, claims));
        }
    }
    let claim_sets = modules
        .iter()
        .map(|(path, claims)| SourceClaimSet {
            module_path: path,
            member_selectors: &claims.member_selectors,
            source_matches: &claims.source_matches,
            anonymous_selectors: &claims.anonymous_selectors,
        })
        .collect::<Vec<_>>();
    let resolved = resolve_source_claims(
        anonymous_resolution::SourceClaimsContext {
            graph,
            dependency_graph_path: &options.dependency_graph_path,
            modules_root: &options.modules_root,
            source_root: Some(source_root),
            source_context: options.source_context.as_ref(),
            diagnostics,
        },
        &claim_sets,
    )?;

    Ok(modules
        .into_iter()
        .zip(resolved)
        .map(|((_, claims), resolved)| ResolvedDecompositionModule { claims, resolved })
        .collect())
}

fn resolve_selector_anchor_relations(
    graph: &DependencyGraphReport,
    source_unit_modules: &[ModulePath],
    modules: &[ResolvedDecompositionModule],
) -> Result<Vec<SelectorAnchorConstraint>> {
    let mut source_unit_indices_by_binding = BTreeMap::<String, Vec<usize>>::new();
    for (source_unit_index, node) in graph.nodes.iter().enumerate() {
        for binding in &node.declared_bindings {
            let source_units = source_unit_indices_by_binding
                .entry(binding.binding.to_string())
                .or_default();
            if !source_units.contains(&source_unit_index) {
                source_units.push(source_unit_index);
            }
        }
    }

    let mut relations = Vec::new();
    for module in modules {
        let claims = &module.claims;
        let resolved = &module.resolved;
        let resolver = SelectorAnchorResolver {
            graph,
            source_unit_modules,
            source_unit_indices_by_binding: &source_unit_indices_by_binding,
            claims,
            resolved,
        };
        for (member, selector) in &claims.member_selectors {
            let MemberSelectorSpec::IntrinsicAlias(alias) = selector else {
                continue;
            };
            relations.push(resolver.member_constraint(
                member,
                &alias.referenced_by,
                ModuleDecompositionSelectorAnchorKind::IntrinsicAlias,
            )?);
        }
        for reference in &resolved.local_member_references {
            if reference.module_neutral_meaning != ModuleNeutralReferenceMeaning::Ambiguous {
                continue;
            }
            relations.push(resolver.source_match_constraint(reference)?);
        }
    }
    Ok(relations)
}

struct SelectorAnchorResolver<'a> {
    graph: &'a DependencyGraphReport,
    source_unit_modules: &'a [ModulePath],
    source_unit_indices_by_binding: &'a BTreeMap<String, Vec<usize>>,
    claims: &'a ModuleClaims,
    resolved: &'a ResolvedSourceClaims,
}

impl SelectorAnchorResolver<'_> {
    fn member_constraint(
        &self,
        member: &str,
        referenced_by: &str,
        kind: ModuleDecompositionSelectorAnchorKind,
    ) -> Result<SelectorAnchorConstraint> {
        self.constraint(
            ModuleDecompositionSelectorAnchorSource::Member {
                name: member.to_string(),
            },
            self.source_unit_indices_for_member(member)?,
            referenced_by,
            kind,
        )
    }

    fn source_match_constraint(
        &self,
        reference: &ResolvedLocalMemberReference,
    ) -> Result<SelectorAnchorConstraint> {
        let (source, source_unit_indices) = match &reference.source {
            ResolvedLocalMemberOrigin::Member(member) => (
                ModuleDecompositionSelectorAnchorSource::Member {
                    name: member.clone(),
                },
                self.source_unit_indices_for_member(member)?,
            ),
            ResolvedLocalMemberOrigin::AnonymousStatement { index, selector } => {
                let source_unit = self
                    .resolved
                    .anonymous_selector_source_units
                    .get(selector)
                    .with_context(|| {
                        format!("anonymous selector index {index} has no resolved source unit")
                    })?;
                anyhow::ensure!(
                    self.graph.nodes.get(source_unit.0).is_some(),
                    "anonymous selector index {index} resolved to missing source unit {}",
                    source_unit.0
                );
                (
                    ModuleDecompositionSelectorAnchorSource::AnonymousStatement { index: *index },
                    vec![source_unit.0],
                )
            }
        };
        self.constraint(
            source,
            source_unit_indices,
            &reference.referenced_by,
            ModuleDecompositionSelectorAnchorKind::SourceMatchReference,
        )
    }

    fn source_unit_indices_for_member(&self, member: &str) -> Result<Vec<usize>> {
        let member_binding = binding_for_member(self.claims, self.resolved, member)
            .with_context(|| format!("selector anchor member {member:?} did not resolve"))?;
        self.source_unit_indices_by_binding
            .get(member_binding)
            .with_context(|| {
                format!("selector anchor member {member:?} binding has no source unit")
            })
            .cloned()
    }

    fn constraint(
        &self,
        source: ModuleDecompositionSelectorAnchorSource,
        source_unit_indices: Vec<usize>,
        referenced_by: &str,
        kind: ModuleDecompositionSelectorAnchorKind,
    ) -> Result<SelectorAnchorConstraint> {
        let anchor_binding = binding_for_member(self.claims, self.resolved, referenced_by)
            .with_context(|| {
                format!("selector anchor reference {referenced_by:?} did not resolve")
            })?;
        let anchor_source_unit_indices = self
            .source_unit_indices_by_binding
            .get(anchor_binding)
            .with_context(|| {
                format!("selector anchor reference {referenced_by:?} binding has no source unit")
            })?
            .clone();
        let relation_modules = source_unit_indices
            .iter()
            .chain(&anchor_source_unit_indices)
            .map(|&source_unit_index| self.source_unit_modules[source_unit_index].clone())
            .collect::<BTreeSet<_>>();
        anyhow::ensure!(
            relation_modules.len() == 1,
            "selector anchor source {source:?} and reference {referenced_by:?} resolve to different modules: {}",
            relation_modules
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(", ")
        );
        let module_path = relation_modules
            .into_iter()
            .next()
            .context("selector anchor relation has no source unit module")?;
        Ok(SelectorAnchorConstraint {
            module_path: module_path.clone(),
            source_unit_indices: source_unit_indices.clone(),
            anchor_source_unit_indices: anchor_source_unit_indices.clone(),
            report: ModuleDecompositionSelectorAnchor {
                kind,
                module_path,
                source,
                referenced_by: referenced_by.to_string(),
                source_unit_ids: source_unit_ids(self.graph, &source_unit_indices),
                anchor_source_unit_ids: source_unit_ids(self.graph, &anchor_source_unit_indices),
            },
        })
    }
}

fn binding_for_member<'a>(
    claims: &'a ModuleClaims,
    resolved: &'a ResolvedSourceClaims,
    name: &str,
) -> Option<&'a str> {
    resolved
        .member_bindings
        .get(name)
        .map(String::as_str)
        .or_else(|| {
            claims
                .member_selectors
                .iter()
                .find(|(member, _)| member == name)
                .and_then(|(_, selector)| match selector {
                    MemberSelectorSpec::Binding(binding) => Some(binding.name.as_str()),
                    _ => None,
                })
        })
}

fn source_unit_ids(graph: &DependencyGraphReport, source_unit_indices: &[usize]) -> Vec<String> {
    let mut source_unit_ids = source_unit_indices
        .iter()
        .map(|&source_unit_index| graph.nodes[source_unit_index].id.clone())
        .collect::<Vec<_>>();
    source_unit_ids.sort();
    source_unit_ids.dedup();
    source_unit_ids
}

fn cohere_selector_anchor_groups(
    groups_by_module: &mut AtomicModuleGroups,
    relations: &[SelectorAnchorConstraint],
    included_modules: &BTreeSet<ModulePath>,
) -> Result<()> {
    for (module_path, groups) in groups_by_module.iter_mut() {
        if !included_modules.contains(module_path) {
            continue;
        }
        let relevant = relations
            .iter()
            .filter(|relation| relation.module_path == *module_path)
            .collect::<Vec<_>>();
        if relevant.is_empty() {
            continue;
        }
        let mut group_by_source_unit = BTreeMap::new();
        for (group_index, (source_unit_indices, _)) in groups.iter().enumerate() {
            for &source_unit_index in source_unit_indices {
                anyhow::ensure!(
                    group_by_source_unit
                        .insert(source_unit_index, group_index)
                        .is_none(),
                    "source unit {} appears in more than one decomposition group in module {module_path}",
                    source_unit_index
                );
            }
        }
        let mut parents = (0..groups.len()).collect::<Vec<_>>();
        for relation in relevant {
            let source_groups = relation
                .source_unit_indices
                .iter()
                .map(|source_unit_index| {
                    group_by_source_unit.get(source_unit_index).copied().with_context(|| {
                        format!(
                            "selector anchor source {:?} source unit {} is absent from decomposition groups",
                            relation.report.source, source_unit_index
                        )
                    })
                })
                .collect::<Result<BTreeSet<_>>>()?;
            let anchor_groups = relation
                .anchor_source_unit_indices
                .iter()
                .map(|source_unit_index| {
                    group_by_source_unit.get(source_unit_index).copied().with_context(|| {
                        format!(
                            "selector anchor reference {} source unit {} is absent from decomposition groups",
                            relation.report.referenced_by, source_unit_index
                        )
                    })
                })
                .collect::<Result<BTreeSet<_>>>()?;
            for &source_group in &source_groups {
                for &anchor_group in &anchor_groups {
                    union_groups(&mut parents, source_group, anchor_group);
                }
            }
        }

        let mut merged = BTreeMap::<usize, (Vec<usize>, bool)>::new();
        for (group_index, (source_unit_indices, preserve_module_path)) in
            groups.drain(..).enumerate()
        {
            let root = find_group_root(&mut parents, group_index);
            let (merged_source_units, merged_preserve_path) = merged.entry(root).or_default();
            merged_source_units.extend(source_unit_indices);
            *merged_preserve_path |= preserve_module_path;
        }
        *groups = merged
            .into_values()
            .map(|(mut source_unit_indices, preserve_module_path)| {
                source_unit_indices.sort_unstable();
                (source_unit_indices, preserve_module_path)
            })
            .collect();
    }
    Ok(())
}

fn find_group_root(parents: &mut [usize], group: usize) -> usize {
    if parents[group] != group {
        parents[group] = find_group_root(parents, parents[group]);
    }
    parents[group]
}

fn union_groups(parents: &mut [usize], left: usize, right: usize) {
    let left_root = find_group_root(parents, left);
    let right_root = find_group_root(parents, right);
    if left_root != right_root {
        let (lower, upper) = if left_root < right_root {
            (left_root, right_root)
        } else {
            (right_root, left_root)
        };
        parents[upper] = lower;
    }
}

fn coarsen_atomic_conflicts(
    conflicts: &[ModuleDecompositionAtomicConflict],
    residual_path: &ModulePath,
    occupied_paths: &mut BTreeSet<ModulePath>,
    candidate_source_unit_modules: &mut [ModulePath],
) -> Result<Vec<ModuleDecompositionAtomicConflictMerge>> {
    if conflicts.is_empty() {
        return Ok(Vec::new());
    }
    let paths: Vec<ModulePath> = candidate_source_unit_modules
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let path_indices: BTreeMap<ModulePath, usize> = paths
        .iter()
        .enumerate()
        .map(|(idx, path)| (path.clone(), idx))
        .collect();
    let mut parents: Vec<usize> = (0..paths.len()).collect();
    for conflict in conflicts {
        let mut indices = conflict.candidate_modules.iter().map(|path| {
            path_indices.get(path).copied().with_context(|| {
                format!(
                    "atomic group {} references absent candidate path {path}",
                    conflict.atomic_group_id
                )
            })
        });
        let first = indices
            .next()
            .context("reported atomic conflict has no candidate paths")??;
        for index in indices {
            union_groups(&mut parents, first, index?);
        }
    }

    let mut paths_by_root = BTreeMap::<usize, Vec<ModulePath>>::new();
    for (idx, path) in paths.into_iter().enumerate() {
        let root = find_group_root(&mut parents, idx);
        paths_by_root.entry(root).or_default().push(path);
    }
    let mut atomic_group_ids_by_root = BTreeMap::<usize, BTreeSet<String>>::new();
    for conflict in conflicts {
        let first_path = conflict.candidate_modules.first().expect("checked above");
        let root = find_group_root(&mut parents, path_indices[first_path]);
        atomic_group_ids_by_root
            .entry(root)
            .or_default()
            .insert(conflict.atomic_group_id.clone());
    }

    let mut remaps = BTreeMap::<ModulePath, ModulePath>::new();
    let mut merges = Vec::new();
    for (root, merged_candidate_paths) in paths_by_root {
        if merged_candidate_paths.len() < 2 {
            continue;
        }
        let suggested_module_path = if merged_candidate_paths.contains(residual_path) {
            residual_path.clone()
        } else {
            next_fragment_path(&merged_candidate_paths[0], occupied_paths)
        };
        for path in &merged_candidate_paths {
            remaps.insert(path.clone(), suggested_module_path.clone());
        }
        merges.push(ModuleDecompositionAtomicConflictMerge {
            atomic_group_ids: atomic_group_ids_by_root
                .remove(&root)
                .context("coarsened component lost its reported atomic groups")?
                .into_iter()
                .collect(),
            merged_candidate_paths,
            suggested_module_path,
        });
    }
    for path in candidate_source_unit_modules {
        if let Some(target) = remaps.get(path) {
            *path = target.clone();
        }
    }
    Ok(merges)
}

struct DecompositionValidation {
    atomic_group_conflicts: Vec<ModuleDecompositionAtomicConflict>,
    cycles: Vec<gate::CycleReport>,
    cross_rebinds: Vec<String>,
    cross_rebind_edges: Vec<gate::CrossRebindEdge>,
}

fn validate_decomposition_candidate(
    input: &DependencyGraphInput,
    candidate_source_unit_modules: &[ModulePath],
    residual_path: &ModulePath,
) -> Result<DecompositionValidation> {
    let graph = input.report();
    let mut atomic_group_conflicts = Vec::new();
    for atomic_group in &graph.atomic_graph.nodes {
        let mut candidate_modules = BTreeSet::new();
        for source_unit_id in &atomic_group.source_unit_ids {
            let source_unit_idx = input
                .source_unit_id(source_unit_id)
                .map(|source_unit| source_unit.0)
                .with_context(|| {
                    format!(
                        "atomic group {} references unknown source unit {source_unit_id}",
                        atomic_group.id
                    )
                })?;
            candidate_modules.insert(candidate_source_unit_modules[source_unit_idx].clone());
        }
        if candidate_modules.len() > 1 {
            atomic_group_conflicts.push(ModuleDecompositionAtomicConflict {
                atomic_group_id: atomic_group.id.clone(),
                source_unit_ids: atomic_group.source_unit_ids.clone(),
                candidate_modules: candidate_modules.into_iter().collect(),
            });
        }
    }

    let (assignments, paths_by_module_id) =
        candidate_module_assignments(candidate_source_unit_modules, residual_path)?;
    let assignment = ModuleAssignment::from_assignments(
        assignments,
        analysis::EntryDestination::from_module_id(ModuleId(0)),
    );
    let gate_validation =
        gate::validate_assignment_with_verdict(input.graph(), &assignment, &|module_id| {
            paths_by_module_id
                .get(&module_id)
                .cloned()
                .unwrap_or_else(|| residual_path.clone())
        });
    Ok(DecompositionValidation {
        atomic_group_conflicts,
        cycles: gate_validation.report.cycles,
        cross_rebinds: gate_validation.report.cross_rebinds,
        cross_rebind_edges: gate_validation.verdict.cross_rebinds,
    })
}

fn candidate_module_assignments(
    candidate_source_unit_modules: &[ModulePath],
    residual_path: &ModulePath,
) -> Result<(Vec<ModuleId>, BTreeMap<ModuleId, ModulePath>)> {
    let mut paths: BTreeSet<ModulePath> = candidate_source_unit_modules.iter().cloned().collect();
    paths.insert(residual_path.clone());
    let mut module_ids = BTreeMap::from([(residual_path.clone(), ModuleId(0))]);
    let mut paths_by_module_id = BTreeMap::from([(ModuleId(0), residual_path.clone())]);
    let mut next_module_idx = 1;
    for path in paths {
        if &path == residual_path {
            continue;
        }
        let module_id = ModuleId(next_module_idx);
        next_module_idx += 1;
        module_ids.insert(path.clone(), module_id);
        paths_by_module_id.insert(module_id, path);
    }
    let assignments = candidate_source_unit_modules
        .iter()
        .map(|path| {
            module_ids
                .get(path)
                .copied()
                .with_context(|| format!("candidate module {path} has no gate identity"))
        })
        .collect::<Result<Vec<_>>>()?;
    Ok((assignments, paths_by_module_id))
}

fn rejoin_original_home_fragments(
    input: &DependencyGraphInput,
    original_source_unit_modules: &[ModulePath],
    residual_path: &ModulePath,
    candidate_source_unit_modules: &mut [ModulePath],
    pair_attempt_limit: Option<usize>,
    whole_home_pass_limit: Option<usize>,
) -> Result<ModuleDecompositionHomeRejoining> {
    let mut owners_by_candidate = BTreeMap::<ModulePath, Vec<usize>>::new();
    let mut home_source_unit_counts = BTreeMap::<ModulePath, usize>::new();
    for (source_unit_idx, (candidate, home)) in candidate_source_unit_modules
        .iter()
        .zip(original_source_unit_modules)
        .enumerate()
    {
        owners_by_candidate
            .entry(candidate.clone())
            .or_default()
            .push(source_unit_idx);
        *home_source_unit_counts.entry(home.clone()).or_default() += 1;
    }
    // A coarsened group spanning multiple original homes is already a gate
    // accepted indivisible class. Never contract it with a pure home fragment.
    let mut pure_home_representatives = BTreeMap::<ModulePath, Vec<SourceUnitIndex>>::new();
    for source_units in owners_by_candidate.values() {
        let home = &original_source_unit_modules[source_units[0]];
        if source_units
            .iter()
            .all(|&source_unit_idx| &original_source_unit_modules[source_unit_idx] == home)
        {
            pure_home_representatives
                .entry(home.clone())
                .or_default()
                .push(SourceUnitIndex(source_units[0]));
        }
    }

    if whole_home_pass_limit == Some(0) {
        let split_pure_home_remains = pure_home_representatives
            .values()
            .any(|representatives| representatives.len() > 1);
        return Ok(summarize_home_rejoining(
            if split_pure_home_remains {
                ModuleDecompositionHomeRejoiningStopReason::WholeHomePassLimitReached
            } else {
                ModuleDecompositionHomeRejoiningStopReason::Converged
            },
            original_source_unit_modules,
            candidate_source_unit_modules,
            HomeRejoiningCounters::default(),
            pair_attempt_limit,
            whole_home_pass_limit,
            Vec::new(),
        ));
    }

    let (assignments, _) =
        candidate_module_assignments(candidate_source_unit_modules, residual_path)?;
    // Decomposition has no size ceiling: an original home can be larger than
    // the proposer's default cap. The shared incremental gate still checks
    // every contraction against the complete candidate assignment.
    let mut quotient = QuotientGraph::from_graph_with_realizable_assignment(
        input,
        usize::MAX,
        &assignments,
        ModuleId(0),
    )?;

    let mut attempted = 0;
    let mut accepted = 0;
    let mut whole_home_pass_count = 0;
    let mut whole_home_pass_limit_reached = false;
    let mut pair_attempts_by_home = BTreeMap::<ModulePath, usize>::new();
    let mut limited_homes = BTreeSet::<ModulePath>::new();
    // Stable path and source_unit order makes the result deterministic. Exhaust each
    // home before moving to the next; another home's contraction may make an
    // earlier rejected pair admissible, so repeat whole-home passes only when
    // some contraction was accepted during the previous pass.
    loop {
        if whole_home_pass_limit.is_some_and(|limit| whole_home_pass_count >= limit) {
            whole_home_pass_limit_reached =
                has_split_pure_home_fragments(&quotient, &pure_home_representatives);
            break;
        }
        whole_home_pass_count += 1;
        let mut pass_progress = false;
        for (home, representatives) in &pure_home_representatives {
            let mut attempted_whole_home = false;
            loop {
                let classes: Vec<ClassId> = representatives
                    .iter()
                    .map(|&source_unit| quotient.class_of(source_unit))
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect();
                // A rejected whole-home target is unchanged by this home's
                // pairwise contractions. Retry only on another whole-home
                // pass, after other homes may have changed the gate.
                if !attempted_whole_home
                    && (classes.len() > 2 || (pair_attempt_limit.is_some() && classes.len() == 2))
                {
                    attempted_whole_home = true;
                    attempted += 1;
                    let whole_home_accepted = if classes.len() == 2 {
                        quotient.contract_if_preserving(classes[0], classes[1])
                    } else {
                        quotient.contract_group_if_preserving(&classes)
                    };
                    if whole_home_accepted {
                        accepted += classes.len() - 1;
                        pass_progress = true;
                        break;
                    }
                    // With exactly two classes, this was the only possible pair.
                    if classes.len() == 2 {
                        break;
                    }
                }
                let mut home_progress = false;
                'pairs: for (left_idx, &left) in classes.iter().enumerate() {
                    for &right in classes.iter().skip(left_idx + 1) {
                        if let Some(limit) = pair_attempt_limit {
                            let pair_attempts =
                                pair_attempts_by_home.entry(home.clone()).or_default();
                            if *pair_attempts >= limit {
                                limited_homes.insert(home.clone());
                                break 'pairs;
                            }
                            *pair_attempts += 1;
                        }
                        attempted += 1;
                        if quotient.contract_if_preserving(left, right) {
                            accepted += 1;
                            home_progress = true;
                            pass_progress = true;
                            break 'pairs;
                        }
                    }
                }
                if !home_progress {
                    break;
                }
            }
        }
        if !pass_progress {
            break;
        }
    }

    // A later whole-home pass can restore a home even after its fallback
    // budget was exhausted. A two-class home also gets its sole pair checked
    // as a whole-home move, leaving no untried pairs to report.
    limited_homes.retain(|home| {
        pure_home_representatives
            .get(home)
            .is_some_and(|representatives| {
                representatives
                    .iter()
                    .map(|&source_unit| quotient.class_of(source_unit))
                    .collect::<BTreeSet<_>>()
                    .len()
                    > 2
            })
    });

    let mut final_paths = BTreeMap::<ClassId, ModulePath>::new();
    let mut used_paths = BTreeSet::new();
    for class in quotient.iter_classes() {
        let members: Vec<usize> = quotient
            .class_members(class)
            .map(|source_unit| source_unit.0)
            .collect();
        let homes: BTreeSet<&ModulePath> = members
            .iter()
            .map(|&source_unit_idx| &original_source_unit_modules[source_unit_idx])
            .collect();
        let candidate_paths: BTreeSet<&ModulePath> = members
            .iter()
            .map(|&source_unit_idx| &candidate_source_unit_modules[source_unit_idx])
            .collect();
        let restored_home = homes.iter().next().filter(|home| {
            homes.len() == 1 && home_source_unit_counts.get(*home) == Some(&members.len())
        });
        let path = if let Some(home) = restored_home {
            (**home).clone()
        } else if candidate_paths.iter().any(|path| *path == residual_path) {
            residual_path.clone()
        } else {
            (**candidate_paths
                .iter()
                .next()
                .expect("nonempty quotient class"))
            .clone()
        };
        anyhow::ensure!(
            used_paths.insert(path.clone()),
            "original-home rejoining produced duplicate candidate path {path}"
        );
        final_paths.insert(class, path);
    }
    for (source_unit_idx, path) in candidate_source_unit_modules.iter_mut().enumerate() {
        *path = final_paths
            .get(&quotient.class_of(SourceUnitIndex(source_unit_idx)))
            .expect("every source unit belongs to a live quotient class")
            .clone();
    }
    let stop_reason = if whole_home_pass_limit_reached {
        ModuleDecompositionHomeRejoiningStopReason::WholeHomePassLimitReached
    } else if limited_homes.is_empty() {
        ModuleDecompositionHomeRejoiningStopReason::Converged
    } else {
        ModuleDecompositionHomeRejoiningStopReason::PairAttemptLimitReached
    };
    Ok(summarize_home_rejoining(
        stop_reason,
        original_source_unit_modules,
        candidate_source_unit_modules,
        HomeRejoiningCounters {
            attempted_contractions: attempted,
            accepted_contractions: accepted,
            whole_home_pass_count,
        },
        pair_attempt_limit,
        whole_home_pass_limit,
        limited_homes.into_iter().collect(),
    ))
}

fn has_split_pure_home_fragments(
    quotient: &QuotientGraph,
    pure_home_representatives: &BTreeMap<ModulePath, Vec<SourceUnitIndex>>,
) -> bool {
    pure_home_representatives.values().any(|representatives| {
        representatives
            .iter()
            .map(|&source_unit| quotient.class_of(source_unit))
            .collect::<BTreeSet<_>>()
            .len()
            > 1
    })
}

#[derive(Debug, Clone, Copy, Default)]
struct HomeRejoiningCounters {
    attempted_contractions: usize,
    accepted_contractions: usize,
    whole_home_pass_count: usize,
}

fn summarize_home_rejoining(
    stop_reason: ModuleDecompositionHomeRejoiningStopReason,
    original_source_unit_modules: &[ModulePath],
    candidate_source_unit_modules: &[ModulePath],
    counters: HomeRejoiningCounters,
    pair_attempt_limit: Option<usize>,
    whole_home_pass_limit: Option<usize>,
    budget_limited_home_paths: Vec<ModulePath>,
) -> ModuleDecompositionHomeRejoining {
    let mut groups = BTreeMap::<&ModulePath, (usize, BTreeSet<&ModulePath>)>::new();
    let mut homes = BTreeMap::<&ModulePath, BTreeSet<&ModulePath>>::new();
    for (home, candidate) in original_source_unit_modules
        .iter()
        .zip(candidate_source_unit_modules)
    {
        let group = groups.entry(candidate).or_default();
        group.0 += 1;
        group.1.insert(home);
        homes.entry(home).or_default().insert(candidate);
    }
    let (fully_restored_module_paths, unrestored_module_paths): (Vec<_>, Vec<_>) =
        homes.into_iter().partition(|(home, candidates)| {
            candidates.len() == 1
                && candidates.contains(home)
                && groups.get(home).is_some_and(|group| group.1.len() == 1)
        });
    let fully_restored_module_paths: Vec<ModulePath> = fully_restored_module_paths
        .into_iter()
        .map(|(home, _)| home.clone())
        .collect();
    let unrestored_module_paths: Vec<ModulePath> = unrestored_module_paths
        .into_iter()
        .map(|(home, _)| home.clone())
        .collect();
    ModuleDecompositionHomeRejoining {
        stop_reason,
        attempted_contractions: counters.attempted_contractions,
        accepted_contractions: counters.accepted_contractions,
        pair_attempt_limit,
        whole_home_pass_count: counters.whole_home_pass_count,
        whole_home_pass_limit,
        budget_limited_home_count: budget_limited_home_paths.len(),
        budget_limited_home_paths,
        original_module_count: fully_restored_module_paths.len() + unrestored_module_paths.len(),
        fully_restored_module_count: fully_restored_module_paths.len(),
        fully_restored_module_paths,
        unrestored_module_paths,
        final_group_count: groups.len(),
        largest_group_source_unit_count: groups.values().map(|group| group.0).max().unwrap_or(0),
        largest_group_origin_module_count: groups
            .values()
            .map(|group| group.1.len())
            .max()
            .unwrap_or(0),
    }
}

fn coarsened_groups(
    graph: &DependencyGraphReport,
    original_source_unit_modules: &[ModulePath],
    candidate_source_unit_modules: &[ModulePath],
) -> Vec<ModuleDecompositionCoarsenedGroup> {
    let mut by_module: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for (source_unit_idx, path) in candidate_source_unit_modules.iter().enumerate() {
        by_module
            .entry(path.clone())
            .or_default()
            .push(source_unit_idx);
    }
    by_module
        .into_iter()
        .map(|(suggested_module_path, source_unit_indices)| {
            let mut origin_module_paths = BTreeSet::new();
            let mut source_unit_ids = Vec::new();
            let mut binding_ids = BTreeSet::new();
            for source_unit_idx in source_unit_indices {
                origin_module_paths.insert(original_source_unit_modules[source_unit_idx].clone());
                let source_unit = &graph.nodes[source_unit_idx];
                source_unit_ids.push(source_unit.id.clone());
                binding_ids.extend(
                    source_unit
                        .declared_bindings
                        .iter()
                        .map(|binding| binding.binding.to_string()),
                );
            }
            source_unit_ids.sort();
            ModuleDecompositionCoarsenedGroup {
                suggested_module_path,
                origin_module_paths: origin_module_paths.into_iter().collect(),
                source_unit_ids,
                binding_ids: binding_ids.into_iter().collect(),
            }
        })
        .collect()
}

fn coarsened_naming_collisions(
    graph: &DependencyGraphReport,
    original_source_unit_modules: &[ModulePath],
    candidate_source_unit_modules: &[ModulePath],
    modules: &[ResolvedDecompositionModule],
) -> Result<Vec<ModuleDecompositionNamingCollision>> {
    let mut owners_by_binding = BTreeMap::<String, Vec<usize>>::new();
    for (source_unit_idx, source_unit) in graph.nodes.iter().enumerate() {
        for binding in &source_unit.declared_bindings {
            owners_by_binding
                .entry(binding.binding.to_string())
                .or_default()
                .push(source_unit_idx);
        }
    }

    // Enumerate names from the canonical authored claims, then use the same
    // resolved binding lookup as selector-anchor cohesion. A claim can have
    // multiple source_unit sites, but it is one prospective export per destination.
    let mut by_name =
        BTreeMap::<(ModulePath, String), BTreeMap<(ModulePath, String), BTreeSet<String>>>::new();
    for module in modules {
        let readable_names = module
            .claims
            .member_selectors
            .iter()
            .map(|(name, _)| name.clone())
            .chain(module.claims.source_matches.iter().flat_map(|claim| {
                claim
                    .bindings
                    .iter()
                    .map(|binding| binding.name().to_string())
            }))
            .collect::<BTreeSet<_>>();
        for readable_name in readable_names {
            let binding_id =
                binding_for_member(&module.claims, &module.resolved, &readable_name)
                    .with_context(|| format!("member {readable_name:?} has no resolved binding"))?;
            let source_unit_indices = owners_by_binding.get(binding_id).with_context(|| {
                format!(
                    "resolved member {readable_name:?} binding {binding_id:?} has no graph source unit"
                )
            })?;
            for &source_unit_idx in source_unit_indices {
                by_name
                    .entry((
                        candidate_source_unit_modules[source_unit_idx].clone(),
                        readable_name.clone(),
                    ))
                    .or_default()
                    .entry((
                        original_source_unit_modules[source_unit_idx].clone(),
                        binding_id.to_string(),
                    ))
                    .or_default()
                    .insert(graph.nodes[source_unit_idx].id.clone());
            }
        }
    }

    Ok(by_name
        .into_iter()
        .filter_map(|((suggested_module_path, readable_name), claims)| {
            let homes = claims.keys().map(|(path, _)| path).collect::<BTreeSet<_>>();
            let bindings = claims
                .keys()
                .map(|(_, binding)| binding)
                .collect::<BTreeSet<_>>();
            (homes.len() > 1 && bindings.len() > 1).then(|| ModuleDecompositionNamingCollision {
                suggested_module_path,
                readable_name,
                claims: claims
                    .into_iter()
                    .map(|((original_module_path, binding_id), source_unit_ids)| {
                        ModuleDecompositionNamedClaim {
                            original_module_path,
                            binding_id,
                            source_unit_ids: source_unit_ids.into_iter().collect(),
                        }
                    })
                    .collect(),
            })
        })
        .collect())
}

type AtomicModuleGroups = BTreeMap<ModulePath, Vec<(Vec<usize>, bool)>>;

fn atomic_module_groups(
    input: &DependencyGraphInput,
    module_source_units: &BTreeMap<ModulePath, Vec<usize>>,
    source_unit_modules: &[ModulePath],
    residual_path: &ModulePath,
    include_residual: bool,
) -> Result<AtomicModuleGroups> {
    let graph = input.report();
    let mut atomic_group_by_source_unit = vec![None; graph.nodes.len()];
    for (atomic_group_idx, atomic_group) in graph.atomic_graph.nodes.iter().enumerate() {
        for source_unit_id in &atomic_group.source_unit_ids {
            let source_unit_idx = input
                .source_unit_id(source_unit_id)
                .map(|source_unit| source_unit.0)
                .with_context(|| {
                    format!(
                        "atomic group {} references unknown source unit {source_unit_id}",
                        atomic_group.id
                    )
                })?;
            if let Some(previous_atomic_group) =
                atomic_group_by_source_unit[source_unit_idx].replace(atomic_group_idx)
            {
                anyhow::bail!(
                    "source unit {source_unit_id} appears in atomic groups {previous_atomic_group} and {atomic_group_idx}"
                );
            }
        }
    }
    if let Some(source_unit_idx) = atomic_group_by_source_unit.iter().position(Option::is_none) {
        anyhow::bail!(
            "source unit {} is absent from the atomic graph",
            graph.nodes[source_unit_idx].id
        );
    }

    let mut groups_by_module: BTreeMap<ModulePath, Vec<(Vec<usize>, bool)>> = BTreeMap::new();
    let mut preserved_by_module: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for atomic_group in &graph.atomic_graph.nodes {
        let source_unit_indices = atomic_group
            .source_unit_ids
            .iter()
            .map(|source_unit_id| {
                input
                    .source_unit_id(source_unit_id)
                    .map(|source_unit| source_unit.0)
                    .with_context(|| {
                        format!(
                            "atomic group {} references unknown source unit {source_unit_id}",
                            atomic_group.id
                        )
                    })
            })
            .collect::<Result<Vec<_>>>()?;
        let modules = source_unit_indices
            .iter()
            .map(|&source_unit_idx| source_unit_modules[source_unit_idx].clone())
            .collect::<BTreeSet<_>>();
        if modules.len() > 1 {
            for source_unit_idx in source_unit_indices {
                if module_source_units.contains_key(&source_unit_modules[source_unit_idx]) {
                    preserved_by_module
                        .entry(source_unit_modules[source_unit_idx].clone())
                        .or_default()
                        .push(source_unit_idx);
                }
            }
        } else if let Some(module_id) = modules.into_iter().next()
            && module_source_units.contains_key(&module_id)
        {
            let keep_in_entry = include_residual
                && module_id == *residual_path
                && source_unit_indices.iter().any(|&source_unit_idx| {
                    matches!(
                        graph.nodes[source_unit_idx].statement_kind,
                        StatementKind::Import | StatementKind::Export
                    )
                });
            if keep_in_entry {
                preserved_by_module
                    .entry(module_id)
                    .or_default()
                    .extend(source_unit_indices);
            } else {
                groups_by_module
                    .entry(module_id)
                    .or_default()
                    .push((source_unit_indices, false));
            }
        }
    }
    for (module_id, mut source_unit_indices) in preserved_by_module {
        source_unit_indices.sort_unstable();
        source_unit_indices.dedup();
        groups_by_module
            .entry(module_id)
            .or_default()
            .push((source_unit_indices, true));
    }

    for (module_id, expected_source_unit_indices) in module_source_units {
        let mut grouped_source_unit_indices = groups_by_module
            .get(module_id)
            .into_iter()
            .flatten()
            .flat_map(|(indices, _)| indices.iter().copied())
            .collect::<Vec<_>>();
        grouped_source_unit_indices.sort_unstable();
        let mut expected_source_unit_indices = expected_source_unit_indices.clone();
        expected_source_unit_indices.sort_unstable();
        anyhow::ensure!(
            grouped_source_unit_indices == expected_source_unit_indices,
            "atomic groups do not cover source units in module {module_id} exactly once"
        );
    }
    Ok(groups_by_module)
}

fn next_fragment_path(
    module_id: &ModulePath,
    occupied_paths: &mut BTreeSet<ModulePath>,
) -> ModulePath {
    for suffix in 1usize.. {
        let raw = format!("{module_id}/part-{suffix}");
        let candidate = ModulePath::parse(&raw, "")
            .expect("a canonical module path followed by a simple suffix is valid");
        if occupied_paths.insert(candidate.clone()) {
            return candidate;
        }
    }
    unreachable!("an unbounded suffix always has an unused module path")
}

fn next_residual_fragment_path(occupied_paths: &mut BTreeSet<ModulePath>) -> ModulePath {
    for suffix in 1usize.. {
        let raw = format!("decomposition/residual/part-{suffix}");
        let candidate =
            ModulePath::parse(&raw, "").expect("a fixed path followed by a simple suffix is valid");
        if occupied_paths.insert(candidate.clone()) {
            return candidate;
        }
    }
    unreachable!("an unbounded suffix always has an unused module path")
}
