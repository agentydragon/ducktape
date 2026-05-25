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
//! Commit 4 of `plans/peel_proposer_contraction_model.md` unifies cell
//! discovery and seed-quotient construction into a single, gated
//! contraction protocol. The factorize pipeline is now:
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
//!      materializes each as a `FactorizeProposal`.
//!
//! Pre-commit-4 the renderer ran off a parallel `Vec<CellClassRecord>`
//! produced by `proposal_cells_from_atomic_graph`. Both that helper
//! and its `Cell` IR are gone; the quotient is now the single source
//! of truth for "which owners are in which proposed class."

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{FactorizeDiagnosticReason, LineRange, OwnerGraphReport, PeelCandidateStatus};
use spec_modules::load_active_claims;

use crate::quotient::{
    ClassId, OwnerIdx, QuotientGraph, SeedContractionRejected, SpecModuleGroup,
    build_seed_quotient, greedy_merge_to_convergence,
};

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
    /// Counts by proposal verdict status.
    /// Keys use the report's stable snake_case status spelling.
    pub status_counts: BTreeMap<String, usize>,
    /// Counts by diagnostic reason. Diagnostics are not module
    /// assignments that can be landed as-is.
    pub diagnostic_counts: BTreeMap<String, usize>,
    /// Proposal size histograms. Each bucket includes total count
    /// plus how many proposals in the bucket are landable today.
    pub size_distributions: FactorizeSizeDistributions,
    /// Per-contraction rejection diagnostics from the seeding
    /// protocol (`peel::quotient::build_seed_quotient`). Empty on
    /// well-formed input; populated when the spec declares an
    /// unrealizable owner grouping. Skipped from JSON output when
    /// empty so existing well-formed fixtures stay byte-identical.
    /// See `plans/peel_proposer_contraction_model.md`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub seed_rejections: Vec<SeedContractionRejected>,
    /// Wall-clock breakdown of the planner phases. Populated by
    /// `analyze_peel_factorize`; left absent when the caller invoked
    /// `factorize` directly (existing tests construct the report
    /// inline). Serialised under `timings` so downstream automation
    /// can correlate plan-work wall-clock with the same phase labels
    /// used by `lower_chunk`'s timings tree.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timings: Option<PlanWorkTimingsReport>,
}

/// Plan-work phase timings shipped alongside the proposals so
/// profiling can map back to source phases without an external
/// profiler. Mirrors the shape of `pipeline.json`'s timings section:
/// duration values plus integer milliseconds for grep-friendly
/// inspection.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PlanWorkTimingsReport {
    pub durations: BTreeMap<String, Duration>,
    pub durations_ms: BTreeMap<String, u128>,
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
    /// Proposal verdict for this closed atomic-unit owner set.
    pub status: PeelCandidateStatus,
    /// `true` for atomic-DAG-closed proposal cells.
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

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeDiagnosticReport {
    pub diagnostic_id: String,
    pub owner_ids: Vec<String>,
    pub binding_ids: Vec<String>,
    pub size_lines_estimate: usize,
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
    let analyze_started = Instant::now();
    let mut timings = PlanWorkTimings::default();
    let graph: OwnerGraphReport = time_phase!(timings, "load_owner_graph", {
        serde_json::from_str(
            &fs::read_to_string(&options.owner_graph_path)
                .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
        )
        .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?
    });
    let claims = time_phase!(timings, "load_active_claims", {
        load_active_claims(&options.modules_root)?
    });
    let mut report = factorize_with_timings(&graph, &claims, options.size_cap_lines, &mut timings);
    report.timings = Some(timings.into_report(analyze_started.elapsed()));
    Ok(report)
}

/// Wall-clock collector used by `analyze_peel_factorize` and the
/// per-phase planner internals it threads timings into. Kept private
/// so the unit tests that construct `PeelFactorizeReport` inline
/// don't have to pay attention to it. The serialised shape lives in
/// `PlanWorkTimingsReport`.
#[derive(Debug, Default)]
struct PlanWorkTimings {
    durations: BTreeMap<String, Duration>,
}

impl PlanWorkTimings {
    fn add(&mut self, name: &str, duration: Duration) {
        *self.durations.entry(name.to_string()).or_default() += duration;
    }

    fn into_report(self, total: Duration) -> PlanWorkTimingsReport {
        let mut durations = self.durations;
        durations.insert("total".to_string(), total);
        let durations_ms: BTreeMap<String, u128> = durations
            .iter()
            .map(|(k, v)| (k.clone(), v.as_millis()))
            .collect();
        PlanWorkTimingsReport {
            durations,
            durations_ms,
        }
    }
}

macro_rules! time_phase {
    ($timings:expr, $name:expr, $body:block) => {{
        let phase_started = std::time::Instant::now();
        let value = $body;
        $timings.add($name, phase_started.elapsed());
        value
    }};
}
pub(crate) use time_phase;

pub fn factorize(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, String>,
    size_cap_lines: usize,
) -> PeelFactorizeReport {
    let mut timings = PlanWorkTimings::default();
    factorize_with_timings(graph, active_claims, size_cap_lines, &mut timings)
}

fn factorize_with_timings(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, String>,
    size_cap_lines: usize,
    timings: &mut PlanWorkTimings,
) -> PeelFactorizeReport {
    let owner_index: HashMap<&str, usize> = time_phase!(timings, "build_owner_index", {
        graph
            .nodes
            .iter()
            .enumerate()
            .map(|(i, node)| (node.id.as_str(), i))
            .collect()
    });

    let residual: BTreeSet<usize> = time_phase!(timings, "collect_residual_owners", {
        graph
            .nodes
            .iter()
            .enumerate()
            .filter(|(_, node)| node.destination.residual)
            .map(|(i, _)| i)
            .collect()
    });

    let owner_to_active_module: HashMap<usize, String> =
        time_phase!(timings, "build_owner_to_active_module", {
            graph
                .nodes
                .iter()
                .enumerate()
                .filter_map(|(i, node)| {
                    if node.destination.residual {
                        return None;
                    }
                    let path = node
                        .declared_bindings
                        .iter()
                        .find_map(|b| active_claims.get(b.binding.as_str()))
                        .cloned()
                        .or_else(|| node.destination.target_file.clone())
                        .unwrap_or_else(|| node.destination.label.clone());
                    Some((i, path))
                })
                .collect()
        });

    // Spec-module groups: every active-claimed module's owners
    // pre-contracted into one class. Used by `build_seed_quotient`'s
    // pass 2 to seed pre-existing-module classes; the greedy then
    // absorbs orphans into them.
    let spec_modules = time_phase!(timings, "spec_module_groups", { spec_module_groups(graph) });
    let (mut quotient, seed_rejections) = time_phase!(timings, "build_seed_quotient", {
        build_seed_quotient(
            graph,
            &graph.atomic_graph.nodes,
            &spec_modules,
            size_cap_lines,
        )
    });
    time_phase!(timings, "greedy_merge_to_convergence", {
        let _greedy_steps = greedy_merge_to_convergence(&mut quotient);
    });

    // Per-class edge accounting. Walk every constraining owner
    // edge and classify (source-class, target-class) into:
    // - internal (same class, residual-only)
    // - inter-residual (different non-pre-existing-module classes)
    // - residual → pre-existing-module class
    // Edges originating from non-residual owners are skipped (the
    // edge-accounting surface mirrors today's cell-edge-accounting
    // semantics; non-residual edges are part of the spec module's
    // internal initialization, not relevant to peel proposals).
    let (residual_constraining_edges, edges_to_active) =
        time_phase!(timings, "classify_constraining_edges", {
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
            (residual_constraining_edges, edges_to_active)
        });

    // Per-class label (pre-existing module path). Built from the
    // active-claimed owners surviving in each class; if a class
    // contains owners from two distinct active modules, the labels
    // are collected and surfaced as a `merge_into` later.
    let class_to_labels: BTreeMap<ClassId, BTreeSet<String>> =
        time_phase!(timings, "build_class_to_labels", {
            let mut class_to_labels: BTreeMap<ClassId, BTreeSet<String>> = BTreeMap::new();
            for (idx, _) in graph.nodes.iter().enumerate() {
                let Some(label) = owner_to_active_module.get(&idx) else {
                    continue;
                };
                let c = quotient.class_of(OwnerIdx(idx));
                class_to_labels.entry(c).or_default().insert(label.clone());
            }
            class_to_labels
        });

    let proposals = time_phase!(timings, "emit_proposals", {
        emit_proposals(
            &quotient,
            &class_to_labels,
            &residual_constraining_edges,
            &edges_to_active,
            graph,
            size_cap_lines,
        )
    });
    let diagnostics = time_phase!(timings, "collect_size_cap_diagnostics", {
        collect_size_cap_diagnostics(&quotient, graph, size_cap_lines)
    });
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
        seed_rejections,
        timings: None,
    }
}

/// Spec-module groups derived from `graph.nodes` destinations.
/// Owners with a non-residual destination are grouped by
/// destination id; residual owners stay out (they'll be singletons).
fn spec_module_groups(graph: &OwnerGraphReport) -> Vec<SpecModuleGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for node in &graph.nodes {
        if node.destination.residual {
            continue;
        }
        groups
            .entry(node.destination.id.clone())
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

/// Render the quotient's surviving (non-empty, non-residual-only)
/// classes into proposals.
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
/// The catch-all residual class (`destination.id == "residual"`) is
/// excluded — it's never a proposal target. Classes that fail the
/// post-greedy size cap appear as `FactorizeDiagnosticReport`s
/// (computed separately in `collect_size_cap_diagnostics`).
fn emit_proposals(
    quotient: &QuotientGraph,
    class_to_labels: &BTreeMap<ClassId, BTreeSet<String>>,
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, String)],
    graph: &OwnerGraphReport,
    size_cap_lines: usize,
) -> Vec<FactorizeProposal> {
    // Build owner-to-class for every owner. The renderer's edge
    // attribution treats classes as the unit of accounting (the
    // pre-commit-4 code keyed by "cell_idx", a parallel index; the
    // ClassId itself is now the natural key).
    let mut owner_to_class: HashMap<usize, ClassId> = HashMap::new();
    for (i, _) in graph.nodes.iter().enumerate() {
        owner_to_class.insert(i, quotient.class_of(OwnerIdx(i)));
    }

    // Candidate classes: every live class that isn't the residual
    // catch-all and either has owners or carries module labels. We
    // skip classes whose only members are spec-module owners with
    // no residual extensions (= pre-existing modules unchanged by
    // the peel) — those don't represent a spec edit.
    let mut candidate_classes: Vec<ClassId> = Vec::new();
    for c in quotient.iter_classes() {
        if quotient.class_is_residual(c) {
            continue;
        }
        let labels = class_to_labels.get(&c);
        let n_labels = labels.map(|s| s.len()).unwrap_or(0);
        let has_residual_origin = quotient
            .class_members(c)
            .any(|o| graph.nodes[o.0].destination.residual);
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
    // count toward the `auto_partition_NNNN` numbering.
    candidate_classes.retain(|c| quotient.class_lines(*c) <= size_cap_lines);

    // Owner-index → candidate_classes-position. Used by edge-
    // attribution to bucket cross-class edges by candidate id.
    let mut owner_to_candidate: HashMap<usize, usize> = HashMap::new();
    for (idx, &c) in candidate_classes.iter().enumerate() {
        for owner in quotient.class_members(c) {
            owner_to_candidate.insert(owner.0, idx);
        }
    }

    let mut proposals: Vec<FactorizeProposal> = candidate_classes
        .iter()
        .enumerate()
        .map(|(candidate_idx, &class_id)| {
            build_proposal(
                candidate_idx,
                class_id,
                class_to_labels.get(&class_id),
                quotient,
                residual_edges,
                active_edges,
                graph,
                &owner_to_candidate,
            )
        })
        .collect();

    // Residual-dependency depth sort with source-line tie-break.
    let depths = compute_topo_depths(proposals.len(), residual_edges, &owner_to_candidate);
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

    // After topo-sort the candidate indices are renumbered; rebuild
    // the original candidate_idx → new_idx map so cross-references
    // inside `other_residual_cells_referenced` point at the right
    // post-sort module IDs.
    let new_id_for: HashMap<usize, usize> = indexed
        .iter()
        .enumerate()
        .map(|(new_idx, (orig_idx, _))| (*orig_idx, new_idx))
        .collect();
    let mut out: Vec<FactorizeProposal> = indexed.into_iter().map(|(_, p)| p).collect();
    let mut fresh_counter = 0usize;
    for proposal in out.iter_mut() {
        if proposal.extends_module_id.is_none() && proposal.merge_into.is_none() {
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

/// Build a `FactorizeDiagnosticReport(ExceedsSizeCap)` for every
/// surviving class whose combined line count exceeds the cap. The
/// pre-commit-4 cell pipeline computed this from cell closures; the
/// new pipeline reads it from the post-greedy quotient.
fn collect_size_cap_diagnostics(
    quotient: &QuotientGraph,
    graph: &OwnerGraphReport,
    size_cap_lines: usize,
) -> Vec<FactorizeDiagnosticReport> {
    let mut diagnostics = Vec::new();
    for c in quotient.iter_classes() {
        if quotient.class_is_residual(c) {
            continue;
        }
        if quotient.class_lines(c) <= size_cap_lines {
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
        diagnostics.push(FactorizeDiagnosticReport {
            diagnostic_id: format!(
                "diagnostic:{}_{idx:04}",
                diagnostic_reason_key(FactorizeDiagnosticReason::ExceedsSizeCap),
            ),
            owner_ids,
            binding_ids: binding_ids.into_iter().collect(),
            size_lines_estimate: quotient.class_lines(c),
            source_line_range: line_range.into_array(),
            ordinal_span: max_ordinal.saturating_sub(min_ordinal),
            status: PeelCandidateStatus::BlockedResidualDependency,
            reason: FactorizeDiagnosticReason::ExceedsSizeCap,
            cycle_blocker_owner_ids: Vec::new(),
            active_modules_referenced: Vec::new(),
            extends_module_id: None,
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

#[allow(clippy::too_many_arguments)]
fn build_proposal(
    candidate_idx: usize,
    class_id: ClassId,
    labels: Option<&BTreeSet<String>>,
    quotient: &QuotientGraph,
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, String)],
    graph: &OwnerGraphReport,
    owner_to_candidate: &HashMap<usize, usize>,
) -> FactorizeProposal {
    let label_vec: Vec<String> = labels
        .map(|s| s.iter().cloned().collect())
        .unwrap_or_default();
    let is_extension = !label_vec.is_empty();
    let merge_into: Option<Vec<String>> = (label_vec.len() >= 2).then(|| label_vec.clone());
    let extends_module_id: Option<String> = (label_vec.len() == 1)
        .then(|| label_vec[0].clone())
        .or_else(|| {
            // For merge proposals, choose a canonical
            // `extends_module_id` = the first label (sorted). Pre-
            // commit-4 emitted merges this way too.
            (label_vec.len() >= 2).then(|| label_vec[0].clone())
        });

    let all_owner_idxs: Vec<usize> = quotient.class_members(class_id).map(|o| o.0).collect();
    let class_lines = quotient.class_lines(class_id);
    let owner_idxs: Vec<usize> = if is_extension {
        all_owner_idxs
            .iter()
            .copied()
            .filter(|&idx| graph.nodes[idx].destination.residual)
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

    let mut internal = 0usize;
    let mut to_residual = 0usize;
    let mut residual_targets: BTreeSet<usize> = BTreeSet::new();
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_candidate.get(&s), owner_to_candidate.get(&t))
        else {
            continue;
        };
        if cs == candidate_idx && ct == candidate_idx {
            internal += 1;
        } else if cs == candidate_idx {
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
    for (source_owner, module_path) in active_edges {
        if owner_to_candidate.get(source_owner) == Some(&candidate_idx) {
            to_active += 1;
            active_targets.insert(module_path.clone());
        }
    }
    let active_modules_referenced: Vec<String> = active_targets.into_iter().collect();

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
            .map(|&idx| owner_line_count(&graph.nodes[idx]))
            .sum()
    } else {
        class_lines
    };
    FactorizeProposal {
        proposed_module_id,
        owner_ids,
        binding_ids: binding_ids.into_iter().collect(),
        anonymous_statement_owner_ids: anonymous_owner_ids,
        size_lines_estimate: size_lines,
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        internal_edges: internal,
        edges_to_other_residual_cells: to_residual,
        other_residual_cells_referenced,
        edges_to_active_modules: to_active,
        active_modules_referenced,
        cycle_blocker_owner_ids: Vec::new(),
        status: PeelCandidateStatus::PeelableNow,
        landable_today: true,
        extends_module_id,
        extension_owner_ids,
        merge_into,
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
        by_members: bucket_counts(proposals, |proposal| proposal.owner_ids.len(), size_bucket),
        by_lines: bucket_counts(
            proposals,
            |proposal| proposal.size_lines_estimate,
            size_bucket,
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
        AtomicGraphReport, AtomicUnitEdgeReport, AtomicUnitReport, DepKind, ModuleReportRef,
        OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport,
        Purity, SourceLocation, StatementKind, StatementOrdinal,
    };

    use super::super::test_utils;

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
            test_utils::module_ref("logical:residual", true),
        )
    }

    fn owner_in_active_module(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        module_path: &str,
    ) -> OwnerGraphNodeReport {
        owner_at(
            id,
            ordinal_value,
            bindings,
            lines,
            test_utils::module_ref(module_path, false),
        )
    }

    fn owner_at(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        destination: ModuleReportRef,
    ) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal_value),
            source_location: Some(SourceLocation {
                source_path: "x.js".to_string(),
                start_line: ordinal_value * 100,
                end_line: ordinal_value * 100 + lines.saturating_sub(1),
            }),
            declared_bindings: bindings.iter().map(|b| test_utils::binding(b)).collect(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination,
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
            at_init_callee_owner: None,
        }
    }

    fn unit(id: &str, owners: &[&OwnerGraphNodeReport]) -> AtomicUnitReport {
        let mut owner_ids = Vec::new();
        let mut members = Vec::new();
        let mut destinations = BTreeMap::<String, ModuleReportRef>::new();
        let mut line_range = LineRange::new();
        let mut min_ordinal = usize::MAX;
        let mut max_ordinal = 0usize;
        for owner in owners {
            owner_ids.push(owner.id.clone());
            members.extend(owner.declared_bindings.clone());
            destinations.insert(owner.destination.id.clone(), owner.destination.clone());
            if let Some(location) = &owner.source_location {
                line_range.expand(location);
            }
            min_ordinal = min_ordinal.min(owner.statement_ordinal.0);
            max_ordinal = max_ordinal.max(owner.statement_ordinal.0);
        }
        AtomicUnitReport {
            id: id.to_string(),
            owner_ids,
            members,
            anonymous_statement_owner_ids: Vec::new(),
            destinations: destinations.into_values().collect(),
            causes: Vec::new(),
            size_lines_estimate: line_range.size_estimate(),
            source_line_range: line_range.into_array(),
            ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        }
    }

    fn atomic_edge(id: &str, source: &str, target: &str) -> AtomicUnitEdgeReport {
        AtomicUnitEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kinds: vec![DepKind::EagerUse],
            owner_edge_ids: vec![id.replace("atomic", "edge")],
            constrains_init_order: true,
        }
    }

    fn graph_with_atomic_units(
        nodes: Vec<OwnerGraphNodeReport>,
        edges: Vec<OwnerGraphEdgeReport>,
        atomic_units: Vec<AtomicUnitReport>,
        atomic_edges: Vec<AtomicUnitEdgeReport>,
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
            atomic_graph: AtomicGraphReport {
                nodes: atomic_units,
                edges: atomic_edges,
            },
        }
    }

    fn no_claims() -> BTreeMap<String, String> {
        BTreeMap::new()
    }

    #[test]
    fn residual_atomic_units_become_singleton_proposals() {
        let a = owner("a", 1, &["a"], 10);
        let b = owner("b", 2, &["b"], 10);
        let graph = graph_with_atomic_units(
            vec![a.clone(), b.clone()],
            vec![],
            vec![unit("atomic:0", &[&a]), unit("atomic:1", &[&b])],
            vec![],
        );
        let report = factorize(&graph, &no_claims(), 10_000);
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
    fn outgoing_residual_atomic_edges_close_proposals() {
        let a = owner("a", 1, &["a"], 10);
        let b = owner("b", 2, &["b"], 10);
        let edges = vec![edge("e1", "a", "b", DepKind::EagerUse, true)];
        let graph = graph_with_atomic_units(
            vec![a.clone(), b.clone()],
            edges,
            vec![unit("atomic:0", &[&a]), unit("atomic:1", &[&b])],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let report = factorize(&graph, &no_claims(), 10_000);
        assert!(
            report.proposals.iter().any(|p| p.binding_ids
                == vec!["a".to_string(), "b".to_string()]
                && p.landable_today),
            "expected closure proposal containing a and b: {report:#?}",
        );
    }

    #[test]
    fn edges_to_active_modules_count_outgoing_to_active_claims() {
        let a = owner_in_active_module("a", 1, &["a"], 10, "ui/x");
        let b = owner("b", 2, &["b"], 10);
        let edges = vec![edge("e1", "b", "a", DepKind::EagerUse, true)];
        let graph = graph_with_atomic_units(
            vec![a.clone(), b.clone()],
            edges,
            vec![unit("atomic:0", &[&a]), unit("atomic:1", &[&b])],
            vec![atomic_edge("atomic_edge:0", "atomic:1", "atomic:0")],
        );
        let claims = BTreeMap::from([("a".to_string(), "ui/x".to_string())]);
        let report = factorize(&graph, &claims, 10_000);
        let proposal = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["b".to_string()])
            .expect("b proposal");
        assert_eq!(proposal.edges_to_active_modules, 1);
        assert_eq!(proposal.active_modules_referenced, vec!["ui/x".to_string()],);
    }

    // Owners that individually exceed the size cap surface as
    // `ExceedsSizeCap` diagnostics rather than proposals. The seed
    // quotient's size-cap gate refuses to merge the two owners into
    // a closure (combined 20 lines > cap=5), so each oversized
    // singleton class becomes its own diagnostic.
    #[test]
    fn oversized_singletons_become_size_cap_diagnostics() {
        let a = owner("a", 1, &["a"], 10);
        let b = owner("b", 2, &["b"], 10);
        let graph = graph_with_atomic_units(
            vec![a.clone(), b.clone()],
            vec![edge("e1", "a", "b", DepKind::EagerUse, true)],
            vec![unit("atomic:0", &[&a]), unit("atomic:1", &[&b])],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let report = factorize(&graph, &no_claims(), 5);
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
        assert!(
            report
                .diagnostics
                .iter()
                .all(|diagnostic| diagnostic.reason == FactorizeDiagnosticReason::ExceedsSizeCap),
        );
    }
}
