//! CLI-facing peel proposer over the serialized owner + atomic DAG report.
//!
//! `debundle run` emits stable graph facts only. This crate computes
//! heuristic peel proposals from `OwnerGraphReport.atomic_graph` on demand and
//! annotates them with spec-tree context (active claims) and cell-graph
//! metrics:
//!
//! - `edges_to_active_modules` / `active_modules_referenced`:
//!   outgoing constraining edges from each cell to active-claimed
//!   binding modules (safe references — active modules materialize
//!   before residual_entry).
//! - `internal_edges`, `edges_to_other_residual_cells`,
//!   `other_residual_cells_referenced`: cell-graph relationship
//!   counts derived from the partition the analyzer chose.
//!
//! Diagnostics come through separately. A diagnostic is not a module
//! assignment the author can land as-is.
//!
//! ## Renderer over `QuotientGraph`
//!
//! The quotient-contraction proposer (documented in
//! `devinfra/js/debundle/docs/peel_proposer.md`) builds every proposal
//! through a single, gated contraction protocol. The proposal
//! pipeline is:
//!
//!   1. `build_seed_quotient` — atomic-unit + spec-module +
//!      atomic-DAG-reachability contractions, each gated by
//!      `merge_preserves_invariants`. Cycle-rejected contractions
//!      surface as `SeedContractionRejected::AtomicReachability`
//!      diagnostics instead of silently forming cyclic cells.
//!   2. `greedy_merge_to_convergence` — extends the quotient with
//!      orphan-into-module and module↔module merges where the
//!      gate permits.
//!   3. `emit_proposals` — walks the surviving classes and
//!      materializes each as a `ModuleProposal`.
//!
//! The quotient is the single source of truth for "which owners are
//! in which proposed class."

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{
    LineRange, ModuleId, OwnerGraph, OwnerGraphNodeReport, OwnerGraphReport, Partition,
    StatementKind,
};
use anonymous_resolution::{
    ResolvedSourceClaims, SourceClaimSet, addressable_anonymous_statement_owner_ids,
    resolve_source_claims,
};
use spec::{MemberSelectorSpec, ModulePath};
use spec_modules::{
    ModuleClaims, collect_module_files, load_active_claims, module_claims, module_path_from_file,
    read_module_file,
};
use spec_tree::ModuleTreeScope;

use crate::quotient::{
    ClassId, OwnerIdx, QuotientGraph, SeedContractionRejected, SpecModuleGroup,
    build_same_module_seed_quotient, build_seed_quotient, greedy_merge_to_convergence,
};

/// Default `--size-cap-lines` for every CLI verb that runs the
/// proposer. One named constant so the planner verbs and the
/// internal rebuild paths can't drift apart.
pub const DEFAULT_SIZE_CAP_LINES: usize = 10_000;

#[derive(Debug, Clone)]
pub struct ProposalOptions {
    pub owner_graph_path: PathBuf,
    pub modules_root: PathBuf,
    /// Optional tree config that scopes active claims to the graph's chunk.
    pub tree_config: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    /// Hard ceiling (in summed source-line counts) per emitted
    /// proposal. Frontiers exceeding the cap appear as diagnostics.
    pub size_cap_lines: usize,
}

/// Read-only candidate formed by splitting existing modules into the
/// same-module classes that survived the gated seed pass.
#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionReport {
    pub strategy: ModuleDecompositionStrategy,
    /// True only when the candidate also keeps each resolved
    /// `intrinsic_alias.referenced_by` owner with its local member owner.
    #[serde(skip_serializing_if = "is_false")]
    pub preserve_selector_anchors: bool,
    /// Resolved local anchor relationships when the opt-in mode is enabled.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selector_anchor_relations: Option<Vec<ModuleDecompositionSelectorAnchor>>,
    /// True only when the candidate preserves whole structural atoms and
    /// passes the shared factorization gate over the complete owner graph.
    pub candidate_is_realizable: bool,
    pub coverage_complete: bool,
    pub owner_count: usize,
    pub assigned_owner_count: usize,
    pub unchanged_module_count: usize,
    /// Existing modules that would split into two or more suggested parts.
    pub modules: Vec<ModuleDecomposition>,
    /// Structural atoms that the candidate would assign to multiple modules.
    pub atomic_unit_conflicts: Vec<ModuleDecompositionAtomicConflict>,
    /// The exact cycle and rebinding result from the shared factorization gate.
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
    AtomicUnitConflicts,
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
    pub unit_ids: Vec<String>,
    pub merged_candidate_paths: Vec<ModulePath>,
    pub suggested_module_path: ModulePath,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionHomeRejoining {
    pub stop_reason: ModuleDecompositionHomeRejoiningStopReason,
    pub attempted_contractions: usize,
    pub accepted_contractions: usize,
    pub original_module_count: usize,
    pub fully_restored_module_count: usize,
    pub fully_restored_module_paths: Vec<ModulePath>,
    pub unrestored_module_paths: Vec<ModulePath>,
    pub final_group_count: usize,
    pub largest_group_owner_count: usize,
    pub largest_group_origin_module_count: usize,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModuleDecompositionHomeRejoiningStopReason {
    CandidateNotRealizable,
    Converged,
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
    pub owner_ids: Vec<String>,
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
    pub owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
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
    pub owner_ids: Vec<String>,
    pub binding_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionSelectorAnchor {
    pub module_path: ModulePath,
    pub member: String,
    pub referenced_by: String,
    pub member_owner_ids: Vec<String>,
    pub anchor_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionAtomicConflict {
    pub unit_id: String,
    pub owner_ids: Vec<String>,
    pub candidate_modules: Vec<ModulePath>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProposalsReport {
    pub proposals: Vec<ModuleProposal>,
    pub diagnostics: Vec<ProposalDiagnostic>,
    pub size_cap_lines: usize,
    pub residual_owner_count: usize,
    pub active_claimed_binding_count: usize,
    /// Counts by proposal verdict status.
    /// Keys use the report's stable snake_case status spelling.
    pub status_counts: BTreeMap<String, usize>,
    /// Counts by diagnostic reason. Diagnostics are not module
    /// assignments that can be landed as-is.
    pub diagnostic_counts: BTreeMap<String, usize>,
    /// Proposal size histograms. Each bucket includes total count
    /// plus how many proposals in the bucket are landable today.
    pub size_distributions: ProposalSizeDistributions,
    /// Per-contraction rejection diagnostics from the seeding
    /// protocol (`peel::quotient::build_seed_quotient`). Empty on
    /// well-formed input; populated when the spec declares an
    /// unrealizable owner grouping. Skipped from JSON output when
    /// empty so existing well-formed fixtures stay byte-identical.
    /// See `devinfra/js/debundle/docs/peel_proposer.md`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub seed_rejections: Vec<SeedContractionRejected>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProposalSizeDistributions {
    pub by_members: Vec<ProposalSizeBucketCount>,
    pub by_lines: Vec<ProposalSizeBucketCount>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProposalSizeBucketCount {
    pub bucket: String,
    pub count: usize,
    pub landable_count: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ModuleProposal {
    pub proposed_module_id: String,
    pub owner_ids: Vec<String>,
    /// Bindings declared by the cell's owners. Excludes
    /// anonymous side-effect statements, which appear under
    /// `anonymous_statement_owner_ids` instead.
    pub binding_ids: Vec<String>,
    /// Anonymous side-effect statements (owners with empty
    /// `declared_bindings`) in this cell. Lane workers materialize
    /// these via `anonymous_statements:` entries quoting the
    /// statement's source verbatim — the materializer's cycle
    /// gate counts these statements when determining whether the
    /// cell's promotion would cycle through `residual_entry`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statement_owner_ids: Vec<String>,
    pub size_lines_estimate: usize,
    /// `[start_line, end_line]` of the lowest-line and highest-line
    /// owner bodies. `None` when none of the cell's owners have
    /// a `source_location`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub internal_edges: usize,
    /// Edges from this cell to OTHER residual cells. Cycle-risk
    /// edges: promoting this cell to active while the pointed-at
    /// residual cells stay residual would create `<this>` →
    /// `residual_entry` reads, which the cycle gate will reject.
    pub edges_to_other_residual_cells: usize,
    /// Other residual cells (by proposed_module_id) this cell's
    /// outgoing constraining edges target.
    pub other_residual_cells_referenced: Vec<String>,
    /// Pre-resolution carrier for `other_residual_cells_referenced`:
    /// the candidate indices (pre-sort `candidate_classes` positions)
    /// this cell's outgoing constraining edges target. `emit_proposals`
    /// resolves these to each target candidate's ACTUAL final
    /// `proposed_module_id` (after merge/extend naming and post-sort id
    /// assignment), then clears this field. A target candidate may be a
    /// fresh `auto_partition`, an `extend:`, or a `merge:` class — so
    /// the resolved id is not always `auto_partition_*`. Never
    /// serialized; it exists only to thread index→id resolution through
    /// the topo-sort.
    #[serde(skip)]
    residual_target_candidate_idxs: Vec<usize>,
    /// Edges from this cell to active-claimed bindings. Safe:
    /// active modules materialize before residual_entry, so reads
    /// to them don't cycle. Informational.
    pub edges_to_active_modules: usize,
    /// Active module paths this cell's outgoing constraining edges
    /// target (deduplicated).
    pub active_modules_referenced: Vec<String>,
    /// Anonymous owners that the proposer cannot turn into stable
    /// `anonymous_statements:` selectors. The proposal stays visible
    /// for architectural review, but is not directly feedable to a
    /// spec edit while this list is non-empty.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unaddressable_anonymous_owner_ids: Vec<String>,
    /// Human-facing reasons a proposal may need manual handling even
    /// when the graph closure itself is useful.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub landability_notes: Vec<String>,
    /// Proposal verdict for this closed atomic-unit owner set.
    /// `PeelableNow` when the cell has no outgoing edges into other
    /// residual cells; `BlockedResidualDependency` when it does
    /// (promoting it alone would route reads through
    /// `residual_entry`, which the cycle gate rejects — the
    /// referenced cells must land first or together).
    pub status: PeelCandidateStatus,
    /// `true` when the proposal is graph-ready for a spec edit:
    /// `status` is `PeelableNow` (no outgoing constraining edges into
    /// other residual cells) AND every anonymous owner is addressable.
    /// Selector synthesis and the post-edit gate can still reject it.
    /// `modules apply` rejects rows where this is `false`;
    /// blocked-residual proposals need their closure grown (land the
    /// referenced cells together) or manual co-location first.
    pub landable_today: bool,
    /// When this proposal extends an existing active module, this carries the
    /// active module id. `None` for fresh-module proposals.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extends_module_id: Option<String>,
    /// Loose owner ids (residual today) that would be added to
    /// `extends_module_id`. Empty for fresh-module proposals.
    pub extension_owner_ids: Vec<String>,
    /// `Some(ids)` if this proposal merges two or more pre-existing
    /// active modules. The list is the active module ids being
    /// merged, in canonical (sorted) order. `None` for non-merge
    /// proposals (fresh module or single-module extension).
    ///
    /// When set, downstream consumers materialize the spec edit
    /// "combine modules A, B (, C, …) into one yaml file." The
    /// proposal's `owner_ids` is the union of merged-module owners
    /// plus any absorbed residual orphans (which also appear in
    /// `extension_owner_ids`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub merge_into: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PeelCandidateStatus {
    PeelableNow,
    BlockedResidualDependency,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProposalDiagnosticReason {
    ExceedsSizeCap,
}

/// A class the proposer cannot turn into a proposal. `reason` is
/// the discriminator; there is deliberately no `status` field, which
/// would be a write-only constant no reader consults.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProposalDiagnostic {
    pub diagnostic_id: String,
    pub owner_ids: Vec<String>,
    pub binding_ids: Vec<String>,
    pub size_lines_estimate: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub reason: ProposalDiagnosticReason,
}

pub fn propose_from_files(options: &ProposalOptions) -> Result<ProposalsReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    propose_from_graph(&graph, options)
}

/// [`propose_from_files`] for callers that already hold the parsed
/// `OwnerGraphReport`. The planner verbs load the graph once for their
/// own report sections; re-reading `owner_graph_path` here would parse
/// the same multi-megabyte JSON a second time per command.
pub fn propose_from_graph(
    graph: &OwnerGraphReport,
    options: &ProposalOptions,
) -> Result<ProposalsReport> {
    let claims = active_claims_from_options(graph, options)?;
    let addressable_anonymous_owner_ids = if options.source_root.is_some() {
        Some(addressable_anonymous_statement_owner_ids(
            graph,
            &options.owner_graph_path,
            &options.modules_root,
            options.source_root.as_deref(),
        )?)
    } else {
        None
    };
    propose_with_context(
        graph,
        &claims,
        options.size_cap_lines,
        ProposalContext {
            addressable_anonymous_owner_ids,
        },
    )
}

#[derive(Debug, Clone, Copy)]
pub struct ModuleDecompositionOptions {
    pub strategy: ModuleDecompositionStrategy,
    pub coarsening: ModuleDecompositionCoarseningMode,
    pub preserve_selector_anchors: bool,
    pub coarsen_atomic_conflicts: bool,
    pub rejoin_original_modules: bool,
}

pub fn decompose_modules_from_files_with_options(
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
) -> Result<ModuleDecompositionReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    decompose_modules_from_graph_with_options(&graph, options, decomposition)
}

pub fn decompose_modules_from_graph_with_options(
    graph: &OwnerGraphReport,
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
) -> Result<ModuleDecompositionReport> {
    let claims = active_claims_from_options(graph, options)?;
    let existing_module_paths = module_paths_from_options(graph, options)?;
    decompose_modules(
        graph,
        &claims,
        existing_module_paths,
        options,
        decomposition,
    )
}

fn module_paths_from_options(
    graph: &OwnerGraphReport,
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

fn active_claims_from_options(
    graph: &OwnerGraphReport,
    options: &ProposalOptions,
) -> Result<BTreeMap<String, ModulePath>> {
    let claims = if let Some(config) = &options.tree_config {
        let scope = ModuleTreeScope::from_config(config, &graph.chunk_id)?;
        let mut claims = BTreeMap::new();
        for root in scope.roots() {
            let path = options.modules_root.join(root);
            if path.exists() {
                claims.extend(load_active_claims(&path)?);
            }
        }
        claims
    } else {
        load_active_claims(&options.modules_root)?
    };
    Ok(claims)
}

fn decompose_modules(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, ModulePath>,
    existing_module_paths: BTreeSet<ModulePath>,
    options: &ProposalOptions,
    decomposition: ModuleDecompositionOptions,
) -> Result<ModuleDecompositionReport> {
    let strategy = decomposition.strategy;
    let owner_modules: Vec<ModulePath> = graph
        .nodes
        .iter()
        .map(|node| active_module_label(node, active_claims, graph))
        .collect::<Result<_>>()?;
    let residual_path = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.residual)
        .map(|module| module.path.clone())
        .context("owner graph has no residual module for decomposition validation")?;
    let include_residual = strategy == ModuleDecompositionStrategy::AtomicIncludingResidual;

    let mut module_owners: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for (owner_idx, node) in graph.nodes.iter().enumerate() {
        if include_residual || !graph.is_residual(&node.destination) {
            module_owners
                .entry(owner_modules[owner_idx].clone())
                .or_default()
                .push(owner_idx);
        }
    }
    let mut module_groups = match strategy {
        ModuleDecompositionStrategy::Seed => {
            let spec_modules: Vec<SpecModuleGroup> = module_owners
                .iter()
                .map(|(module_id, owner_indices)| SpecModuleGroup {
                    module_id: module_id.to_string(),
                    owner_ids: owner_indices
                        .iter()
                        .map(|&owner_idx| graph.nodes[owner_idx].id.clone())
                        .collect(),
                })
                .collect();
            // Module size is not a decomposition constraint. Keep the gate's
            // structural and timing checks while allowing an existing large module
            // to be considered as a whole.
            let (quotient, _) = build_same_module_seed_quotient(
                graph,
                &graph.atomic_graph.nodes,
                &spec_modules,
                usize::MAX,
            )?;
            module_owners
                .iter()
                .map(|(module_id, owner_indices)| {
                    let mut fragments_by_class: BTreeMap<ClassId, Vec<usize>> = BTreeMap::new();
                    for &owner_idx in owner_indices {
                        let owner = quotient
                            .owner_idx_of(&graph.nodes[owner_idx].id)
                            .with_context(|| {
                                format!(
                                    "owner {} disappeared from seed quotient",
                                    graph.nodes[owner_idx].id
                                )
                            })?;
                        fragments_by_class
                            .entry(quotient.class_of(owner))
                            .or_default()
                            .push(owner_idx);
                    }
                    Ok((
                        module_id.clone(),
                        fragments_by_class
                            .into_values()
                            .map(|owner_indices| (owner_indices, false))
                            .collect::<Vec<_>>(),
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()?
        }
        ModuleDecompositionStrategy::Atomic => {
            atomic_module_groups(graph, &module_owners, &owner_modules, &residual_path, false)?
        }
        ModuleDecompositionStrategy::AtomicIncludingResidual => {
            atomic_module_groups(graph, &module_owners, &owner_modules, &residual_path, true)?
        }
    };

    let resolved_modules = if decomposition.preserve_selector_anchors {
        Some(resolve_decomposition_claims(graph, options)?)
    } else {
        None
    };
    let selector_anchor_relations = if let Some(resolved_modules) = &resolved_modules {
        let relations = resolve_selector_anchor_relations(graph, &owner_modules, resolved_modules)?;
        let included_modules = module_owners.keys().cloned().collect();
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

    let mut occupied_paths: BTreeSet<ModulePath> = owner_modules.iter().cloned().collect();
    occupied_paths.extend(existing_module_paths.iter().cloned());
    let mut candidate_owner_modules = owner_modules.clone();
    let mut refinements = Vec::new();
    for (module_id, groups) in module_groups {
        if groups.len() < 2 {
            continue;
        }

        let mut fragments = Vec::with_capacity(groups.len());
        for (owner_indices, preserve_module_path) in groups {
            let suggested_module_path = if preserve_module_path {
                module_id.clone()
            } else if include_residual && module_id == residual_path {
                next_residual_fragment_path(&mut occupied_paths)
            } else {
                next_fragment_path(&module_id, &mut occupied_paths)
            };
            let mut fragment_owner_ids = Vec::with_capacity(owner_indices.len());
            let mut binding_ids = BTreeSet::new();
            for owner_idx in owner_indices {
                candidate_owner_modules[owner_idx] = suggested_module_path.clone();
                let node = &graph.nodes[owner_idx];
                fragment_owner_ids.push(node.id.clone());
                binding_ids.extend(
                    node.declared_bindings
                        .iter()
                        .map(|binding| binding.binding.to_string()),
                );
            }
            fragment_owner_ids.sort();
            fragments.push(ModuleDecompositionFragment {
                suggested_module_path,
                owner_ids: fragment_owner_ids,
                binding_ids: binding_ids.into_iter().collect(),
            });
        }
        refinements.push(ModuleDecomposition {
            module_id,
            fragments,
        });
    }

    let mut validation =
        validate_decomposition_candidate(graph, &candidate_owner_modules, &residual_path)?;
    let mut coarsening = None;
    anyhow::ensure!(
        !decomposition.coarsen_atomic_conflicts
            || decomposition.coarsening == ModuleDecompositionCoarseningMode::MergeBlockingSccs,
        "coarsening atomic conflicts requires SCC coarsening"
    );
    if decomposition.coarsening == ModuleDecompositionCoarseningMode::MergeBlockingSccs {
        let mut occupied_paths: BTreeSet<ModulePath> =
            candidate_owner_modules.iter().cloned().collect();
        occupied_paths.extend(existing_module_paths.iter().cloned());
        let atomic_conflict_merges = if decomposition.coarsen_atomic_conflicts {
            let merges = coarsen_atomic_conflicts(
                &validation.atomic_unit_conflicts,
                &residual_path,
                &mut occupied_paths,
                &mut candidate_owner_modules,
            )?;
            if !merges.is_empty() {
                validation = validate_decomposition_candidate(
                    graph,
                    &candidate_owner_modules,
                    &residual_path,
                )?;
                anyhow::ensure!(
                    validation.atomic_unit_conflicts.is_empty(),
                    "native atomic-conflict coarsening left structural units split"
                );
            }
            merges
        } else {
            Vec::new()
        };
        let mut rounds = Vec::new();
        let stop_reason = loop {
            if !validation.atomic_unit_conflicts.is_empty() {
                break ModuleDecompositionCoarseningStopReason::AtomicUnitConflicts;
            }
            if !validation.cross_rebinds.is_empty() {
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
                let mut represented_paths: BTreeSet<ModulePath> = candidate_owner_modules
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
                for owner_path in &mut candidate_owner_modules {
                    if paths.contains(owner_path) {
                        *owner_path = target.clone();
                    }
                }
            }
            rounds.push(ModuleDecompositionCoarseningRound {
                blocking_sccs,
                suggested_merge_paths,
            });
            validation =
                validate_decomposition_candidate(graph, &candidate_owner_modules, &residual_path)?;
        };
        coarsening = Some(ModuleDecompositionCoarsening {
            stop_reason,
            atomic_conflict_merges,
            rounds,
            groups: coarsened_groups(graph, &owner_modules, &candidate_owner_modules),
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
                graph,
                &owner_modules,
                &residual_path,
                &mut candidate_owner_modules,
            )?
        } else {
            summarize_home_rejoining(
                ModuleDecompositionHomeRejoiningStopReason::CandidateNotRealizable,
                &owner_modules,
                &candidate_owner_modules,
                0,
                0,
            )
        };
        validation =
            validate_decomposition_candidate(graph, &candidate_owner_modules, &residual_path)?;
        anyhow::ensure!(
            !accepted_baseline
                || (validation.atomic_unit_conflicts.is_empty()
                    && validation.cycles.is_empty()
                    && validation.cross_rebinds.is_empty()),
            "shared gate rejected the partition after original-home rejoining"
        );
        coarsening_report.groups =
            coarsened_groups(graph, &owner_modules, &candidate_owner_modules);
        coarsening_report.original_home_rejoining = Some(home_rejoining);
    }

    if let (Some(coarsening_report), Some(resolved_modules)) =
        (coarsening.as_mut(), resolved_modules.as_ref())
    {
        coarsening_report.naming_collisions = Some(coarsened_naming_collisions(
            graph,
            &owner_modules,
            &candidate_owner_modules,
            resolved_modules,
        )?);
    }

    let covered_owner_ids: BTreeSet<&str> =
        graph.nodes.iter().map(|node| node.id.as_str()).collect();
    let coverage_complete = covered_owner_ids.len() == graph.nodes.len()
        && candidate_owner_modules.len() == graph.nodes.len();
    let candidate_is_realizable = coverage_complete
        && validation.atomic_unit_conflicts.is_empty()
        && validation.cycles.is_empty()
        && validation.cross_rebinds.is_empty();
    let original_modules: BTreeSet<ModulePath> = owner_modules.into_iter().collect();
    let unchanged_module_count = original_modules.len().saturating_sub(refinements.len());

    Ok(ModuleDecompositionReport {
        strategy,
        preserve_selector_anchors: decomposition.preserve_selector_anchors,
        selector_anchor_relations,
        candidate_is_realizable,
        coverage_complete,
        owner_count: graph.nodes.len(),
        assigned_owner_count: candidate_owner_modules.len(),
        unchanged_module_count,
        modules: refinements,
        atomic_unit_conflicts: validation.atomic_unit_conflicts,
        cycles: validation.cycles,
        cross_rebinds: validation.cross_rebinds,
        coarsening,
    })
}

fn is_false(value: &bool) -> bool {
    !value
}

struct SelectorAnchorConstraint {
    module_path: ModulePath,
    member_owner_indices: Vec<usize>,
    anchor_owner_indices: Vec<usize>,
    report: ModuleDecompositionSelectorAnchor,
}

struct ResolvedDecompositionModule {
    claims: ModuleClaims,
    resolved: ResolvedSourceClaims,
}

fn resolve_decomposition_claims(
    graph: &OwnerGraphReport,
    options: &ProposalOptions,
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
        graph,
        &options.owner_graph_path,
        &options.modules_root,
        Some(source_root),
        &claim_sets,
    )?;

    Ok(modules
        .into_iter()
        .zip(resolved)
        .map(|((_, claims), resolved)| ResolvedDecompositionModule { claims, resolved })
        .collect())
}

fn resolve_selector_anchor_relations(
    graph: &OwnerGraphReport,
    owner_modules: &[ModulePath],
    modules: &[ResolvedDecompositionModule],
) -> Result<Vec<SelectorAnchorConstraint>> {
    let mut owner_indices_by_binding = BTreeMap::<String, Vec<usize>>::new();
    for (owner_index, node) in graph.nodes.iter().enumerate() {
        for binding in &node.declared_bindings {
            let owners = owner_indices_by_binding
                .entry(binding.binding.to_string())
                .or_default();
            if !owners.contains(&owner_index) {
                owners.push(owner_index);
            }
        }
    }

    let mut relations = Vec::new();
    for module in modules {
        let claims = &module.claims;
        let resolved = &module.resolved;
        for (member, selector) in &claims.member_selectors {
            let MemberSelectorSpec::IntrinsicAlias(alias) = selector else {
                continue;
            };
            let member_binding = resolved
                .member_bindings
                .get(member)
                .with_context(|| format!("intrinsic_alias member {member:?} did not resolve"))?;
            let anchor_binding = binding_for_member(claims, resolved, &alias.referenced_by)
                .with_context(|| {
                    format!(
                        "intrinsic_alias {member:?} anchor {:?} did not resolve",
                        alias.referenced_by
                    )
                })?;
            let member_owner_indices = owner_indices_by_binding
                .get(member_binding)
                .with_context(|| {
                    format!(
                        "intrinsic_alias member {member:?} binding {member_binding:?} has no owner"
                    )
                })?
                .clone();
            let anchor_owner_indices = owner_indices_by_binding
                .get(anchor_binding)
                .with_context(|| {
                    format!(
                        "intrinsic_alias anchor {:?} binding {anchor_binding:?} has no owner",
                        alias.referenced_by
                    )
                })?
                .clone();
            let relation_modules = member_owner_indices
                .iter()
                .chain(&anchor_owner_indices)
                .map(|&owner_index| owner_modules[owner_index].clone())
                .collect::<BTreeSet<_>>();
            anyhow::ensure!(
                relation_modules.len() == 1,
                "intrinsic_alias member {member:?} and anchor {:?} resolve to different modules: {}",
                alias.referenced_by,
                relation_modules
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            );
            let module_path = relation_modules
                .into_iter()
                .next()
                .context("intrinsic_alias relation has no owner module")?;
            relations.push(SelectorAnchorConstraint {
                module_path: module_path.clone(),
                member_owner_indices: member_owner_indices.clone(),
                anchor_owner_indices: anchor_owner_indices.clone(),
                report: ModuleDecompositionSelectorAnchor {
                    module_path,
                    member: member.clone(),
                    referenced_by: alias.referenced_by.clone(),
                    member_owner_ids: owner_ids(graph, &member_owner_indices),
                    anchor_owner_ids: owner_ids(graph, &anchor_owner_indices),
                },
            });
        }
    }
    Ok(relations)
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

fn owner_ids(graph: &OwnerGraphReport, owner_indices: &[usize]) -> Vec<String> {
    let mut owner_ids = owner_indices
        .iter()
        .map(|&owner_index| graph.nodes[owner_index].id.clone())
        .collect::<Vec<_>>();
    owner_ids.sort();
    owner_ids.dedup();
    owner_ids
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
        let mut group_by_owner = BTreeMap::new();
        for (group_index, (owner_indices, _)) in groups.iter().enumerate() {
            for &owner_index in owner_indices {
                anyhow::ensure!(
                    group_by_owner.insert(owner_index, group_index).is_none(),
                    "owner {} appears in more than one decomposition group in module {module_path}",
                    owner_index
                );
            }
        }
        let mut parents = (0..groups.len()).collect::<Vec<_>>();
        for relation in relevant {
            let member_groups = relation
                .member_owner_indices
                .iter()
                .map(|owner_index| {
                    group_by_owner.get(owner_index).copied().with_context(|| {
                        format!(
                            "intrinsic_alias member {} owner {} is absent from decomposition groups",
                            relation.report.member, owner_index
                        )
                    })
                })
                .collect::<Result<BTreeSet<_>>>()?;
            let anchor_groups = relation
                .anchor_owner_indices
                .iter()
                .map(|owner_index| {
                    group_by_owner.get(owner_index).copied().with_context(|| {
                        format!(
                            "intrinsic_alias anchor {} owner {} is absent from decomposition groups",
                            relation.report.referenced_by, owner_index
                        )
                    })
                })
                .collect::<Result<BTreeSet<_>>>()?;
            for &member_group in &member_groups {
                for &anchor_group in &anchor_groups {
                    union_groups(&mut parents, member_group, anchor_group);
                }
            }
        }

        let mut merged = BTreeMap::<usize, (Vec<usize>, bool)>::new();
        for (group_index, (owner_indices, preserve_module_path)) in groups.drain(..).enumerate() {
            let root = find_group_root(&mut parents, group_index);
            let (merged_owners, merged_preserve_path) = merged.entry(root).or_default();
            merged_owners.extend(owner_indices);
            *merged_preserve_path |= preserve_module_path;
        }
        *groups = merged
            .into_values()
            .map(|(mut owner_indices, preserve_module_path)| {
                owner_indices.sort_unstable();
                (owner_indices, preserve_module_path)
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
    candidate_owner_modules: &mut [ModulePath],
) -> Result<Vec<ModuleDecompositionAtomicConflictMerge>> {
    if conflicts.is_empty() {
        return Ok(Vec::new());
    }
    let paths: Vec<ModulePath> = candidate_owner_modules
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
                    "atomic unit {} references absent candidate path {path}",
                    conflict.unit_id
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
    let mut unit_ids_by_root = BTreeMap::<usize, BTreeSet<String>>::new();
    for conflict in conflicts {
        let first_path = conflict.candidate_modules.first().expect("checked above");
        let root = find_group_root(&mut parents, path_indices[first_path]);
        unit_ids_by_root
            .entry(root)
            .or_default()
            .insert(conflict.unit_id.clone());
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
            unit_ids: unit_ids_by_root
                .remove(&root)
                .context("coarsened component lost its reported atomic units")?
                .into_iter()
                .collect(),
            merged_candidate_paths,
            suggested_module_path,
        });
    }
    for path in candidate_owner_modules {
        if let Some(target) = remaps.get(path) {
            *path = target.clone();
        }
    }
    Ok(merges)
}

struct DecompositionValidation {
    atomic_unit_conflicts: Vec<ModuleDecompositionAtomicConflict>,
    cycles: Vec<gate::CycleReport>,
    cross_rebinds: Vec<String>,
}

fn validate_decomposition_candidate(
    graph: &OwnerGraphReport,
    candidate_owner_modules: &[ModulePath],
    residual_path: &ModulePath,
) -> Result<DecompositionValidation> {
    let owner_index: HashMap<&str, usize> = graph
        .nodes
        .iter()
        .enumerate()
        .map(|(idx, node)| (node.id.as_str(), idx))
        .collect();
    let mut atomic_unit_conflicts = Vec::new();
    for unit in &graph.atomic_graph.nodes {
        let mut candidate_modules = BTreeSet::new();
        for owner_id in &unit.owner_ids {
            let owner_idx = owner_index.get(owner_id.as_str()).with_context(|| {
                format!(
                    "atomic unit {} references unknown owner {owner_id}",
                    unit.id
                )
            })?;
            candidate_modules.insert(candidate_owner_modules[*owner_idx].clone());
        }
        if candidate_modules.len() > 1 {
            atomic_unit_conflicts.push(ModuleDecompositionAtomicConflict {
                unit_id: unit.id.clone(),
                owner_ids: unit.owner_ids.clone(),
                candidate_modules: candidate_modules.into_iter().collect(),
            });
        }
    }

    let owner_graph = OwnerGraph::from_report(graph)
        .context("reconstructing the owner graph for decomposition validation")?;
    let (assignments, paths_by_module_id) =
        candidate_partition_assignments(candidate_owner_modules, residual_path)?;
    let partition = Partition::from_assignments(assignments, ModuleId::logical(0));
    let gate_report = gate::validate_factorization(&owner_graph, &partition, &|module_id| {
        paths_by_module_id
            .get(&module_id)
            .cloned()
            .unwrap_or_else(|| residual_path.clone())
    });
    Ok(DecompositionValidation {
        atomic_unit_conflicts,
        cycles: gate_report.cycles,
        cross_rebinds: gate_report.cross_rebinds,
    })
}

fn candidate_partition_assignments(
    candidate_owner_modules: &[ModulePath],
    residual_path: &ModulePath,
) -> Result<(Vec<ModuleId>, BTreeMap<ModuleId, ModulePath>)> {
    let mut paths: BTreeSet<ModulePath> = candidate_owner_modules.iter().cloned().collect();
    paths.insert(residual_path.clone());
    let mut module_ids = BTreeMap::from([(residual_path.clone(), ModuleId::logical(0))]);
    let mut paths_by_module_id = BTreeMap::from([(ModuleId::logical(0), residual_path.clone())]);
    let mut next_module_idx = 1;
    for path in paths {
        if &path == residual_path {
            continue;
        }
        let module_id = ModuleId::logical(next_module_idx);
        next_module_idx += 1;
        module_ids.insert(path.clone(), module_id);
        paths_by_module_id.insert(module_id, path);
    }
    let assignments = candidate_owner_modules
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
    graph: &OwnerGraphReport,
    original_owner_modules: &[ModulePath],
    residual_path: &ModulePath,
    candidate_owner_modules: &mut [ModulePath],
) -> Result<ModuleDecompositionHomeRejoining> {
    let (assignments, _) = candidate_partition_assignments(candidate_owner_modules, residual_path)?;
    // Decomposition has no size ceiling: an original home can be larger than
    // the proposer's default cap. The shared incremental gate still checks
    // every contraction against the complete candidate partition.
    let mut quotient = QuotientGraph::from_report_with_realizable_partition(
        graph,
        usize::MAX,
        &assignments,
        ModuleId::logical(0),
    )?;

    let mut owners_by_candidate = BTreeMap::<ModulePath, Vec<usize>>::new();
    let mut home_owner_counts = BTreeMap::<ModulePath, usize>::new();
    for (owner_idx, (candidate, home)) in candidate_owner_modules
        .iter()
        .zip(original_owner_modules)
        .enumerate()
    {
        owners_by_candidate
            .entry(candidate.clone())
            .or_default()
            .push(owner_idx);
        *home_owner_counts.entry(home.clone()).or_default() += 1;
    }
    // A coarsened group spanning multiple original homes is already a gate
    // accepted indivisible class. Never contract it with a pure home fragment.
    let mut pure_home_representatives = BTreeMap::<ModulePath, Vec<OwnerIdx>>::new();
    for owners in owners_by_candidate.values() {
        let home = &original_owner_modules[owners[0]];
        if owners
            .iter()
            .all(|&owner_idx| &original_owner_modules[owner_idx] == home)
        {
            pure_home_representatives
                .entry(home.clone())
                .or_default()
                .push(OwnerIdx(owners[0]));
        }
    }

    let mut attempted = 0;
    let mut accepted = 0;
    // Stable path and owner order makes the result deterministic. Exhaust each
    // home before moving to the next; another home's contraction may make an
    // earlier rejected pair admissible, so repeat whole-home passes only when
    // some contraction was accepted during the previous pass.
    loop {
        let mut pass_progress = false;
        for representatives in pure_home_representatives.values() {
            loop {
                let classes: Vec<ClassId> = representatives
                    .iter()
                    .map(|&owner| quotient.class_of(owner))
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect();
                let mut home_progress = false;
                'pairs: for (left_idx, &left) in classes.iter().enumerate() {
                    for &right in classes.iter().skip(left_idx + 1) {
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

    let mut final_paths = BTreeMap::<ClassId, ModulePath>::new();
    let mut used_paths = BTreeSet::new();
    for class in quotient.iter_classes() {
        let members: Vec<usize> = quotient.class_members(class).map(|owner| owner.0).collect();
        let homes: BTreeSet<&ModulePath> = members
            .iter()
            .map(|&owner_idx| &original_owner_modules[owner_idx])
            .collect();
        let candidate_paths: BTreeSet<&ModulePath> = members
            .iter()
            .map(|&owner_idx| &candidate_owner_modules[owner_idx])
            .collect();
        let restored_home = homes.iter().next().filter(|home| {
            homes.len() == 1 && home_owner_counts.get(*home) == Some(&members.len())
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
    for (owner_idx, path) in candidate_owner_modules.iter_mut().enumerate() {
        *path = final_paths
            .get(&quotient.class_of(OwnerIdx(owner_idx)))
            .expect("every owner belongs to a live quotient class")
            .clone();
    }
    Ok(summarize_home_rejoining(
        ModuleDecompositionHomeRejoiningStopReason::Converged,
        original_owner_modules,
        candidate_owner_modules,
        attempted,
        accepted,
    ))
}

fn summarize_home_rejoining(
    stop_reason: ModuleDecompositionHomeRejoiningStopReason,
    original_owner_modules: &[ModulePath],
    candidate_owner_modules: &[ModulePath],
    attempted_contractions: usize,
    accepted_contractions: usize,
) -> ModuleDecompositionHomeRejoining {
    let mut groups = BTreeMap::<&ModulePath, (usize, BTreeSet<&ModulePath>)>::new();
    let mut homes = BTreeMap::<&ModulePath, BTreeSet<&ModulePath>>::new();
    for (home, candidate) in original_owner_modules.iter().zip(candidate_owner_modules) {
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
        attempted_contractions,
        accepted_contractions,
        original_module_count: fully_restored_module_paths.len() + unrestored_module_paths.len(),
        fully_restored_module_count: fully_restored_module_paths.len(),
        fully_restored_module_paths,
        unrestored_module_paths,
        final_group_count: groups.len(),
        largest_group_owner_count: groups.values().map(|group| group.0).max().unwrap_or(0),
        largest_group_origin_module_count: groups
            .values()
            .map(|group| group.1.len())
            .max()
            .unwrap_or(0),
    }
}

fn coarsened_groups(
    graph: &OwnerGraphReport,
    original_owner_modules: &[ModulePath],
    candidate_owner_modules: &[ModulePath],
) -> Vec<ModuleDecompositionCoarsenedGroup> {
    let mut by_module: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for (owner_idx, path) in candidate_owner_modules.iter().enumerate() {
        by_module.entry(path.clone()).or_default().push(owner_idx);
    }
    by_module
        .into_iter()
        .map(|(suggested_module_path, owner_indices)| {
            let mut origin_module_paths = BTreeSet::new();
            let mut owner_ids = Vec::new();
            let mut binding_ids = BTreeSet::new();
            for owner_idx in owner_indices {
                origin_module_paths.insert(original_owner_modules[owner_idx].clone());
                let owner = &graph.nodes[owner_idx];
                owner_ids.push(owner.id.clone());
                binding_ids.extend(
                    owner
                        .declared_bindings
                        .iter()
                        .map(|binding| binding.binding.to_string()),
                );
            }
            owner_ids.sort();
            ModuleDecompositionCoarsenedGroup {
                suggested_module_path,
                origin_module_paths: origin_module_paths.into_iter().collect(),
                owner_ids,
                binding_ids: binding_ids.into_iter().collect(),
            }
        })
        .collect()
}

fn coarsened_naming_collisions(
    graph: &OwnerGraphReport,
    original_owner_modules: &[ModulePath],
    candidate_owner_modules: &[ModulePath],
    modules: &[ResolvedDecompositionModule],
) -> Result<Vec<ModuleDecompositionNamingCollision>> {
    let mut owners_by_binding = BTreeMap::<String, Vec<usize>>::new();
    for (owner_idx, owner) in graph.nodes.iter().enumerate() {
        for binding in &owner.declared_bindings {
            owners_by_binding
                .entry(binding.binding.to_string())
                .or_default()
                .push(owner_idx);
        }
    }

    // Enumerate names from the canonical authored claims, then use the same
    // resolved binding lookup as selector-anchor cohesion. A claim can have
    // multiple owner sites, but it is one prospective export per destination.
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
            let owner_indices = owners_by_binding.get(binding_id).with_context(|| {
                format!(
                    "resolved member {readable_name:?} binding {binding_id:?} has no graph owner"
                )
            })?;
            for &owner_idx in owner_indices {
                by_name
                    .entry((
                        candidate_owner_modules[owner_idx].clone(),
                        readable_name.clone(),
                    ))
                    .or_default()
                    .entry((
                        original_owner_modules[owner_idx].clone(),
                        binding_id.to_string(),
                    ))
                    .or_default()
                    .insert(graph.nodes[owner_idx].id.clone());
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
                    .map(|((original_module_path, binding_id), owner_ids)| {
                        ModuleDecompositionNamedClaim {
                            original_module_path,
                            binding_id,
                            owner_ids: owner_ids.into_iter().collect(),
                        }
                    })
                    .collect(),
            })
        })
        .collect())
}

type AtomicModuleGroups = BTreeMap<ModulePath, Vec<(Vec<usize>, bool)>>;

fn atomic_module_groups(
    graph: &OwnerGraphReport,
    module_owners: &BTreeMap<ModulePath, Vec<usize>>,
    owner_modules: &[ModulePath],
    residual_path: &ModulePath,
    include_residual: bool,
) -> Result<AtomicModuleGroups> {
    let owner_index: HashMap<&str, usize> = graph
        .nodes
        .iter()
        .enumerate()
        .map(|(idx, node)| (node.id.as_str(), idx))
        .collect();
    let mut unit_by_owner = vec![None; graph.nodes.len()];
    for (unit_idx, unit) in graph.atomic_graph.nodes.iter().enumerate() {
        for owner_id in &unit.owner_ids {
            let owner_idx = owner_index.get(owner_id.as_str()).with_context(|| {
                format!(
                    "atomic unit {} references unknown owner {owner_id}",
                    unit.id
                )
            })?;
            if let Some(previous_unit) = unit_by_owner[*owner_idx].replace(unit_idx) {
                anyhow::bail!(
                    "owner {owner_id} appears in atomic units {previous_unit} and {unit_idx}"
                );
            }
        }
    }
    if let Some(owner_idx) = unit_by_owner.iter().position(Option::is_none) {
        anyhow::bail!(
            "owner {} is absent from the atomic graph",
            graph.nodes[owner_idx].id
        );
    }

    let mut groups_by_module: BTreeMap<ModulePath, Vec<(Vec<usize>, bool)>> = BTreeMap::new();
    let mut preserved_by_module: BTreeMap<ModulePath, Vec<usize>> = BTreeMap::new();
    for unit in &graph.atomic_graph.nodes {
        let owner_indices = unit
            .owner_ids
            .iter()
            .map(|owner_id| {
                owner_index
                    .get(owner_id.as_str())
                    .copied()
                    .with_context(|| {
                        format!(
                            "atomic unit {} references unknown owner {owner_id}",
                            unit.id
                        )
                    })
            })
            .collect::<Result<Vec<_>>>()?;
        let modules = owner_indices
            .iter()
            .map(|&owner_idx| owner_modules[owner_idx].clone())
            .collect::<BTreeSet<_>>();
        if modules.len() > 1 {
            for owner_idx in owner_indices {
                if module_owners.contains_key(&owner_modules[owner_idx]) {
                    preserved_by_module
                        .entry(owner_modules[owner_idx].clone())
                        .or_default()
                        .push(owner_idx);
                }
            }
        } else if let Some(module_id) = modules.into_iter().next()
            && module_owners.contains_key(&module_id)
        {
            let keep_in_entry = include_residual
                && module_id == *residual_path
                && owner_indices.iter().any(|&owner_idx| {
                    matches!(
                        graph.nodes[owner_idx].statement_kind,
                        StatementKind::Import | StatementKind::Export
                    )
                });
            if keep_in_entry {
                preserved_by_module
                    .entry(module_id)
                    .or_default()
                    .extend(owner_indices);
            } else {
                groups_by_module
                    .entry(module_id)
                    .or_default()
                    .push((owner_indices, false));
            }
        }
    }
    for (module_id, mut owner_indices) in preserved_by_module {
        owner_indices.sort_unstable();
        owner_indices.dedup();
        groups_by_module
            .entry(module_id)
            .or_default()
            .push((owner_indices, true));
    }

    for (module_id, expected_owner_indices) in module_owners {
        let mut grouped_owner_indices = groups_by_module
            .get(module_id)
            .into_iter()
            .flatten()
            .flat_map(|(indices, _)| indices.iter().copied())
            .collect::<Vec<_>>();
        grouped_owner_indices.sort_unstable();
        let mut expected_owner_indices = expected_owner_indices.clone();
        expected_owner_indices.sort_unstable();
        anyhow::ensure!(
            grouped_owner_indices == expected_owner_indices,
            "atomic groups do not cover owners in module {module_id} exactly once"
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

pub fn propose(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, ModulePath>,
    size_cap_lines: usize,
) -> Result<ProposalsReport> {
    propose_with_context(
        graph,
        active_claims,
        size_cap_lines,
        ProposalContext::default(),
    )
}

#[derive(Debug, Clone, Default)]
struct ProposalContext {
    addressable_anonymous_owner_ids: Option<BTreeSet<String>>,
}

fn propose_with_context(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, ModulePath>,
    size_cap_lines: usize,
    context: ProposalContext,
) -> Result<ProposalsReport> {
    let owner_index: HashMap<&str, usize> = graph
        .nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.id.as_str(), i))
        .collect();

    let residual: BTreeSet<usize> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| graph.is_residual(&node.destination))
        .map(|(i, _)| i)
        .collect();

    let owner_to_active_module: HashMap<usize, ModulePath> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| !graph.is_residual(&node.destination))
        .map(|(i, node)| Ok((i, active_module_label(node, active_claims, graph)?)))
        .collect::<Result<_>>()?;

    // Spec-module groups: every active-claimed module's owners
    // pre-contracted into one class. Used by `build_seed_quotient`'s
    // pass 2 to seed pre-existing-module classes; the greedy then
    // absorbs orphans into them.
    let spec_modules = spec_module_groups(graph);
    let (mut quotient, seed_rejections) = build_seed_quotient(
        graph,
        &graph.atomic_graph.nodes,
        &spec_modules,
        size_cap_lines,
    )?;
    // Mutates the quotient in place; the returned merge-step list is unused here.
    greedy_merge_to_convergence(&mut quotient);

    // Per-class edge accounting. Walk every constraining owner
    // edge and classify (source-class, target-class) into:
    // - internal (same class, residual-only)
    // - inter-residual (different non-pre-existing-module classes)
    // - residual → pre-existing-module class
    // Edges originating from non-residual owners are skipped: they are
    // part of the spec module's internal initialization, not relevant
    // to peel proposals.
    let mut residual_constraining_edges: Vec<(usize, usize)> = Vec::new();
    let mut edges_to_active: Vec<(usize, ModulePath)> = Vec::new();
    for edge in &graph.edges {
        if !edge.constrains_init_order {
            continue;
        }
        let (Some(&source), Some(&target)) = (
            owner_index.get(edge.source.as_str()),
            owner_index.get(edge.target.as_str()),
        ) else {
            continue;
        };
        if !residual.contains(&source) || source == target {
            continue;
        }
        if residual.contains(&target) {
            residual_constraining_edges.push((source, target));
        } else if let Some(module_path) = owner_to_active_module.get(&target) {
            edges_to_active.push((source, module_path.clone()));
        }
    }

    // Per-class label (pre-existing module path). Built from the
    // active-claimed owners surviving in each class; if a class
    // contains owners from two distinct active modules, the labels
    // are collected and surfaced as a `merge_into` later.
    let mut class_to_labels: BTreeMap<ClassId, BTreeSet<ModulePath>> = BTreeMap::new();
    for (idx, _) in graph.nodes.iter().enumerate() {
        let Some(label) = owner_to_active_module.get(&idx) else {
            continue;
        };
        let c = quotient.class_of(OwnerIdx(idx));
        class_to_labels.entry(c).or_default().insert(label.clone());
    }

    let proposals = emit_proposals(
        &quotient,
        &class_to_labels,
        &residual_constraining_edges,
        &edges_to_active,
        graph,
        size_cap_lines,
        &context,
    );
    let diagnostics =
        collect_size_cap_diagnostics(&quotient, &class_to_labels, graph, size_cap_lines);
    let status_counts = status_counts(&proposals);
    let diagnostic_counts = diagnostic_counts(&diagnostics);
    let size_distributions = size_distributions(&proposals);
    Ok(ProposalsReport {
        proposals,
        diagnostics,
        size_cap_lines,
        residual_owner_count: residual.len(),
        active_claimed_binding_count: active_claims.len(),
        status_counts,
        diagnostic_counts,
        size_distributions,
        seed_rejections,
    })
}

/// The canonical [`ModulePath`] an owner's destination resolves to.
///
/// A claimed binding resolves via `active_claims` (the spec authority).
/// Otherwise the destination's canonical path comes straight from the
/// module table — a single source of truth, already normalized, so no
/// chunk-prefix stripping or fallback chain is needed and two owners of
/// one module can never disagree (the former self-merge bug). A
/// destination key absent from the table is a malformed report
/// (truncated / version-skewed `owner_graph.json`), surfaced as an
/// `Err` rather than a panic so the CLI reports it cleanly.
fn active_module_label(
    node: &OwnerGraphNodeReport,
    active_claims: &BTreeMap<String, ModulePath>,
    graph: &OwnerGraphReport,
) -> Result<ModulePath> {
    if let Some(claimed) = node
        .declared_bindings
        .iter()
        .find_map(|b| active_claims.get(b.binding.as_str()))
    {
        return Ok(claimed.clone());
    }
    Ok(graph
        .module(&node.destination)
        .with_context(|| {
            format!(
                "malformed owner graph: owner {} destination {} absent from module table",
                node.id, node.destination
            )
        })?
        .path
        .clone())
}

/// Spec-module groups derived from `graph.nodes` destinations.
/// Owners with a non-residual destination are grouped by their
/// (interned) destination key; residual owners stay out (they'll be
/// singletons).
fn spec_module_groups(graph: &OwnerGraphReport) -> Vec<SpecModuleGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for node in &graph.nodes {
        if graph.is_residual(&node.destination) {
            continue;
        }
        groups
            .entry(node.destination.0.clone())
            .or_default()
            .push(node.id.clone());
    }
    groups
        .into_iter()
        .map(|(module_id, mut owner_ids)| {
            owner_ids.sort();
            SpecModuleGroup {
                module_id,
                owner_ids,
            }
        })
        .collect()
}

/// The line count the size cap (and `size_lines_estimate`) measures
/// for a class, matching the spec edit the proposal would perform:
///
/// - Unlabeled (fresh-module) class: every owner lands in the new
///   module, so the whole class counts (`class_lines`).
/// - Labeled (extension / merge) class: the edit only ADDS the
///   residual-origin owners to already-active modules, so only those
///   additions count. Measuring the whole class here would reject a
///   10-line extension to a 600-line module as "600 lines".
fn class_proposal_lines(
    quotient: &QuotientGraph,
    graph: &OwnerGraphReport,
    c: ClassId,
    is_extension: bool,
) -> usize {
    if !is_extension {
        return quotient.class_lines(c);
    }
    quotient
        .class_members(c)
        .filter(|o| graph.is_residual(&graph.nodes[o.0].destination))
        .map(|o| graph.nodes[o.0].line_count())
        .sum()
}

/// `true` when `class_to_labels` marks `c` as carrying at least one
/// pre-existing-module label (i.e. the class is an extension or merge,
/// not a fresh module).
fn class_has_labels(class_to_labels: &BTreeMap<ClassId, BTreeSet<ModulePath>>, c: ClassId) -> bool {
    class_to_labels.get(&c).is_some_and(|s| !s.is_empty())
}

/// Render the quotient's surviving (non-empty) classes into proposals.
///
/// Each surviving class becomes at most one proposal. The shape is
/// driven by `class_to_labels`:
/// - 0 labels: residual-only class. Fresh-module proposal
///   (`auto_partition_NNNN`).
/// - 1 label: pre-existing-module class. Extension proposal
///   (`extend:M`); proposal surfaces only the residual-origin
///   owners as `extension_owner_ids` (the spec edit: "add these
///   owners to module M"). If the class has no residual-origin
///   owners, no proposal is emitted (the spec module is already
///   complete).
/// - ≥2 labels: module↔module merge proposal (`merge:M1+M2+…`).
///   Residual-origin owners ride as `extension_owner_ids`.
///
/// Classes that fail the post-greedy size cap appear as
/// `ProposalDiagnostic`s (computed separately in
/// `collect_size_cap_diagnostics`).
fn emit_proposals(
    quotient: &QuotientGraph,
    class_to_labels: &BTreeMap<ClassId, BTreeSet<ModulePath>>,
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, ModulePath)],
    graph: &OwnerGraphReport,
    size_cap_lines: usize,
    context: &ProposalContext,
) -> Vec<ModuleProposal> {
    // Candidate classes: every live class that either has owners or
    // carries module labels. We skip classes whose only members are
    // spec-module owners with no residual extensions (= pre-existing
    // modules unchanged by the peel) — those don't represent a spec
    // edit.
    let mut candidate_classes: Vec<ClassId> = Vec::new();
    for c in quotient.iter_classes() {
        let labels = class_to_labels.get(&c);
        let n_labels = labels.map(|s| s.len()).unwrap_or(0);
        let has_residual_origin = quotient
            .class_members(c)
            .any(|o| graph.is_residual(&graph.nodes[o.0].destination));
        if n_labels == 0 {
            // Pure residual class. Always emit (fresh-module).
            candidate_classes.push(c);
        } else if n_labels == 1 {
            // Extension: only emit if there are residual-origin
            // owners to add. A class containing only the
            // already-claimed spec-module owners is a no-op.
            if has_residual_origin {
                candidate_classes.push(c);
            }
        } else {
            // Merge of ≥2 pre-existing modules. Always emit.
            candidate_classes.push(c);
        }
    }

    // Classes that exceed the size cap aren't proposals — they
    // surface as diagnostics. Filter them out here so they don't
    // count toward the `auto_partition_NNNN` numbering. The cap
    // applies to the size of the spec edit, not the whole class —
    // see `class_proposal_lines`.
    candidate_classes.retain(|&c| {
        class_proposal_lines(quotient, graph, c, class_has_labels(class_to_labels, c))
            <= size_cap_lines
    });

    // Owner-index → candidate_classes-position. Used by edge-
    // attribution to bucket cross-class edges by candidate id.
    let mut owner_to_candidate: HashMap<usize, usize> = HashMap::new();
    for (idx, &c) in candidate_classes.iter().enumerate() {
        for owner in quotient.class_members(c) {
            owner_to_candidate.insert(owner.0, idx);
        }
    }

    // Bucket every residual / active edge by source candidate in ONE
    // pass (`O(edges)`, not `O(candidates × edges)`).
    let mut edge_stats: Vec<CandidateEdgeStats> = candidate_classes
        .iter()
        .map(|_| CandidateEdgeStats::default())
        .collect();
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_candidate.get(&s), owner_to_candidate.get(&t))
        else {
            continue;
        };
        if cs == ct {
            edge_stats[cs].internal += 1;
        } else {
            edge_stats[cs].to_residual += 1;
            edge_stats[cs].residual_targets.insert(ct);
        }
    }
    for (source_owner, module_path) in active_edges {
        if let Some(&cs) = owner_to_candidate.get(source_owner) {
            edge_stats[cs].to_active += 1;
            edge_stats[cs].active_targets.insert(module_path.clone());
        }
    }

    let mut proposals: Vec<ModuleProposal> = candidate_classes
        .iter()
        .zip(edge_stats)
        .enumerate()
        .map(|(candidate_idx, (&class_id, edges))| {
            build_proposal(
                candidate_idx,
                class_id,
                class_to_labels.get(&class_id),
                quotient,
                edges,
                graph,
                context,
            )
        })
        .collect();

    // Residual-dependency depth sort with source-line tie-break.
    let depths = compute_topo_depths(proposals.len(), residual_edges, &owner_to_candidate);
    let mut indexed: Vec<(usize, ModuleProposal)> = proposals.drain(..).enumerate().collect();
    indexed.sort_by(|(li, left), (ri, right)| {
        depths[*li].cmp(&depths[*ri]).then_with(|| {
            let lk = left
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX);
            let rk = right
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX);
            lk.cmp(&rk)
        })
    });

    // After topo-sort the candidate indices are renumbered. Remember
    // each proposal's original (pre-sort) candidate index so we can
    // resolve cross-references — which were recorded against pre-sort
    // indices — to the targets' final `proposed_module_id`s.
    let orig_idx_of: Vec<usize> = indexed.iter().map(|(orig_idx, _)| *orig_idx).collect();
    let mut out: Vec<ModuleProposal> = indexed.into_iter().map(|(_, p)| p).collect();

    // Assign final ids for fresh-module proposals (merge/extend ids
    // were fixed at `build_proposal` time from their labels). This must
    // happen before resolving cross-references so the index→final-id
    // map below sees every candidate's ACTUAL final id.
    let mut fresh_counter = 0usize;
    for proposal in out.iter_mut() {
        if proposal.extends_module_id.is_none() && proposal.merge_into.is_none() {
            proposal.proposed_module_id = format!("auto_partition_{fresh_counter:04}");
            fresh_counter += 1;
        }
    }

    // pre-sort candidate index → final proposed_module_id, covering all
    // candidate classes (fresh `auto_partition`, `extend:`, `merge:`).
    // Owns the id strings so the immutable borrow on `out` ends before
    // the mutable resolution loop below.
    let final_id_for: HashMap<usize, String> = out
        .iter()
        .enumerate()
        .map(|(new_idx, proposal)| (orig_idx_of[new_idx], proposal.proposed_module_id.clone()))
        .collect();

    // Resolve cross-references once, from the candidate-index carrier to
    // the targets' final ids. Every target index is a live candidate
    // (it came from `owner_to_candidate`, which only maps candidate-
    // class owners), so the lookup never misses — a miss would mean a
    // dangling cross-reference, which is a bug worth crashing on.
    for proposal in out.iter_mut() {
        let mut referenced: Vec<String> = proposal
            .residual_target_candidate_idxs
            .iter()
            .map(|idx| {
                final_id_for
                    .get(idx)
                    .unwrap_or_else(|| {
                        panic!("cross-reference target candidate {idx} has no final proposal id")
                    })
                    .clone()
            })
            .collect();
        referenced.sort();
        referenced.dedup();
        proposal.other_residual_cells_referenced = referenced;
        proposal.residual_target_candidate_idxs = Vec::new();
    }
    out
}

/// Per-candidate edge attribution, bucketed in one pass over the
/// residual / active edge lists by `emit_proposals`.
#[derive(Debug, Default)]
struct CandidateEdgeStats {
    /// Constraining residual edges with both endpoints in this candidate.
    internal: usize,
    /// Constraining residual edges from this candidate into other
    /// candidate classes.
    to_residual: usize,
    /// The target candidate indices of `to_residual` edges.
    residual_targets: BTreeSet<usize>,
    /// Constraining edges from this candidate into active-claimed modules.
    to_active: usize,
    /// The active module paths `to_active` edges target.
    active_targets: BTreeSet<ModulePath>,
}

/// Build a `ProposalDiagnostic(ExceedsSizeCap)` for every
/// surviving class whose proposal size exceeds the cap. Uses the same
/// `class_proposal_lines` measure as `emit_proposals`' cap filter, so
/// a class is either a proposal or a size-cap diagnostic — never both,
/// never neither. The current pipeline reads it from the post-greedy
/// quotient.
fn collect_size_cap_diagnostics(
    quotient: &QuotientGraph,
    class_to_labels: &BTreeMap<ClassId, BTreeSet<ModulePath>>,
    graph: &OwnerGraphReport,
    size_cap_lines: usize,
) -> Vec<ProposalDiagnostic> {
    let mut diagnostics = Vec::new();
    for c in quotient.iter_classes() {
        if class_proposal_lines(quotient, graph, c, class_has_labels(class_to_labels, c))
            <= size_cap_lines
        {
            continue;
        }
        // Compose the diagnostic from the class's owners.
        let mut owner_ids: Vec<String> = Vec::new();
        let mut binding_ids: BTreeSet<String> = BTreeSet::new();
        let mut line_range = LineRange::new();
        let mut max_ordinal = 0usize;
        let mut min_ordinal = usize::MAX;
        for owner in quotient.class_members(c) {
            let node = &graph.nodes[owner.0];
            owner_ids.push(node.id.clone());
            for binding in &node.declared_bindings {
                binding_ids.insert(binding.binding.to_string());
            }
            if let Some(loc) = &node.source_location {
                line_range.expand(loc);
            }
            min_ordinal = min_ordinal.min(node.statement_ordinal.0);
            max_ordinal = max_ordinal.max(node.statement_ordinal.0);
        }
        owner_ids.sort();
        let idx = diagnostics.len();
        diagnostics.push(ProposalDiagnostic {
            diagnostic_id: format!(
                "diagnostic:{}_{idx:04}",
                diagnostic_reason_key(ProposalDiagnosticReason::ExceedsSizeCap),
            ),
            owner_ids,
            binding_ids: binding_ids.into_iter().collect(),
            size_lines_estimate: class_proposal_lines(
                quotient,
                graph,
                c,
                class_has_labels(class_to_labels, c),
            ),
            source_line_range: line_range.into_array(),
            ordinal_span: max_ordinal.saturating_sub(min_ordinal),
            reason: ProposalDiagnosticReason::ExceedsSizeCap,
        });
    }
    diagnostics
}

fn compute_topo_depths(
    candidate_count: usize,
    residual_edges: &[(usize, usize)],
    owner_to_candidate: &HashMap<usize, usize>,
) -> Vec<usize> {
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); candidate_count];
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_candidate.get(&s), owner_to_candidate.get(&t))
        else {
            continue;
        };
        if cs != ct {
            adj[cs].insert(ct);
        }
    }
    let mut depths = vec![None; candidate_count];
    fn dfs(node: usize, adj: &[BTreeSet<usize>], depths: &mut [Option<usize>]) -> usize {
        if let Some(d) = depths[node] {
            return d;
        }
        depths[node] = Some(0);
        let max_child = adj[node]
            .iter()
            .map(|&child| dfs(child, adj, depths))
            .max()
            .map(|d| d + 1)
            .unwrap_or(0);
        depths[node] = Some(max_child);
        max_child
    }
    for i in 0..candidate_count {
        dfs(i, &adj, &mut depths);
    }
    depths.into_iter().map(|d| d.unwrap_or(0)).collect()
}

fn build_proposal(
    candidate_idx: usize,
    class_id: ClassId,
    labels: Option<&BTreeSet<ModulePath>>,
    quotient: &QuotientGraph,
    edges: CandidateEdgeStats,
    graph: &OwnerGraphReport,
    context: &ProposalContext,
) -> ModuleProposal {
    // Labels arrive sorted (BTreeSet over canonical `ModulePath`).
    // Convert to wire strings once, at this boundary; identity logic
    // above this point is type-safe.
    let label_vec: Vec<String> = labels
        .map(|s| s.iter().map(ModulePath::to_string).collect())
        .unwrap_or_default();
    let is_extension = !label_vec.is_empty();
    let merge_into: Option<Vec<String>> = (label_vec.len() >= 2).then(|| label_vec.clone());
    let extends_module_id: Option<String> = (label_vec.len() == 1)
        .then(|| label_vec[0].clone())
        .or_else(|| {
            // For merge proposals, choose a canonical
            // `extends_module_id` = the first label (sorted).
            (label_vec.len() >= 2).then(|| label_vec[0].clone())
        });

    let all_owner_idxs: Vec<usize> = quotient.class_members(class_id).map(|o| o.0).collect();
    let class_lines = quotient.class_lines(class_id);
    let owner_idxs: Vec<usize> = if is_extension {
        all_owner_idxs
            .iter()
            .copied()
            .filter(|&idx| graph.is_residual(&graph.nodes[idx].destination))
            .collect()
    } else {
        all_owner_idxs.clone()
    };

    let mut owner_ids: Vec<String> = Vec::with_capacity(owner_idxs.len());
    let mut anonymous_owner_ids: Vec<String> = Vec::new();
    let mut binding_ids: BTreeSet<String> = BTreeSet::new();
    let mut line_range = LineRange::new();
    let mut max_ordinal = 0usize;
    let mut min_ordinal = usize::MAX;
    for &owner_idx in &owner_idxs {
        let node = &graph.nodes[owner_idx];
        owner_ids.push(node.id.clone());
        if node.declared_bindings.is_empty() {
            anonymous_owner_ids.push(node.id.clone());
        }
        for binding in &node.declared_bindings {
            binding_ids.insert(binding.binding.to_string());
        }
        if let Some(loc) = &node.source_location {
            line_range.expand(loc);
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }
    owner_ids.sort();
    anonymous_owner_ids.sort();

    // Carry the cross-class residual targets as candidate indices.
    // Resolving them to final `proposed_module_id`s here would be
    // wrong: the post-sort renumber rewrites fresh-module ids, and a
    // target candidate may itself be an `extend:`/`merge:` class whose
    // id is not `auto_partition_*`. `emit_proposals` resolves these
    // indices to each target's actual final id after id assignment.
    let residual_target_candidate_idxs: Vec<usize> = edges.residual_targets.into_iter().collect();
    let active_modules_referenced: Vec<String> = edges
        .active_targets
        .iter()
        .map(ModulePath::to_string)
        .collect();

    let extension_owner_ids: Vec<String> = if is_extension {
        let mut ids: Vec<String> = owner_idxs
            .iter()
            .map(|&idx| graph.nodes[idx].id.clone())
            .collect();
        ids.sort();
        ids
    } else {
        Vec::new()
    };
    let proposed_module_id = match (&merge_into, &extends_module_id) {
        (Some(lbls), _) => format!("merge:{}", lbls.join("+")),
        (None, Some(target)) => format!("extend:{target}"),
        (None, None) => format!("auto_partition_{candidate_idx:04}"),
    };
    let size_lines = if is_extension {
        owner_idxs
            .iter()
            .map(|&idx| graph.nodes[idx].line_count())
            .sum()
    } else {
        class_lines
    };
    let AnonymousAddressability {
        addressable,
        unaddressable_owner_ids,
        mut notes,
    } = anonymous_addressability(&owner_idxs, graph, context);
    // Status reflects the cell-graph reality: outgoing constraining
    // edges into other residual cells mean this cell cannot be
    // promoted alone (the reads would route through residual_entry).
    let status = if edges.to_residual > 0 {
        PeelCandidateStatus::BlockedResidualDependency
    } else {
        PeelCandidateStatus::PeelableNow
    };
    // Landability derives from the SAME predicate as `status` (plus
    // anonymous addressability) — a blocked-residual proposal cannot
    // be landed alone, so it must not claim `landable_today`.
    if status == PeelCandidateStatus::BlockedResidualDependency {
        notes.push(
            "reads other residual cells; land the referenced cells first or together".to_string(),
        );
    }
    let landable_today = addressable && status == PeelCandidateStatus::PeelableNow;
    ModuleProposal {
        proposed_module_id,
        owner_ids,
        binding_ids: binding_ids.into_iter().collect(),
        anonymous_statement_owner_ids: anonymous_owner_ids,
        size_lines_estimate: size_lines,
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        internal_edges: edges.internal,
        edges_to_other_residual_cells: edges.to_residual,
        other_residual_cells_referenced: Vec::new(),
        residual_target_candidate_idxs,
        edges_to_active_modules: edges.to_active,
        active_modules_referenced,
        unaddressable_anonymous_owner_ids: unaddressable_owner_ids,
        landability_notes: notes,
        status,
        landable_today,
        extends_module_id,
        extension_owner_ids,
        merge_into,
    }
}

#[derive(Debug, Clone)]
struct AnonymousAddressability {
    addressable: bool,
    unaddressable_owner_ids: Vec<String>,
    notes: Vec<String>,
}

fn anonymous_addressability(
    owner_idxs: &[usize],
    graph: &OwnerGraphReport,
    context: &ProposalContext,
) -> AnonymousAddressability {
    let mut unaddressable_owner_ids = Vec::new();
    let mut notes = BTreeSet::<String>::new();
    for &idx in owner_idxs {
        let node = &graph.nodes[idx];
        if !node.declared_bindings.is_empty() {
            continue;
        }
        if matches!(
            node.statement_kind,
            StatementKind::Import | StatementKind::Export
        ) {
            unaddressable_owner_ids.push(node.id.clone());
            notes.insert(
                "contains import/export owner(s) that are advisory only for spec edits".to_string(),
            );
            continue;
        }
        if let Some(addressable_ids) = &context.addressable_anonymous_owner_ids
            && !addressable_ids.contains(&node.id)
        {
            unaddressable_owner_ids.push(node.id.clone());
            notes.insert(
                "contains anonymous owner(s) whose full-AST selector is ambiguous or unavailable"
                    .to_string(),
            );
        }
    }
    unaddressable_owner_ids.sort();
    AnonymousAddressability {
        addressable: unaddressable_owner_ids.is_empty(),
        unaddressable_owner_ids,
        notes: notes.into_iter().collect(),
    }
}

fn status_counts(proposals: &[ModuleProposal]) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for proposal in proposals {
        *counts
            .entry(status_key(proposal.status).to_string())
            .or_insert(0) += 1;
    }
    counts
}

fn diagnostic_counts(diagnostics: &[ProposalDiagnostic]) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for diagnostic in diagnostics {
        *counts
            .entry(diagnostic_reason_key(diagnostic.reason).to_string())
            .or_insert(0) += 1;
    }
    counts
}

fn size_distributions(proposals: &[ModuleProposal]) -> ProposalSizeDistributions {
    ProposalSizeDistributions {
        by_members: bucket_counts(proposals, |proposal| proposal.owner_ids.len(), size_bucket),
        by_lines: bucket_counts(
            proposals,
            |proposal| proposal.size_lines_estimate,
            size_bucket,
        ),
    }
}

fn bucket_counts(
    proposals: &[ModuleProposal],
    value: fn(&ModuleProposal) -> usize,
    bucket: fn(usize) -> &'static str,
) -> Vec<ProposalSizeBucketCount> {
    const SIZE_BUCKETS: &[&str] = &[
        "0", "1", "2", "3-5", "6-10", "11-20", "21-50", "51-100", "101-250", "251-500", "501-1000",
        ">1000",
    ];
    let mut counts: BTreeMap<&'static str, (usize, usize)> = BTreeMap::new();
    for proposal in proposals {
        let entry = counts.entry(bucket(value(proposal))).or_default();
        entry.0 += 1;
        if proposal.landable_today {
            entry.1 += 1;
        }
    }
    SIZE_BUCKETS
        .iter()
        .filter_map(|bucket| {
            counts
                .get(bucket)
                .map(|(count, landable_count)| ProposalSizeBucketCount {
                    bucket: (*bucket).to_string(),
                    count: *count,
                    landable_count: *landable_count,
                })
        })
        .collect()
}

fn size_bucket(value: usize) -> &'static str {
    match value {
        0 => "0",
        1 => "1",
        2 => "2",
        3..=5 => "3-5",
        6..=10 => "6-10",
        11..=20 => "11-20",
        21..=50 => "21-50",
        51..=100 => "51-100",
        101..=250 => "101-250",
        251..=500 => "251-500",
        501..=1000 => "501-1000",
        _ => ">1000",
    }
}

fn status_key(status: PeelCandidateStatus) -> &'static str {
    match status {
        PeelCandidateStatus::PeelableNow => "peelable_now",
        PeelCandidateStatus::BlockedResidualDependency => "blocked_residual_dependency",
    }
}

fn diagnostic_reason_key(reason: ProposalDiagnosticReason) -> &'static str {
    match reason {
        ProposalDiagnosticReason::ExceedsSizeCap => "exceeds_size_cap",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{DepKind, StatementKind};
    use report_fixtures::{
        active_owner, atomic_edge, atomic_unit_for, claims, graph_of, module_ref, no_claims, owner,
        owner_edge, residual_owner,
    };

    #[test]
    fn residual_atomic_units_become_singleton_proposals() {
        let a = residual_owner("a", 1, &["a"], 10);
        let b = residual_owner("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose(&graph, &no_claims(), 10_000).unwrap();
        assert_eq!(report.residual_owner_count, 2);
        assert_eq!(report.proposals.len(), 2);
        assert!(report.proposals.iter().all(|p| p.owner_ids.len() == 1));
        assert!(report.proposals.iter().all(|p| p.landable_today));
        assert_eq!(
            report.status_counts,
            BTreeMap::from([("peelable_now".to_string(), 2)]),
        );
        assert_eq!(
            report.size_distributions.by_members,
            vec![ProposalSizeBucketCount {
                bucket: "1".to_string(),
                count: 2,
                landable_count: 2,
            }],
        );
        assert_eq!(
            report.size_distributions.by_lines,
            vec![ProposalSizeBucketCount {
                bucket: "6-10".to_string(),
                count: 2,
                landable_count: 2,
            }],
        );
    }

    // A constraining owner edge between two residual cells that the
    // quotient does NOT merge (no atomic-DAG edge, so pass-3
    // reachability never contracts them and the greedy refuses
    // orphan↔orphan merges) leaves the source cell blocked: landing it
    // alone would route the read through `residual_entry` and trip the
    // realizability gate. `landable_today` must agree with `status` —
    // a `blocked_residual_dependency` proposal is never landable.
    #[test]
    fn cross_residual_cell_edges_block_landability() {
        let a = residual_owner("a", 1, &["a"], 10);
        let b = residual_owner("b", 2, &["b"], 10);
        let edges = vec![owner_edge("e1", "a", "b", DepKind::EagerUse, true)];
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            edges,
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose(&graph, &no_claims(), 10_000).unwrap();
        let blocked = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["a".to_string()])
            .expect("singleton proposal for a");
        assert_eq!(
            blocked.status,
            PeelCandidateStatus::BlockedResidualDependency
        );
        assert_eq!(blocked.edges_to_other_residual_cells, 1);
        assert!(!blocked.landable_today, "{blocked:?}");
        assert!(
            blocked
                .landability_notes
                .iter()
                .any(|note| note.contains("other residual cells")),
            "expected cross-residual note: {blocked:?}",
        );
        let peelable = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["b".to_string()])
            .expect("singleton proposal for b");
        assert_eq!(peelable.status, PeelCandidateStatus::PeelableNow);
        assert!(peelable.landable_today, "{peelable:?}");
        assert_eq!(
            report.status_counts,
            BTreeMap::from([
                ("blocked_residual_dependency".to_string(), 1),
                ("peelable_now".to_string(), 1),
            ]),
        );
        // Size buckets count only the landable proposal as landable.
        assert_eq!(
            report.size_distributions.by_members,
            vec![ProposalSizeBucketCount {
                bucket: "1".to_string(),
                count: 2,
                landable_count: 1,
            }],
        );
    }

    // `docs/cli.md`, `docs/design.md` and the `debundle_intake` skill tell
    // consumers to key on these wire names and the `status` spelling; the typed
    // tests read the struct, so a serde rename would not reach them.
    #[test]
    fn blocked_proposal_serializes_the_keys_consumers_read() {
        let a = residual_owner("a", 1, &["a"], 10);
        let b = residual_owner("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![owner_edge("e1", "a", "b", DepKind::EagerUse, true)],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let json = serde_json::to_value(propose(&graph, &no_claims(), 10_000).unwrap()).unwrap();
        let proposal = |binding: &str| {
            json["proposals"]
                .as_array()
                .unwrap()
                .iter()
                .find(|p| p["binding_ids"] == serde_json::json!([binding]))
                .unwrap_or_else(|| panic!("no proposal for `{binding}`: {json:#}"))
        };
        let (blocked, peelable) = (proposal("a"), proposal("b"));
        assert_eq!(blocked["status"], "blocked_residual_dependency");
        assert_eq!(peelable["status"], "peelable_now");
        assert_eq!(blocked["owner_ids"], serde_json::json!(["a"]));
        assert_eq!(
            blocked["other_residual_cells_referenced"],
            serde_json::json!([peelable["proposed_module_id"]]),
        );
    }

    #[test]
    fn extension_proposal_serializes_extension_owner_ids() {
        let a = active_owner("a", 1, &["a"], 10, "ui/x");
        let b = residual_owner("b", 2, &["b"], 5);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![owner_edge("e1", "b", "a", DepKind::EagerUse, true)],
            vec![
                atomic_unit_for("atomic:0", &[&b]),
                atomic_unit_for("atomic:1", &[&a]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let json =
            serde_json::to_value(propose(&graph, &claims(&[("a", "ui/x")]), 10_000).unwrap())
                .unwrap();
        assert_eq!(json["proposals"][0]["extends_module_id"], "ui/x");
        assert_eq!(
            json["proposals"][0]["extension_owner_ids"],
            serde_json::json!(["b"]),
        );
    }

    #[test]
    fn edges_to_active_modules_count_outgoing_to_active_claims() {
        let a = active_owner("a", 1, &["a"], 10, "ui/x");
        let b = residual_owner("b", 2, &["b"], 10);
        let edges = vec![owner_edge("e1", "b", "a", DepKind::EagerUse, true)];
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            edges,
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:1", "atomic:0")],
        );
        let report = propose(&graph, &claims(&[("a", "ui/x")]), 10_000).unwrap();
        let proposal = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["b".to_string()])
            .expect("b proposal");
        assert_eq!(proposal.edges_to_active_modules, 1);
        assert_eq!(proposal.active_modules_referenced, vec!["ui/x".to_string()],);
    }

    #[test]
    fn cross_reference_to_extension_class_resolves_to_an_emitted_proposal_id() {
        // Regression: cross-class residual references must resolve to a
        // `proposed_module_id` that actually exists among the emitted
        // proposals — including when the target candidate is an
        // EXTENSION class (whose id is the extended module path, not an
        // `auto_partition_*` string).
        //
        // Shape:
        // - `foo` is claimed for `features/foo` (active module). It is
        //   the absorption target.
        // - `bridge` is a residual owner whose only cross-module edge
        //   is into `features/foo`. The greedy absorbs `bridge`'s
        //   orphan class into `features/foo`, producing an extension
        //   class that contains a residual-origin owner.
        // - `consumer` is a residual owner with a constraining edge to
        //   `bridge` AND a constraining edge into a SECOND active module
        //   `features/baz`. Touching two modules makes the greedy
        //   refuse to absorb `consumer`, so it survives as its own
        //   residual class. Crucially there is no atomic-DAG edge
        //   `consumer → bridge`, so the seed quotient's pass-3
        //   reachability does NOT fold them into one class — the
        //   owner-level constraining edge `consumer → bridge` survives
        //   as a cross-class residual edge.
        //
        // `consumer`'s proposal therefore records a cross-class residual
        // reference whose target candidate is the `features/foo`
        // extension. The buggy emitter formatted that reference as
        // `auto_partition_<candidate_idx>` — an id no emitted proposal
        // carries — yielding a dangling reference.
        let foo = active_owner("foo", 1, &["foo"], 10, "features/foo");
        let baz = active_owner("baz", 2, &["baz"], 10, "features/baz");
        let bridge = residual_owner("bridge", 3, &["bridge"], 10);
        let consumer = residual_owner("consumer", 4, &["consumer"], 10);
        let graph = graph_of(
            vec![foo.clone(), baz.clone(), bridge.clone(), consumer.clone()],
            vec![
                // bridge depends on the `features/foo` module -> absorbed.
                owner_edge("e1", "bridge", "foo", DepKind::EagerUse, true),
                // consumer depends on bridge (cross-class residual edge)
                // and on the `features/baz` module (second module ->
                // greedy refuses to absorb consumer).
                owner_edge("e2", "consumer", "bridge", DepKind::EagerUse, true),
                owner_edge("e3", "consumer", "baz", DepKind::EagerUse, true),
            ],
            vec![
                atomic_unit_for("atomic:0", &[&foo]),
                atomic_unit_for("atomic:1", &[&baz]),
                atomic_unit_for("atomic:2", &[&bridge]),
                atomic_unit_for("atomic:3", &[&consumer]),
            ],
            // Only the bridge -> foo reachability edge. No
            // consumer -> bridge atomic edge: pass-3 must not fold
            // consumer and bridge into one class.
            vec![atomic_edge("atomic_edge:0", "atomic:2", "atomic:0")],
        );
        let report = propose(
            &graph,
            &claims(&[("foo", "features/foo"), ("baz", "features/baz")]),
            10_000,
        )
        .unwrap();

        // The scenario must actually produce the `features/foo`
        // extension carrying the residual-origin `bridge`, plus a
        // distinct `consumer` proposal that references it cross-class.
        let extension = report
            .proposals
            .iter()
            .find(|p| {
                p.extends_module_id.as_deref() == Some("features/foo")
                    && p.binding_ids.contains(&"bridge".to_string())
            })
            .unwrap_or_else(|| {
                panic!("fixture must extend `features/foo` with the residual `bridge`: {report:#?}")
            });
        // The extension's id is the `extend:<module path>` form — NOT
        // an `auto_partition_*` string. This is what makes the buggy
        // `auto_partition_<idx>` cross-reference dangling.
        assert_eq!(
            extension.proposed_module_id, "extend:features/foo",
            "extension proposal id must be the `extend:` form: {extension:#?}",
        );

        let consumer = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["consumer".to_string()])
            .unwrap_or_else(|| {
                panic!("fixture must produce a standalone `consumer` proposal: {report:#?}")
            });
        // `consumer` reads `bridge`, which lives in the `features/foo`
        // extension — so its sole cross-class residual reference must
        // resolve to the extension's actual id, `features/foo`.
        assert_eq!(
            consumer.other_residual_cells_referenced,
            vec!["extend:features/foo".to_string()],
            "consumer's cross-reference must resolve to the extension's real id \
             (`extend:features/foo`), not a dangling `auto_partition_*`: {report:#?}",
        );
        // The cross-class residual reference blocks `consumer`:
        // status and landability are one predicate.
        assert_eq!(
            consumer.status,
            PeelCandidateStatus::BlockedResidualDependency
        );
        assert!(!consumer.landable_today, "{consumer:?}");

        // Belt and suspenders: every cross-reference any proposal emits
        // must name a `proposed_module_id` that actually exists among
        // the emitted proposals (no dangling references anywhere).
        let emitted_ids: BTreeSet<&str> = report
            .proposals
            .iter()
            .map(|p| p.proposed_module_id.as_str())
            .collect();
        for proposal in &report.proposals {
            for referenced in &proposal.other_residual_cells_referenced {
                assert!(
                    emitted_ids.contains(referenced.as_str()),
                    "cross-reference `{referenced}` must name an emitted proposal id, but none \
                     carries it (emitted ids: {emitted_ids:?}); report: {report:#?}",
                );
            }
        }
    }

    #[test]
    fn merge_proposals_use_spec_module_labels_not_generated_target_files() {
        let a = owner("a", 1, &["a"], 10, module_ref("domains/system/ids"));
        let b = owner("b", 2, &["b"], 10, module_ref("domains/system/id_helpers"));
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![
                owner_edge("e1", "a", "b", DepKind::EagerUse, true),
                owner_edge("e2", "b", "a", DepKind::EagerUse, true),
            ],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose(&graph, &no_claims(), 10_000).unwrap();
        let proposal = report
            .proposals
            .iter()
            .find(|proposal| proposal.merge_into.is_some())
            .expect("merge proposal");
        assert_eq!(
            proposal.merge_into.as_ref().unwrap(),
            &vec![
                "domains/system/id_helpers".to_string(),
                "domains/system/ids".to_string(),
            ],
        );
    }

    #[test]
    fn two_owners_of_one_module_reached_by_different_routes_do_not_self_merge() {
        // Two owners of ONE spec module (same destination key ->
        // `spec_module_groups` contracts them into one class). Owner
        // `a` resolves its identity via an active claim
        // (`domains/system/ids`); owner `b` has no claim and resolves
        // via the module table. Both routes yield the one canonical
        // `ModulePath` (the wire carries one interned key and the table
        // holds one canonical path), so the class collapses to a single
        // label and emits NO `merge_into` self-merge.
        let dest = module_ref("domains/system/ids");
        let a = owner("a", 1, &["a"], 10, dest.clone());
        let b = owner("b", 2, &["b"], 10, dest.clone());
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![
                owner_edge("e1", "a", "b", DepKind::EagerUse, true),
                owner_edge("e2", "b", "a", DepKind::EagerUse, true),
            ],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose(&graph, &claims(&[("a", "domains/system/ids")]), 10_000).unwrap();
        assert!(
            report.proposals.iter().all(|p| p.merge_into.is_none()),
            "expected no self-merge proposal from two spellings of one module: {report:#?}",
        );
    }

    #[test]
    fn non_residual_owner_with_missing_destination_is_clean_error_not_panic() {
        // A non-residual owner whose `destination` key is absent from
        // the module table (`quotient.nodes`) models a truncated /
        // version-skewed `owner_graph.json` deserialized from disk by
        // `propose_from_files`. `is_residual` returns false for an
        // unknown key, so the owner survives the residual filter and
        // reaches `active_module_label`. The proposer must surface a
        // clean `Err` (not panic) so the CLI (`debundle modules
        // propose`) reports it through `anyhow` rather than aborting.
        let a = owner("a", 1, &["a"], 10, module_ref("features/foo"));
        let mut graph = graph_of(
            vec![a.clone()],
            vec![],
            vec![atomic_unit_for("atomic:0", &[&a])],
            vec![],
        );
        // Drop the owner's destination from the module table to
        // simulate the malformed report.
        graph
            .quotient
            .nodes
            .retain(|entry| entry.key != a.destination);
        assert!(graph.module(&a.destination).is_none());

        let result = propose(&graph, &no_claims(), 10_000);
        let err = result.expect_err("missing destination must be a clean error, not a panic");
        let message = format!("{err:#}");
        assert!(
            message.contains("malformed owner graph") && message.contains(&a.id),
            "error must name the malformed report and the offending owner: {message}",
        );
    }

    #[test]
    fn anonymous_import_export_proposals_stay_visible_but_not_landable() {
        let mut import = residual_owner("owner:import", 1, &[], 1);
        import.statement_kind = StatementKind::Import;
        let graph = graph_of(
            vec![import.clone()],
            vec![],
            vec![atomic_unit_for("atomic:0", &[&import])],
            vec![],
        );
        let report = propose(&graph, &no_claims(), 10_000).unwrap();
        let proposal = report.proposals.first().expect("anonymous proposal");
        assert_eq!(
            proposal.anonymous_statement_owner_ids,
            vec!["owner:import".to_string()],
        );
        assert!(!proposal.landable_today);
        assert_eq!(
            proposal.unaddressable_anonymous_owner_ids,
            vec!["owner:import".to_string()],
        );
        assert!(
            proposal
                .landability_notes
                .iter()
                .any(|note| note.contains("import/export")),
            "expected import/export note: {proposal:?}",
        );
    }

    #[test]
    fn full_ast_addressable_anonymous_proposals_remain_landable() {
        let mut effect = residual_owner("owner:effect", 1, &[], 1);
        effect.statement_kind = StatementKind::SideEffect;
        let graph = graph_of(
            vec![effect.clone()],
            vec![],
            vec![atomic_unit_for("atomic:0", &[&effect])],
            vec![],
        );
        let report = propose_with_context(
            &graph,
            &no_claims(),
            10_000,
            ProposalContext {
                addressable_anonymous_owner_ids: Some(BTreeSet::from(["owner:effect".to_string()])),
            },
        )
        .unwrap();
        let proposal = report.proposals.first().expect("anonymous proposal");
        assert!(proposal.landable_today, "{proposal:?}");
        assert!(proposal.unaddressable_anonymous_owner_ids.is_empty());
        assert!(proposal.landability_notes.is_empty());
    }

    // Owners that individually exceed the size cap surface as
    // `ExceedsSizeCap` diagnostics rather than proposals. The seed
    // quotient's size-cap gate refuses to merge the two owners into
    // a closure (combined 20 lines > cap=5), so each oversized
    // singleton class becomes its own diagnostic.
    #[test]
    fn oversized_singletons_become_size_cap_diagnostics() {
        let a = residual_owner("a", 1, &["a"], 10);
        let b = residual_owner("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![owner_edge("e1", "a", "b", DepKind::EagerUse, true)],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let report = propose(&graph, &no_claims(), 5).unwrap();
        assert!(
            report.proposals.is_empty(),
            "oversized owners should not appear as proposals: {report:#?}",
        );
        let binding_sets: Vec<Vec<String>> = report
            .diagnostics
            .iter()
            .map(|diagnostic| diagnostic.binding_ids.clone())
            .collect();
        assert_eq!(
            binding_sets,
            vec![vec!["a".to_string()], vec!["b".to_string()]],
            "each oversized singleton class should surface as its own diagnostic",
        );
    }
}
