//! CLI-facing peel proposer that **annotates** the analyzer's
//! SSOT factorize cells with spec-tree context (active claims) and
//! cell-graph metrics.
//!
//! The certifying proposal algorithm itself lives in
//! `analysis::factorize` and runs at owner-graph build time. The
//! resulting `FactorizeReport` rides inside
//! `OwnerGraphReport.factorize`. This crate reads those precomputed
//! certified cells and adds:
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

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{
    FactorizeCell, FactorizeDiagnosticReason, OwnerGraphReport, PeelCandidateStatus,
    RESIDUAL_ENTRY_MODULE_ID,
};
use spec_modules::load_active_claims;

#[derive(Debug, Clone)]
pub struct PeelFactorizeOptions {
    pub owner_graph_path: PathBuf,
    pub modules_root: PathBuf,
    /// Hard ceiling (in summed source-line counts) per emitted
    /// proposal. Frontiers exceeding the cap appear as diagnostics.
    pub size_cap_lines: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PeelFactorizeReport {
    pub proposals: Vec<FactorizeProposal>,
    pub diagnostics: Vec<FactorizeDiagnosticReport>,
    pub size_cap_lines: usize,
    pub residual_owner_count: usize,
    pub active_claimed_binding_count: usize,
    /// Counts by analyzer verdict status for certified proposals.
    /// Keys use the report's stable snake_case status spelling.
    pub status_counts: BTreeMap<String, usize>,
    /// Counts by diagnostic reason. Diagnostics are not module
    /// assignments that can be landed as-is.
    pub diagnostic_counts: BTreeMap<String, usize>,
    /// Proposal size histograms. Each bucket includes total count
    /// plus how many proposals in the bucket are landable today.
    pub size_distributions: FactorizeSizeDistributions,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeSizeDistributions {
    pub by_members: Vec<FactorizeSizeBucketCount>,
    pub by_lines: Vec<FactorizeSizeBucketCount>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeSizeBucketCount {
    pub bucket: String,
    pub count: usize,
    pub landable_count: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeProposal {
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
    pub size_members: usize,
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
    /// Edges from this cell to active-claimed bindings. Safe:
    /// active modules materialize before residual_entry, so reads
    /// to them don't cycle. Informational.
    pub edges_to_active_modules: usize,
    /// Active module paths this cell's outgoing constraining edges
    /// target (deduplicated).
    pub active_modules_referenced: Vec<String>,
    /// Should be empty for certified proposals. Kept for defensive
    /// compatibility with older reports.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cycle_blocker_owner_ids: Vec<String>,
    /// Analyzer verdict for this proposal's final owner set.
    /// Certified proposals should be `PeelableNow`.
    pub status: PeelCandidateStatus,
    /// Mirrors the materializer predicate; certified proposals are
    /// `true`.
    pub landable_today: bool,
    /// When this proposal is **extending an existing active
    /// module** (i.e. the analyzer's supernode-aware factorize
    /// emitted an `extends_module_id` on the underlying cell),
    /// this carries the active module's id — passed through from
    /// `FactorizeCell::extends_module_id`. `None` for fresh-module
    /// proposals.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extends_module_id: Option<String>,
    /// Loose owner ids (residual today) the analyzer's supernode-
    /// aware factorize identified as the extension's additions to
    /// the existing module. Empty for fresh-module proposals. Pass-
    /// through from `FactorizeCell::extension_owner_ids`.
    pub extension_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeDiagnosticReport {
    pub diagnostic_id: String,
    pub owner_ids: Vec<String>,
    pub binding_ids: Vec<String>,
    pub size_lines_estimate: usize,
    pub size_members: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub status: PeelCandidateStatus,
    pub reason: FactorizeDiagnosticReason,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cycle_blocker_owner_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub active_modules_referenced: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extends_module_id: Option<String>,
}

pub fn analyze_peel_factorize(options: &PeelFactorizeOptions) -> Result<PeelFactorizeReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    let claims = load_active_claims(&options.modules_root)?;
    Ok(factorize(&graph, &claims, options.size_cap_lines))
}

pub fn factorize(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, String>,
    size_cap_lines: usize,
) -> PeelFactorizeReport {
    let owner_index: HashMap<&str, usize> = graph
        .nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.id.as_str(), i))
        .collect();

    // Residual scope mirrors the analyzer's SSOT residual definition:
    // owners whose post-spec destination is the implicit
    // `<residual_entry>` (ModuleId::ResidualEntry), including anonymous
    // side-effect statements.
    let residual: BTreeSet<usize> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| node.destination.id == RESIDUAL_ENTRY_MODULE_ID)
        .map(|(i, _)| i)
        .collect();

    // Owner -> active module path (for any owner whose binding lives
    // in an active YAML). Multi-binding owners take the first match's
    // module — in practice owners after Bucket-F split have one
    // binding each, so the choice is unambiguous.
    let owner_to_active_module: HashMap<usize, String> = graph
        .nodes
        .iter()
        .enumerate()
        .filter_map(|(i, node)| {
            node.declared_bindings
                .iter()
                .find_map(|b| active_claims.get(b.binding.as_str()))
                .map(|path| (i, path.clone()))
        })
        .collect();

    // Analyzer cells are certified proposals. Defensively move any
    // legacy non-landable cell into diagnostics instead of surfacing
    // it as a proposal.
    let mut diagnostics: Vec<FactorizeDiagnosticReport> = diagnostics_from_analyzer(graph);
    let mut cells: Vec<(Cell, Verdict)> = Vec::new();
    for cell in &graph.factorize.cells {
        if cell.landable_today && cell.status == PeelCandidateStatus::PeelableNow {
            cells.push((
                cell_from_factorize_cell(cell, graph, &owner_index),
                Verdict {
                    status: cell.status,
                    landable_today: cell.landable_today,
                    cycle_blocker_owner_ids: cell.cycle_blocker_owner_ids.clone(),
                },
            ));
        } else {
            diagnostics.push(diagnostic_from_legacy_cell(diagnostics.len(), cell));
        }
    }
    // Per-cell edge accounting. We walk every constraining edge
    // once, classifying the source-cell / target-cell pair into:
    // - internal (same cell, residual)            → cell.internal_edges
    // - inter-residual (different residual cells) → cell.edges_to_other_residual_cells
    // - cell → active claim                       → cell.edges_to_active_modules
    let mut residual_constraining_edges: Vec<(usize, usize)> = Vec::new();
    let mut edges_to_active: Vec<(usize, String)> = Vec::new();
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

    let proposals = emit_proposals(
        &cells,
        &residual_constraining_edges,
        &edges_to_active,
        graph,
    );
    let status_counts = status_counts(&proposals);
    let diagnostic_counts = diagnostic_counts(&diagnostics);
    let size_distributions = size_distributions(&proposals);
    PeelFactorizeReport {
        proposals,
        diagnostics,
        size_cap_lines,
        residual_owner_count: residual.len(),
        active_claimed_binding_count: active_claims.len(),
        status_counts,
        diagnostic_counts,
        size_distributions,
    }
}

/// Translate the analyzer-side `FactorizeCell` shape (string owner
/// ids + SSOT verdict) into the CLI's internal `Cell` shape (owner
/// indices into `graph.nodes` + cached line count). `FactorizeCell`
/// entries whose `owner_ids` don't resolve via `owner_index` get
/// silently dropped — that shouldn't happen for any well-formed
/// report but stays defensive against report-shape drift.
fn cell_from_factorize_cell(
    cell: &FactorizeCell,
    graph: &OwnerGraphReport,
    owner_index: &HashMap<&str, usize>,
) -> Cell {
    let owners: BTreeSet<usize> = cell
        .owner_ids
        .iter()
        .filter_map(|id| owner_index.get(id.as_str()).copied())
        .collect();
    let lines = owners
        .iter()
        .map(|&i| owner_line_count(&graph.nodes[i]))
        .sum();
    let extension_owner_idxs: BTreeSet<usize> = cell
        .extension_owner_ids
        .iter()
        .filter_map(|id| owner_index.get(id.as_str()).copied())
        .collect();
    Cell {
        owners,
        lines,
        extends_module_id: cell.extends_module_id.clone(),
        extension_owner_idxs,
    }
}

fn diagnostics_from_analyzer(graph: &OwnerGraphReport) -> Vec<FactorizeDiagnosticReport> {
    graph
        .factorize
        .diagnostics
        .iter()
        .map(|diagnostic| FactorizeDiagnosticReport {
            diagnostic_id: diagnostic.diagnostic_id.clone(),
            owner_ids: diagnostic.owner_ids.clone(),
            binding_ids: diagnostic
                .binding_ids
                .iter()
                .map(|a| a.to_string())
                .collect(),
            size_lines_estimate: diagnostic.size_lines_estimate,
            size_members: diagnostic.size_members,
            source_line_range: diagnostic.source_line_range,
            ordinal_span: diagnostic.ordinal_span,
            status: diagnostic.status,
            reason: diagnostic.reason,
            cycle_blocker_owner_ids: diagnostic.cycle_blocker_owner_ids.clone(),
            active_modules_referenced: diagnostic.active_modules_referenced.clone(),
            extends_module_id: diagnostic.extends_module_id.clone(),
        })
        .collect()
}

fn diagnostic_from_legacy_cell(idx: usize, cell: &FactorizeCell) -> FactorizeDiagnosticReport {
    FactorizeDiagnosticReport {
        diagnostic_id: format!("legacy_factorize_cell_{idx:04}"),
        owner_ids: cell.owner_ids.clone(),
        binding_ids: cell.binding_ids.iter().map(|a| a.to_string()).collect(),
        size_lines_estimate: cell.size_lines_estimate,
        size_members: cell.size_members,
        source_line_range: cell.source_line_range,
        ordinal_span: cell.ordinal_span,
        status: cell.status,
        reason: FactorizeDiagnosticReason::NoExactRepair,
        cycle_blocker_owner_ids: cell.cycle_blocker_owner_ids.clone(),
        active_modules_referenced: cell.active_modules_referenced.clone(),
        extends_module_id: cell.extends_module_id.clone(),
    }
}

#[derive(Debug, Clone)]
struct Cell {
    owners: BTreeSet<usize>,
    lines: usize,
    /// Pass-through from `FactorizeCell::extends_module_id` — the
    /// analyzer-side cell's supernode target, if any.
    extends_module_id: Option<String>,
    /// Pass-through from `FactorizeCell::extension_owner_ids`,
    /// translated to owner indices.
    extension_owner_idxs: BTreeSet<usize>,
}

/// Per-cell gate result from the analyzer's certified
/// `FactorizeCell`.
#[derive(Debug, Clone)]
struct Verdict {
    status: PeelCandidateStatus,
    landable_today: bool,
    cycle_blocker_owner_ids: Vec<String>,
}

fn owner_line_count(node: &analysis::OwnerGraphNodeReport) -> usize {
    node.source_location
        .as_ref()
        .map(|loc| {
            loc.end_line
                .saturating_sub(loc.start_line)
                .saturating_add(1)
        })
        .unwrap_or(0)
}

struct ProposalContext<'a> {
    graph: &'a OwnerGraphReport,
    residual_edges: &'a [(usize, usize)],
    active_edges: &'a [(usize, String)],
    owner_to_cell: HashMap<usize, usize>,
}

fn emit_proposals(
    cells: &[(Cell, Verdict)],
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, String)],
    graph: &OwnerGraphReport,
) -> Vec<FactorizeProposal> {
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (cell_idx, (cell, _)) in cells.iter().enumerate() {
        for &owner in &cell.owners {
            owner_to_cell.insert(owner, cell_idx);
        }
    }

    let ctx = ProposalContext {
        graph,
        residual_edges,
        active_edges,
        owner_to_cell,
    };

    let mut proposals: Vec<FactorizeProposal> = cells
        .iter()
        .enumerate()
        .map(|(cell_idx, (cell, verdict))| build_proposal(cell_idx, cell, verdict, &ctx))
        .collect();

    // Residual-dependency depth sort with source-line tie-break.
    // Certified analyzer output normally has no outgoing residual
    // constraining edges; this still keeps legacy/synthetic reports
    // deterministic.
    let depths = compute_topo_depths(cells.len(), residual_edges, &ctx.owner_to_cell);
    let mut indexed: Vec<(usize, FactorizeProposal)> = proposals.drain(..).enumerate().collect();
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

    // After topo-sort the cells are renumbered; rebuild the original
    // cell_idx → new_idx map so cross-references inside the
    // `other_residual_cells_referenced` lists point at the right
    // post-sort module IDs.
    let new_id_for: HashMap<usize, usize> = indexed
        .iter()
        .enumerate()
        .map(|(new_idx, (orig_idx, _))| (*orig_idx, new_idx))
        .collect();
    let mut out: Vec<FactorizeProposal> = indexed.into_iter().map(|(_, p)| p).collect();
    let mut fresh_counter = 0usize;
    for proposal in out.iter_mut() {
        if proposal.extends_module_id.is_none() {
            proposal.proposed_module_id = format!("auto_partition_{fresh_counter:04}");
            fresh_counter += 1;
        }
        proposal.other_residual_cells_referenced = proposal
            .other_residual_cells_referenced
            .iter()
            .filter_map(|old_id| {
                let old_idx: usize = old_id.strip_prefix("auto_partition_")?.parse().ok()?;
                new_id_for
                    .get(&old_idx)
                    .map(|i| format!("auto_partition_{i:04}"))
            })
            .collect();
    }
    out
}

fn compute_topo_depths(
    cell_count: usize,
    residual_edges: &[(usize, usize)],
    owner_to_cell: &HashMap<usize, usize>,
) -> Vec<usize> {
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); cell_count];
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
            continue;
        };
        if cs != ct {
            adj[cs].insert(ct);
        }
    }
    let mut depths = vec![None; cell_count];
    fn dfs(node: usize, adj: &[BTreeSet<usize>], depths: &mut [Option<usize>]) -> usize {
        if let Some(d) = depths[node] {
            return d;
        }
        // Mark as in-progress with depth 0. If a legacy/synthetic
        // report contains an inter-cell cycle, the sort remains
        // deterministic instead of recursing forever.
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
    for i in 0..cell_count {
        dfs(i, &adj, &mut depths);
    }
    depths.into_iter().map(|d| d.unwrap_or(0)).collect()
}

fn build_proposal(
    cell_idx: usize,
    cell: &Cell,
    verdict: &Verdict,
    ctx: &ProposalContext,
) -> FactorizeProposal {
    let mut owner_ids: Vec<String> = Vec::with_capacity(cell.owners.len());
    let mut anonymous_owner_ids: Vec<String> = Vec::new();
    let mut binding_ids: BTreeSet<String> = BTreeSet::new();
    let mut start_line = usize::MAX;
    let mut end_line = 0usize;
    let mut have_loc = false;
    let mut max_ordinal = 0usize;
    let mut min_ordinal = usize::MAX;
    for &owner_idx in &cell.owners {
        let node = &ctx.graph.nodes[owner_idx];
        owner_ids.push(node.id.clone());
        if node.declared_bindings.is_empty() {
            anonymous_owner_ids.push(node.id.clone());
        }
        for binding in &node.declared_bindings {
            binding_ids.insert(binding.binding.to_string());
        }
        if let Some(loc) = &node.source_location {
            have_loc = true;
            start_line = start_line.min(loc.start_line);
            end_line = end_line.max(loc.end_line);
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }
    owner_ids.sort();
    anonymous_owner_ids.sort();

    let mut internal = 0usize;
    let mut to_residual = 0usize;
    let mut residual_targets: BTreeSet<usize> = BTreeSet::new();
    for &(s, t) in ctx.residual_edges {
        let (Some(&cs), Some(&ct)) = (ctx.owner_to_cell.get(&s), ctx.owner_to_cell.get(&t)) else {
            continue;
        };
        if cs == cell_idx && ct == cell_idx {
            internal += 1;
        } else if cs == cell_idx {
            to_residual += 1;
            residual_targets.insert(ct);
        }
    }
    let other_residual_cells_referenced: Vec<String> = residual_targets
        .into_iter()
        .map(|idx| format!("auto_partition_{idx:04}"))
        .collect();

    let mut to_active = 0usize;
    let mut active_targets: BTreeSet<String> = BTreeSet::new();
    for (source_owner, module_path) in ctx.active_edges {
        if ctx.owner_to_cell.get(source_owner) == Some(&cell_idx) {
            to_active += 1;
            active_targets.insert(module_path.clone());
        }
    }
    let active_modules_referenced: Vec<String> = active_targets.into_iter().collect();

    // `status`, `landable_today`, and blocker lists come straight
    // from the analyzer's SSOT verdict on this cell
    // (computed once via
    // `peelability::evaluate_peel_candidate` at owner-graph
    // build time). The CLI used to recompute them from the JSON
    // shape, which drifted from the predicate on edges through
    // pre-existing entry exports (the recompute treated those as
    // residual_entry cycles even though entry mediates them).
    let cycle_blocker_owner_ids = verdict.cycle_blocker_owner_ids.clone();
    let extension_owner_ids: Vec<String> = {
        let mut ids: Vec<String> = cell
            .extension_owner_idxs
            .iter()
            .map(|&idx| ctx.graph.nodes[idx].id.clone())
            .collect();
        ids.sort();
        ids
    };
    let proposed_module_id = match &cell.extends_module_id {
        Some(target) => format!("extend:{target}"),
        None => format!("auto_partition_{cell_idx:04}"),
    };
    FactorizeProposal {
        proposed_module_id,
        owner_ids,
        binding_ids: binding_ids.into_iter().collect(),
        anonymous_statement_owner_ids: anonymous_owner_ids,
        size_lines_estimate: cell.lines,
        size_members: cell.owners.len(),
        source_line_range: if have_loc {
            Some([start_line, end_line])
        } else {
            None
        },
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        internal_edges: internal,
        edges_to_other_residual_cells: to_residual,
        other_residual_cells_referenced,
        edges_to_active_modules: to_active,
        active_modules_referenced,
        cycle_blocker_owner_ids,
        status: verdict.status,
        landable_today: verdict.landable_today,
        extends_module_id: cell.extends_module_id.clone(),
        extension_owner_ids,
    }
}

fn status_counts(proposals: &[FactorizeProposal]) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for proposal in proposals {
        *counts
            .entry(status_key(proposal.status).to_string())
            .or_insert(0) += 1;
    }
    counts
}

fn diagnostic_counts(diagnostics: &[FactorizeDiagnosticReport]) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for diagnostic in diagnostics {
        *counts
            .entry(diagnostic_reason_key(diagnostic.reason).to_string())
            .or_insert(0) += 1;
    }
    counts
}

fn size_distributions(proposals: &[FactorizeProposal]) -> FactorizeSizeDistributions {
    FactorizeSizeDistributions {
        by_members: bucket_counts(proposals, |proposal| proposal.size_members, member_bucket),
        by_lines: bucket_counts(
            proposals,
            |proposal| proposal.size_lines_estimate,
            line_bucket,
        ),
    }
}

fn bucket_counts(
    proposals: &[FactorizeProposal],
    value: fn(&FactorizeProposal) -> usize,
    bucket: fn(usize) -> &'static str,
) -> Vec<FactorizeSizeBucketCount> {
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
                .map(|(count, landable_count)| FactorizeSizeBucketCount {
                    bucket: (*bucket).to_string(),
                    count: *count,
                    landable_count: *landable_count,
                })
        })
        .collect()
}

fn member_bucket(value: usize) -> &'static str {
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

fn line_bucket(value: usize) -> &'static str {
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
        PeelCandidateStatus::BlockedCycle => "blocked_cycle",
        PeelCandidateStatus::BlockedResidualDependency => "blocked_residual_dependency",
    }
}

fn diagnostic_reason_key(reason: FactorizeDiagnosticReason) -> &'static str {
    match reason {
        FactorizeDiagnosticReason::ExceedsSizeCap => "exceeds_size_cap",
        FactorizeDiagnosticReason::NoExactRepair => "no_exact_repair",
        FactorizeDiagnosticReason::ActiveModuleConflict => "active_module_conflict",
        FactorizeDiagnosticReason::RepeatedFrontier => "repeated_frontier",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_atoms::Atom;

    use analysis::{
        BindingReport, DepKind, FactorizeCell, FactorizeReport, ModuleReportRef,
        OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphPeelabilityReport,
        OwnerGraphQuotientReport, OwnerGraphReport, PeelCandidateStatus, Purity,
        RESIDUAL_ENTRY_LABEL, SourceLocation, StatementKind, StatementOrdinal,
    };
    use spec::DEFAULT_RESIDUAL_MODULE_PATH;

    fn binding(name: &str) -> BindingReport {
        BindingReport {
            binding: name.into(),
            export_name: name.into(),
        }
    }

    fn module_ref(label: &str) -> ModuleReportRef {
        // Match the production `module_key` shape: the implicit
        // residual entry's id is `RESIDUAL_ENTRY_MODULE_ID`, not
        // the path-style `DEFAULT_RESIDUAL_MODULE_PATH` label some
        // test fixtures use as a catch-all sentinel.
        let is_residual = label == DEFAULT_RESIDUAL_MODULE_PATH || label == RESIDUAL_ENTRY_LABEL;
        ModuleReportRef {
            id: if is_residual {
                RESIDUAL_ENTRY_MODULE_ID.to_string()
            } else {
                label.to_string()
            },
            label: label.to_string(),
            residual: is_residual,
            index: None,
            target_file: None,
        }
    }

    fn owner(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
    ) -> OwnerGraphNodeReport {
        owner_at(
            id,
            ordinal_value,
            bindings,
            lines,
            DEFAULT_RESIDUAL_MODULE_PATH,
        )
    }

    fn owner_in_active_module(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        module_path: &str,
    ) -> OwnerGraphNodeReport {
        owner_at(id, ordinal_value, bindings, lines, module_path)
    }

    fn owner_at(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        destination_label: &str,
    ) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal_value),
            source_location: Some(SourceLocation {
                source_path: "x.js".to_string(),
                start_line: ordinal_value * 100,
                end_line: ordinal_value * 100 + lines.saturating_sub(1),
            }),
            declared_bindings: bindings.iter().map(|b| binding(b)).collect(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: module_ref(destination_label),
        }
    }

    fn edge(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        constrains: bool,
    ) -> OwnerGraphEdgeReport {
        edge_for_binding(id, source, target, kind, constrains, None)
    }

    fn edge_for_binding(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        constrains: bool,
        binding: Option<&str>,
    ) -> OwnerGraphEdgeReport {
        OwnerGraphEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kind: kind,
            binding: binding.map(Atom::from),
            statement_ordinal: StatementOrdinal(0),
            constrains_init_order: constrains,
        }
    }

    fn graph_with_cells(
        nodes: Vec<OwnerGraphNodeReport>,
        edges: Vec<OwnerGraphEdgeReport>,
        cells: Vec<FactorizeCell>,
    ) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "x".to_string(),
            nodes,
            edges,
            quotient: OwnerGraphQuotientReport {
                nodes: vec![],
                edges: vec![],
                sccs: vec![],
            },
            peelability: OwnerGraphPeelabilityReport {
                residual_destinations: vec![],
                minimal_peel_sets: vec![],
                residual_owner_horizon: vec![],
                evaluated_owner_sets: vec![],
            },
            factorize: FactorizeReport {
                size_cap_lines: 10_000,
                residual_owner_count: nodes_residual_count(&[]),
                cells,
                diagnostics: Vec::new(),
            },
        }
    }

    fn nodes_residual_count(_nodes: &[OwnerGraphNodeReport]) -> usize {
        // Stub: the field is informational on the analyzer side;
        // tests don't assert on it.
        0
    }

    /// Build a `FactorizeCell` mirroring the shape the analyzer
    /// would emit for a given owner set. `proposed_module_id`,
    /// `binding_ids`, `anonymous_statement_owner_ids`, and
    /// `source_line_range` are derived from the matching nodes
    /// so callers only need to spell out the owner partition and
    /// the status verdict.
    fn cell(
        id: &str,
        owner_ids: &[&str],
        nodes: &[OwnerGraphNodeReport],
        status: PeelCandidateStatus,
    ) -> FactorizeCell {
        cell_with_blockers(id, owner_ids, nodes, status, &[])
    }

    fn cell_with_blockers(
        id: &str,
        owner_ids: &[&str],
        nodes: &[OwnerGraphNodeReport],
        status: PeelCandidateStatus,
        cycle_blockers: &[&str],
    ) -> FactorizeCell {
        let owners: Vec<String> = owner_ids.iter().map(|s| s.to_string()).collect();
        let mut bindings: Vec<Atom> = Vec::new();
        let mut anonymous: Vec<String> = Vec::new();
        let mut size_lines = 0usize;
        let mut start = usize::MAX;
        let mut end = 0usize;
        let mut have_loc = false;
        let mut min_ord = usize::MAX;
        let mut max_ord = 0usize;
        for owner_id in &owners {
            let node = nodes
                .iter()
                .find(|n| &n.id == owner_id)
                .unwrap_or_else(|| panic!("cell owner {owner_id} not in nodes"));
            if node.declared_bindings.is_empty() {
                anonymous.push(node.id.clone());
            }
            for b in &node.declared_bindings {
                bindings.push(b.binding.clone());
            }
            if let Some(loc) = &node.source_location {
                have_loc = true;
                start = start.min(loc.start_line);
                end = end.max(loc.end_line);
                size_lines += loc.end_line + 1 - loc.start_line;
            }
            min_ord = min_ord.min(node.statement_ordinal.0);
            max_ord = max_ord.max(node.statement_ordinal.0);
        }
        bindings.sort();
        bindings.dedup();
        anonymous.sort();
        let landable = matches!(status, PeelCandidateStatus::PeelableNow);
        FactorizeCell {
            proposed_module_id: id.to_string(),
            owner_ids: owners,
            binding_ids: bindings,
            anonymous_statement_owner_ids: anonymous,
            size_lines_estimate: size_lines,
            size_members: owner_ids.len(),
            source_line_range: have_loc.then_some([start, end]),
            ordinal_span: max_ord.saturating_sub(min_ord),
            status,
            landable_today: landable,
            cycle_blocker_owner_ids: cycle_blockers.iter().map(|s| s.to_string()).collect(),
            active_modules_referenced: Vec::new(),
            extends_module_id: None,
            extension_owner_ids: Vec::new(),
        }
    }

    fn no_claims() -> BTreeMap<String, String> {
        BTreeMap::new()
    }

    #[test]
    fn empty_factorize_cells_produce_no_proposals() {
        let graph = graph_with_cells(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
            vec![],
        );
        let report = factorize(&graph, &no_claims(), 10_000);
        assert_eq!(report.residual_owner_count, 2);
        assert!(report.proposals.is_empty());
    }

    #[test]
    fn singleton_cells_pass_through_with_landable_verdict() {
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["a"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
            ),
            cell(
                "auto_partition_0001",
                &["b"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
            ),
        ];
        let graph = graph_with_cells(nodes, vec![], cells);
        let report = factorize(&graph, &no_claims(), 10_000);
        assert_eq!(report.proposals.len(), 2);
        assert!(report.proposals.iter().all(|p| p.size_members == 1));
        assert!(report.proposals.iter().all(|p| p.landable_today));
        assert_eq!(
            report.status_counts,
            BTreeMap::from([("peelable_now".to_string(), 2)]),
        );
        assert_eq!(
            report.size_distributions.by_members,
            vec![FactorizeSizeBucketCount {
                bucket: "1".to_string(),
                count: 2,
                landable_count: 2,
            }],
        );
        assert_eq!(
            report.size_distributions.by_lines,
            vec![FactorizeSizeBucketCount {
                bucket: "6-10".to_string(),
                count: 2,
                landable_count: 2,
            }],
        );
    }

    #[test]
    fn inter_cell_constraining_edges_are_counted_per_proposal() {
        // Two cells (a, b) with a single constraining edge a → b.
        // Cell 0 (a) reports edges_to_other_residual_cells=1
        // pointing at cell 1 (b). Cell 1 reports 0. The combined
        // closure would be landable, so use a low size cap to keep
        // the cells separate for this metric test.
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let edges = vec![edge("e1", "a", "b", DepKind::EagerUse, true)];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["a"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
            ),
            cell(
                "auto_partition_0001",
                &["b"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
            ),
        ];
        let graph = graph_with_cells(nodes, edges, cells);
        let report = factorize(&graph, &no_claims(), 15);
        let by_binding = |b: &str| -> &FactorizeProposal {
            report
                .proposals
                .iter()
                .find(|p| p.binding_ids.contains(&b.to_string()))
                .expect("cell")
        };
        assert_eq!(by_binding("a").edges_to_other_residual_cells, 1);
        assert_eq!(
            by_binding("a").other_residual_cells_referenced,
            vec![by_binding("b").proposed_module_id.clone()],
        );
        assert_eq!(by_binding("b").edges_to_other_residual_cells, 0);
    }

    #[test]
    fn edges_to_active_modules_count_outgoing_to_active_claims() {
        // Residual cell {b} has a constraining edge to `a`, an
        // owner whose destination is the active module `ui/x`.
        // Expected: edges_to_active_modules=1,
        // active_modules_referenced=["ui/x"].
        let nodes = vec![
            owner_in_active_module("a", 1, &["a"], 10, "ui/x"),
            owner("b", 2, &["b"], 10),
        ];
        let edges = vec![edge("e1", "b", "a", DepKind::EagerUse, true)];
        let cells = vec![cell(
            "auto_partition_0000",
            &["b"],
            &nodes,
            PeelCandidateStatus::PeelableNow,
        )];
        let graph = graph_with_cells(nodes, edges, cells);
        let claims = BTreeMap::from([("a".to_string(), "ui/x".to_string())]);
        let report = factorize(&graph, &claims, 10_000);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(report.proposals[0].edges_to_active_modules, 1);
        assert_eq!(
            report.proposals[0].active_modules_referenced,
            vec!["ui/x".to_string()],
        );
    }

    #[test]
    fn legacy_analyzer_cycle_blocked_cell_is_reported_as_diagnostic() {
        let nodes = vec![
            owner("consumer", 1, &["consumer"], 10),
            owner("blocker", 2, &["blocker"], 5),
        ];
        let cells = vec![
            cell_with_blockers(
                "auto_partition_0000",
                &["consumer"],
                &nodes,
                PeelCandidateStatus::BlockedCycle,
                &["blocker"],
            ),
            cell(
                "auto_partition_0001",
                &["blocker"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
            ),
        ];
        let graph = graph_with_cells(nodes, vec![], cells);
        let report = factorize(&graph, &no_claims(), 10_000);
        assert!(
            !report
                .proposals
                .iter()
                .any(|p| p.binding_ids.contains(&"consumer".to_string())),
            "blocked legacy cell must not be a proposal: {report:#?}",
        );
        let consumer = report
            .diagnostics
            .iter()
            .find(|p| p.binding_ids.contains(&"consumer".to_string()))
            .expect("consumer diagnostic");
        assert_eq!(consumer.status, PeelCandidateStatus::BlockedCycle);
        assert_eq!(
            consumer.cycle_blocker_owner_ids,
            vec!["blocker".to_string()]
        );
        assert_eq!(consumer.reason, FactorizeDiagnosticReason::NoExactRepair);
        assert_eq!(
            report.status_counts,
            BTreeMap::from([("peelable_now".to_string(), 1)]),
        );
        assert_eq!(
            report.diagnostic_counts,
            BTreeMap::from([("no_exact_repair".to_string(), 1)]),
        );
    }
}
