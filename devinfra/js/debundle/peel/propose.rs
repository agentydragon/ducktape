//! CLI-facing peel proposer over the serialized source_unit + atomic DAG report.
//!
//! `debundle run` emits stable graph facts only. This crate computes
//! heuristic peel proposals from `DependencyGraphReport.atomic_graph` on demand and
//! annotates them with spec-tree context (active claims) and cell-graph
//! metrics:
//!
//! - `edges_to_active_modules` / `active_modules_referenced`:
//!   outgoing constraining edges from each cell to active-claimed
//!   binding modules (safe references — active modules materialize
//!   before residual_entry).
//! - `internal_edges`, `edges_to_other_residual_classes`,
//!   `other_residual_classes_referenced`: quotient-class relationship
//!   counts derived from the assignment the analyzer chose.
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
//!   1. `build_seed_quotient` — atomic-group + spec-module +
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
//! The quotient is the single source of truth for "which source units are
//! in which proposed class."

use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{
    DependencyEdgeReport, DependencyGraphInput, DependencyGraphReport, LineRange, SourceUnitClass,
    SourceUnitReport, StatementKind,
};
use anonymous_resolution::addressable_anonymous_statement_source_unit_ids;
use graph_provenance::VerifiedGraphSourceContext;
use source_identity::SourceUnitId;
use spec::ModulePath;
use spec_modules::load_active_claims;
use spec_tree::ModuleTreeScope;

use crate::quotient::{
    ClassId, QuotientGraph, SeedContractionRejected, SourceUnitIndex, SpecModuleGroup,
    build_seed_quotient, greedy_merge_to_convergence,
};

/// Default `--size-cap-lines` for every CLI verb that runs the
/// proposer. One named constant so the planner verbs and the
/// internal rebuild paths can't drift apart.
pub const DEFAULT_SIZE_CAP_LINES: usize = 10_000;

const MAX_OBSERVED_CONSUMER_SOURCE_UNITS: usize = 8;
const MAX_OBSERVED_CONSUMER_MODULES: usize = 8;
const MAX_OBSERVED_CONSUMER_EDGES: usize = 12;

#[derive(Debug, Clone)]
pub struct ProposalOptions {
    pub dependency_graph_path: PathBuf,
    pub modules_root: PathBuf,
    /// Optional tree config that scopes active claims to the graph's chunk.
    pub tree_config: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    /// Source bytes verified by graph preflight for the graph's chunk.
    pub source_context: Option<VerifiedGraphSourceContext>,
    /// Hard ceiling (in summed source-line counts) per emitted
    /// proposal. Frontiers exceeding the cap appear as diagnostics.
    pub size_cap_lines: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProposalsReport {
    pub proposals: Vec<ModuleProposal>,
    pub diagnostics: Vec<ProposalDiagnostic>,
    pub size_cap_lines: usize,
    pub residual_source_unit_count: usize,
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
    /// unrealizable source_unit grouping. Skipped from JSON output when
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
pub struct ObservedConsumers {
    /// Value reads from source units outside this proposal's resulting quotient class.
    pub value_reads: ConsumerEvidence,
    /// Rebinding writes from source units outside this proposal's resulting quotient class.
    pub writes: ConsumerEvidence,
    /// Scheduling constraints and promoted-at-init edges. A promoted read or
    /// write also appears in its value-read or write category.
    pub scheduling_or_promoted_context: ConsumerEvidence,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ConsumerEvidence {
    pub total_source_units: usize,
    pub omitted_source_units: usize,
    pub source_units: Vec<ObservedConsumerSourceUnit>,
    pub total_modules: usize,
    pub omitted_modules: usize,
    pub modules: Vec<String>,
    pub total_edges: usize,
    pub omitted_edges: usize,
    /// Original directed graph edges (`source` consumes or constrains `target`).
    pub edges: Vec<DependencyEdgeReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ObservedConsumerSourceUnit {
    pub source_unit_id: String,
    pub current_module: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ModuleProposal {
    pub proposed_module_id: String,
    pub source_unit_ids: Vec<String>,
    /// Bindings declared by the cell's source_units. Excludes
    /// anonymous side-effect statements, which appear under
    /// `anonymous_statement_source_unit_ids` instead.
    pub binding_ids: Vec<String>,
    /// Bounded incoming graph evidence for the source units this proposal adds.
    /// These observed consumers are context for source review, not inferred owners.
    pub observed_consumers: ObservedConsumers,
    /// Anonymous side-effect statements (source_units with empty
    /// `declared_bindings`) in this cell. Lane workers materialize
    /// these via `anonymous_statements:` entries quoting the
    /// statement's source verbatim — the materializer's cycle
    /// gate counts these statements when determining whether the
    /// cell's promotion would cycle through `residual_entry`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statement_source_unit_ids: Vec<String>,
    pub size_lines_estimate: usize,
    /// `[start_line, end_line]` of the lowest-line and highest-line
    /// source_unit bodies. `None` when none of the cell's source_units have
    /// a `source_location`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub internal_edges: usize,
    /// Edges from this cell to OTHER residual classes. Cycle-risk
    /// edges: promoting this cell to active while the pointed-at
    /// residual classes stay residual would create `<this>` →
    /// `residual_entry` reads, which the cycle gate will reject.
    pub edges_to_other_residual_classes: usize,
    /// Other residual classes (by proposed_module_id) this cell's
    /// outgoing constraining edges target.
    pub other_residual_classes_referenced: Vec<String>,
    /// Pre-resolution carrier for `other_residual_classes_referenced`:
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
    /// Anonymous source_units that the proposer cannot turn into stable
    /// `anonymous_statements:` selectors. The proposal stays visible
    /// for architectural review, but is not directly feedable to a
    /// spec edit while this list is non-empty.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unaddressable_anonymous_source_unit_ids: Vec<String>,
    /// Human-facing reasons a proposal may need manual handling even
    /// when the graph closure itself is useful.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub landability_notes: Vec<String>,
    /// Proposal verdict for this closed atomic-group source_unit set.
    /// `PeelableNow` when the cell has no outgoing edges into other
    /// residual classes; `BlockedResidualDependency` when it does
    /// (promoting it alone would route reads through
    /// `residual_entry`, which the cycle gate rejects — the
    /// referenced classes must land first or together).
    pub status: PeelCandidateStatus,
    /// `true` when the proposal is graph-ready for a spec edit:
    /// `status` is `PeelableNow` (no outgoing constraining edges into
    /// other residual classes) AND every anonymous source unit is addressable.
    /// Selector synthesis and the post-edit gate can still reject it.
    /// `modules apply` rejects rows where this is `false`;
    /// blocked-residual proposals need their closure grown (land the
    /// referenced cells together) or manual co-location first.
    pub landable_today: bool,
    /// When this proposal extends an existing active module, this carries the
    /// active module id. `None` for fresh-module proposals.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extends_module_id: Option<String>,
    /// Loose source_unit ids (residual today) that would be added to
    /// `extends_module_id`. Empty for fresh-module proposals.
    pub extension_source_unit_ids: Vec<String>,
    /// `Some(ids)` if this proposal merges two or more pre-existing
    /// active modules. The list is the active module ids being
    /// merged, in canonical (sorted) order. `None` for non-merge
    /// proposals (fresh module or single-module extension).
    ///
    /// When set, downstream consumers materialize the spec edit
    /// "combine modules A, B (, C, …) into one yaml file." The
    /// proposal's `source_unit_ids` is the union of merged-module source_units
    /// plus any absorbed residual orphans (which also appear in
    /// `extension_source_unit_ids`).
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
    pub source_unit_ids: Vec<String>,
    pub binding_ids: Vec<String>,
    pub size_lines_estimate: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub reason: ProposalDiagnosticReason,
}

pub fn propose_from_files(options: &ProposalOptions) -> Result<ProposalsReport> {
    let input = DependencyGraphInput::read(&options.dependency_graph_path)?;
    propose_from_graph(&input, options)
}

/// [`propose_from_files`] for callers that already hold the validated graph
/// input. Topology is shared across proposal construction and every candidate
/// gate query; report data supplies labels and destinations.
pub fn propose_from_graph(
    input: &DependencyGraphInput,
    options: &ProposalOptions,
) -> Result<ProposalsReport> {
    let graph = input.report();
    let claims = active_claims_from_options(graph, options)?;
    let addressable_anonymous_source_unit_ids =
        if options.source_root.is_some() || options.source_context.is_some() {
            Some(addressable_anonymous_statement_source_unit_ids(
                graph,
                &options.dependency_graph_path,
                &options.modules_root,
                options.source_root.as_deref(),
                options.source_context.as_ref(),
            )?)
        } else {
            None
        };
    propose_with_context(
        input,
        &claims,
        options.size_cap_lines,
        ProposalContext {
            addressable_anonymous_source_unit_ids,
        },
    )
}

pub(crate) fn active_claims_from_options(
    graph: &DependencyGraphReport,
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

pub fn propose(
    input: &DependencyGraphInput,
    active_claims: &BTreeMap<String, ModulePath>,
    size_cap_lines: usize,
) -> Result<ProposalsReport> {
    propose_with_context(
        input,
        active_claims,
        size_cap_lines,
        ProposalContext::default(),
    )
}

#[derive(Debug, Clone, Default)]
struct ProposalContext {
    addressable_anonymous_source_unit_ids: Option<BTreeSet<String>>,
}

fn propose_with_context(
    input: &DependencyGraphInput,
    active_claims: &BTreeMap<String, ModulePath>,
    size_cap_lines: usize,
    context: ProposalContext,
) -> Result<ProposalsReport> {
    let graph = input.report();
    let residual: BTreeSet<usize> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| graph.is_unassigned_or_entry(&node.destination))
        .map(|(i, _)| i)
        .collect();

    let source_unit_to_active_module: HashMap<usize, ModulePath> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| !graph.is_unassigned_or_entry(&node.destination))
        .map(|(i, node)| Ok((i, active_module_label(node, active_claims, graph)?)))
        .collect::<Result<_>>()?;

    // Spec-module groups: every active-claimed module's source_units
    // pre-contracted into one class. Used by `build_seed_quotient`'s
    // pass 2 to seed pre-existing-module classes; the greedy then
    // absorbs orphans into them.
    let spec_modules = spec_module_groups(graph);
    let (mut quotient, seed_rejections) = build_seed_quotient(
        input,
        &graph.atomic_graph.nodes,
        &spec_modules,
        size_cap_lines,
    );
    // Mutates the quotient in place; the returned merge-step list is unused here.
    greedy_merge_to_convergence(&mut quotient);

    // Per-class edge accounting. Walk every constraining source_unit
    // edge and classify (source-class, target-class) into:
    // - internal (same class, residual-only)
    // - inter-residual (different non-pre-existing-module classes)
    // - residual → pre-existing-module class
    // Edges originating from non-residual source_units are skipped: they are
    // part of the spec module's internal initialization, not relevant
    // to peel proposals.
    let mut residual_constraining_edges: Vec<(usize, usize)> = Vec::new();
    let mut edges_to_active: Vec<(usize, ModulePath)> = Vec::new();
    for edge in input.graph().iter_edges() {
        if !edge.reason.constrains_init_order() {
            continue;
        }
        let (source, target) = (edge.from.0, edge.to.0);
        if !residual.contains(&source) || source == target {
            continue;
        }
        if residual.contains(&target) {
            residual_constraining_edges.push((source, target));
        } else if let Some(module_path) = source_unit_to_active_module.get(&target) {
            edges_to_active.push((source, module_path.clone()));
        }
    }

    // Per-class label (pre-existing module path). Built from the
    // active-claimed source_units surviving in each class; if a class
    // contains source_units from two distinct active modules, the labels
    // are collected and surfaced as a `merge_into` later.
    let mut class_to_labels: BTreeMap<ClassId, BTreeSet<ModulePath>> = BTreeMap::new();
    for (idx, _) in graph.nodes.iter().enumerate() {
        let Some(label) = source_unit_to_active_module.get(&idx) else {
            continue;
        };
        let c = quotient.class_of(SourceUnitIndex(idx));
        class_to_labels.entry(c).or_default().insert(label.clone());
    }

    let proposals = emit_proposals(
        &quotient,
        &class_to_labels,
        &residual_constraining_edges,
        &edges_to_active,
        input,
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
        residual_source_unit_count: residual.len(),
        active_claimed_binding_count: active_claims.len(),
        status_counts,
        diagnostic_counts,
        size_distributions,
        seed_rejections,
    })
}

/// The canonical [`ModulePath`] an source_unit's destination resolves to.
///
/// A claimed binding resolves via `active_claims` (the spec authority).
/// Otherwise the destination's canonical path comes straight from the
/// module table — a single source of truth, already normalized, so no
/// chunk-prefix stripping or fallback chain is needed and two source_units of
/// one module can never disagree (the former self-merge bug). A
/// destination key absent from the table is a malformed report
/// (truncated / version-skewed `dependency_graph.json.gz`), surfaced as an
/// `Err` rather than a panic so the CLI reports it cleanly.
pub(crate) fn active_module_label(
    node: &SourceUnitReport,
    active_claims: &BTreeMap<String, ModulePath>,
    graph: &DependencyGraphReport,
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
                "malformed dependency graph: source unit {} destination {} absent from module table",
                node.id, node.destination
            )
        })?
        .path
        .clone())
}

/// Spec-module groups derived from `graph.nodes` destinations.
/// Source units with a non-residual destination are grouped by their
/// (interned) destination key; residual source_units stay out (they'll be
/// singletons).
fn spec_module_groups(graph: &DependencyGraphReport) -> Vec<SpecModuleGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for node in &graph.nodes {
        if graph.is_unassigned_or_entry(&node.destination) {
            continue;
        }
        groups
            .entry(node.destination.0.clone())
            .or_default()
            .push(node.id.clone());
    }
    groups
        .into_iter()
        .map(|(module_id, mut source_unit_ids)| {
            source_unit_ids.sort();
            SpecModuleGroup {
                module_id,
                source_unit_ids,
            }
        })
        .collect()
}

/// The line count the size cap (and `size_lines_estimate`) measures
/// for a class, matching the spec edit the proposal would perform:
///
/// - Unlabeled (fresh-module) class: every source_unit lands in the new
///   module, so the whole class counts (`class_lines`).
/// - Labeled (extension / merge) class: the edit only ADDS the
///   residual-origin source_units to already-active modules, so only those
///   additions count. Measuring the whole class here would reject a
///   10-line extension to a 600-line module as "600 lines".
fn class_proposal_lines(
    quotient: &QuotientGraph,
    graph: &DependencyGraphReport,
    c: ClassId,
    is_extension: bool,
) -> usize {
    if !is_extension {
        return quotient.class_lines(c);
    }
    quotient
        .class_members(c)
        .filter(|o| graph.is_unassigned_or_entry(&graph.nodes[o.0].destination))
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
///   source_units as `extension_source_unit_ids` (the spec edit: "add these
///   source_units to module M"). If the class has no residual-origin
///   source_units, no proposal is emitted (the spec module is already
///   complete).
/// - ≥2 labels: module↔module merge proposal (`merge:M1+M2+…`).
///   Residual-origin source_units ride as `extension_source_unit_ids`.
///
/// Classes that fail the post-greedy size cap appear as
/// `ProposalDiagnostic`s (computed separately in
/// `collect_size_cap_diagnostics`).
fn emit_proposals(
    quotient: &QuotientGraph,
    class_to_labels: &BTreeMap<ClassId, BTreeSet<ModulePath>>,
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, ModulePath)],
    input: &DependencyGraphInput,
    size_cap_lines: usize,
    context: &ProposalContext,
) -> Vec<ModuleProposal> {
    let graph = input.report();
    let source_modules = source_modules_by_id(graph);
    let build_context = ProposalBuildContext {
        proposal_context: context,
        input,
        source_modules: &source_modules,
    };

    // Candidate classes: every live class that either has source_units or
    // carries module labels. We skip classes whose only members are
    // spec-module source_units with no residual extensions (= pre-existing
    // modules unchanged by the peel) — those don't represent a spec
    // edit.
    let mut candidate_classes: Vec<ClassId> = Vec::new();
    for c in quotient.iter_classes() {
        let labels = class_to_labels.get(&c);
        let n_labels = labels.map(|s| s.len()).unwrap_or(0);
        let has_residual_origin = quotient
            .class_members(c)
            .any(|o| graph.is_unassigned_or_entry(&graph.nodes[o.0].destination));
        if n_labels == 0 {
            // Pure residual class. Always emit (fresh-module).
            candidate_classes.push(c);
        } else if n_labels == 1 {
            // Extension: only emit if there are residual-origin
            // source_units to add. A class containing only the
            // already-claimed spec-module source_units is a no-op.
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

    // SourceUnitIndex → candidate_classes-position. Used by edge-
    // attribution to bucket cross-class edges by candidate id.
    let mut source_unit_to_candidate: HashMap<usize, usize> = HashMap::new();
    for (idx, &c) in candidate_classes.iter().enumerate() {
        for source_unit in quotient.class_members(c) {
            source_unit_to_candidate.insert(source_unit.0, idx);
        }
    }

    // Bucket every residual / active edge by source candidate in ONE
    // pass (`O(edges)`, not `O(candidates × edges)`).
    let mut edge_stats: Vec<CandidateEdgeStats> = candidate_classes
        .iter()
        .map(|_| CandidateEdgeStats::default())
        .collect();
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (
            source_unit_to_candidate.get(&s),
            source_unit_to_candidate.get(&t),
        ) else {
            continue;
        };
        if cs == ct {
            edge_stats[cs].internal += 1;
        } else {
            edge_stats[cs].to_residual += 1;
            edge_stats[cs].residual_targets.insert(ct);
        }
    }
    for (source_unit, module_path) in active_edges {
        if let Some(&cs) = source_unit_to_candidate.get(source_unit) {
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
                &build_context,
            )
        })
        .collect();

    // Residual-dependency depth sort with source-line tie-break.
    let depths = compute_topo_depths(proposals.len(), residual_edges, &source_unit_to_candidate);
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
    // (it came from `source_unit_to_candidate`, which only maps candidate-
    // class source_units), so the lookup never misses — a miss would mean a
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
        proposal.other_residual_classes_referenced = referenced;
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

struct ProposalBuildContext<'context, 'graph> {
    proposal_context: &'context ProposalContext,
    input: &'graph DependencyGraphInput,
    source_modules: &'context HashMap<&'graph str, &'graph analysis::ModuleEntry>,
}

fn source_modules_by_id(graph: &DependencyGraphReport) -> HashMap<&str, &analysis::ModuleEntry> {
    let modules_by_key: HashMap<&str, &analysis::ModuleEntry> = graph
        .quotient
        .nodes
        .iter()
        .map(|module| (module.key.as_str(), module))
        .collect();
    graph
        .nodes
        .iter()
        .map(|source_unit| {
            let module = modules_by_key
                .get(source_unit.destination.as_str())
                .copied()
                .expect("source unit destination must exist in graph modules");
            (source_unit.id.as_str(), module)
        })
        .collect()
}

fn observed_consumers_for(
    target_source_unit_idxs: &[usize],
    proposal_class_source_unit_idxs: &[usize],
    input: &DependencyGraphInput,
    source_modules: &HashMap<&str, &analysis::ModuleEntry>,
) -> ObservedConsumers {
    let candidate_source_units: BTreeSet<usize> =
        proposal_class_source_unit_idxs.iter().copied().collect();
    let mut value_reads = Vec::new();
    let mut writes = Vec::new();
    let mut scheduling_or_promoted_context = Vec::new();

    for &target_idx in target_source_unit_idxs {
        for &edge_id in input.graph().in_edges_of(SourceUnitId(target_idx)) {
            let edge = input.graph().edge(edge_id);
            let report = input.edge_report(edge_id);
            if candidate_source_units.contains(&edge.from.0) {
                continue;
            }
            // Keep recorded promotion evidence even when a historical
            // report's callee is unavailable to the canonical topology.
            let promoted = report.role.is_some();
            match edge.reason.kind() {
                DepKind::EagerUse | DepKind::LazyUse | DepKind::HoistedUse => {
                    value_reads.push(report);
                    if promoted {
                        scheduling_or_promoted_context.push(report);
                    }
                }
                DepKind::EagerRebind | DepKind::LazyRebind | DepKind::DeferredRebind => {
                    writes.push(report);
                    if promoted {
                        scheduling_or_promoted_context.push(report);
                    }
                }
                DepKind::Sequenced
                | DepKind::CoDeclaration
                | DepKind::CoLocation
                | DepKind::LocalEffect => {
                    scheduling_or_promoted_context.push(report);
                }
            }
        }
    }

    ObservedConsumers {
        value_reads: consumer_evidence(&value_reads, source_modules),
        writes: consumer_evidence(&writes, source_modules),
        scheduling_or_promoted_context: consumer_evidence(
            &scheduling_or_promoted_context,
            source_modules,
        ),
    }
}

fn consumer_evidence(
    edges: &[&DependencyEdgeReport],
    source_modules: &HashMap<&str, &analysis::ModuleEntry>,
) -> ConsumerEvidence {
    let mut source_units = BTreeMap::new();
    for edge in edges {
        let current_module = source_modules
            .get(edge.source.as_str())
            .expect("dependency edge source must exist in graph nodes")
            .path
            .to_string();
        source_units.insert(
            edge.source.clone(),
            ObservedConsumerSourceUnit {
                source_unit_id: edge.source.clone(),
                current_module,
            },
        );
    }
    let modules: BTreeSet<String> = source_units
        .values()
        .map(|source_unit| source_unit.current_module.clone())
        .collect();
    let mut sorted_edges = edges.to_vec();
    sorted_edges.sort_by(|left, right| left.id.cmp(&right.id));

    let total_source_units = source_units.len();
    let total_modules = modules.len();
    let total_edges = sorted_edges.len();
    ConsumerEvidence {
        total_source_units,
        omitted_source_units: total_source_units.saturating_sub(MAX_OBSERVED_CONSUMER_SOURCE_UNITS),
        source_units: source_units
            .into_values()
            .take(MAX_OBSERVED_CONSUMER_SOURCE_UNITS)
            .collect(),
        total_modules,
        omitted_modules: total_modules.saturating_sub(MAX_OBSERVED_CONSUMER_MODULES),
        modules: modules
            .into_iter()
            .take(MAX_OBSERVED_CONSUMER_MODULES)
            .collect(),
        total_edges,
        omitted_edges: total_edges.saturating_sub(MAX_OBSERVED_CONSUMER_EDGES),
        edges: sorted_edges
            .into_iter()
            .take(MAX_OBSERVED_CONSUMER_EDGES)
            .cloned()
            .collect(),
    }
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
    graph: &DependencyGraphReport,
    size_cap_lines: usize,
) -> Vec<ProposalDiagnostic> {
    let mut diagnostics = Vec::new();
    for c in quotient.iter_classes() {
        if class_proposal_lines(quotient, graph, c, class_has_labels(class_to_labels, c))
            <= size_cap_lines
        {
            continue;
        }
        // Compose the diagnostic from the class's source_units.
        let mut source_unit_ids: Vec<String> = Vec::new();
        let mut binding_ids: BTreeSet<String> = BTreeSet::new();
        let mut line_range = LineRange::new();
        let mut max_ordinal = 0usize;
        let mut min_ordinal = usize::MAX;
        for source_unit in quotient.class_members(c) {
            let node = &graph.nodes[source_unit.0];
            source_unit_ids.push(node.id.clone());
            for binding in &node.declared_bindings {
                binding_ids.insert(binding.binding.to_string());
            }
            if let Some(loc) = &node.source_location {
                line_range.expand(loc);
            }
            min_ordinal = min_ordinal.min(node.statement_ordinal.0);
            max_ordinal = max_ordinal.max(node.statement_ordinal.0);
        }
        source_unit_ids.sort();
        let idx = diagnostics.len();
        diagnostics.push(ProposalDiagnostic {
            diagnostic_id: format!(
                "diagnostic:{}_{idx:04}",
                diagnostic_reason_key(ProposalDiagnosticReason::ExceedsSizeCap),
            ),
            source_unit_ids,
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
    source_unit_to_candidate: &HashMap<usize, usize>,
) -> Vec<usize> {
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); candidate_count];
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (
            source_unit_to_candidate.get(&s),
            source_unit_to_candidate.get(&t),
        ) else {
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
    context: &ProposalBuildContext<'_, '_>,
) -> ModuleProposal {
    let graph = context.input.report();
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

    let all_source_unit_idxs: Vec<usize> = quotient.class_members(class_id).map(|o| o.0).collect();
    let class_lines = quotient.class_lines(class_id);
    let source_unit_idxs: Vec<usize> = if is_extension {
        all_source_unit_idxs
            .iter()
            .copied()
            .filter(|&idx| graph.is_unassigned_or_entry(&graph.nodes[idx].destination))
            .collect()
    } else {
        all_source_unit_idxs.clone()
    };

    let mut source_unit_ids: Vec<String> = Vec::with_capacity(source_unit_idxs.len());
    let mut anonymous_source_unit_ids: Vec<String> = Vec::new();
    let mut binding_ids: BTreeSet<String> = BTreeSet::new();
    let mut line_range = LineRange::new();
    let mut max_ordinal = 0usize;
    let mut min_ordinal = usize::MAX;
    for &source_unit_idx in &source_unit_idxs {
        let node = &graph.nodes[source_unit_idx];
        source_unit_ids.push(node.id.clone());
        if node.class() == SourceUnitClass::AnonymousStatement {
            anonymous_source_unit_ids.push(node.id.clone());
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
    source_unit_ids.sort();
    anonymous_source_unit_ids.sort();

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

    let extension_source_unit_ids: Vec<String> = if is_extension {
        let mut ids: Vec<String> = source_unit_idxs
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
        source_unit_idxs
            .iter()
            .map(|&idx| graph.nodes[idx].line_count())
            .sum()
    } else {
        class_lines
    };
    let AnonymousAddressability {
        addressable,
        unaddressable_source_unit_ids,
        mut notes,
    } = anonymous_addressability(&source_unit_idxs, graph, context.proposal_context);
    // Status reflects the cell-graph reality: outgoing constraining
    // edges into other residual classes mean this cell cannot be
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
            "reads other residual classes; land the referenced classes first or together"
                .to_string(),
        );
    }
    let landable_today = addressable && status == PeelCandidateStatus::PeelableNow;
    ModuleProposal {
        proposed_module_id,
        source_unit_ids,
        binding_ids: binding_ids.into_iter().collect(),
        observed_consumers: observed_consumers_for(
            source_unit_idxs.as_slice(),
            all_source_unit_idxs.as_slice(),
            context.input,
            context.source_modules,
        ),
        anonymous_statement_source_unit_ids: anonymous_source_unit_ids,
        size_lines_estimate: size_lines,
        source_line_range: line_range.into_array(),
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        internal_edges: edges.internal,
        edges_to_other_residual_classes: edges.to_residual,
        other_residual_classes_referenced: Vec::new(),
        residual_target_candidate_idxs,
        edges_to_active_modules: edges.to_active,
        active_modules_referenced,
        unaddressable_anonymous_source_unit_ids: unaddressable_source_unit_ids,
        landability_notes: notes,
        status,
        landable_today,
        extends_module_id,
        extension_source_unit_ids,
        merge_into,
    }
}

#[derive(Debug, Clone)]
struct AnonymousAddressability {
    addressable: bool,
    unaddressable_source_unit_ids: Vec<String>,
    notes: Vec<String>,
}

fn anonymous_addressability(
    source_unit_idxs: &[usize],
    graph: &DependencyGraphReport,
    context: &ProposalContext,
) -> AnonymousAddressability {
    let mut unaddressable_source_unit_ids = Vec::new();
    let mut notes = BTreeSet::<String>::new();
    for &idx in source_unit_idxs {
        let node = &graph.nodes[idx];
        if node.class() == SourceUnitClass::BindingDeclaration {
            continue;
        }
        if matches!(
            node.statement_kind,
            StatementKind::Import | StatementKind::Export
        ) {
            unaddressable_source_unit_ids.push(node.id.clone());
            notes.insert(
                "contains import/export source unit(s) that are advisory only for spec edits"
                    .to_string(),
            );
            continue;
        }
        if let Some(addressable_ids) = &context.addressable_anonymous_source_unit_ids
            && !addressable_ids.contains(&node.id)
        {
            unaddressable_source_unit_ids.push(node.id.clone());
            notes.insert(
                "contains anonymous source unit(s) whose full-AST selector is ambiguous or unavailable"
                    .to_string(),
            );
        }
    }
    unaddressable_source_unit_ids.sort();
    AnonymousAddressability {
        addressable: unaddressable_source_unit_ids.is_empty(),
        unaddressable_source_unit_ids,
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
        by_members: bucket_counts(
            proposals,
            |proposal| proposal.source_unit_ids.len(),
            size_bucket,
        ),
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
    use analysis::StatementKind;
    use analysis_model::DepKind;
    use report_fixtures::{
        active_source_unit, atomic_edge, atomic_group_for, claims, dependency_edge, graph_of,
        module_ref, residual_source_unit, source_unit,
    };

    fn propose_report(
        report: &DependencyGraphReport,
        active_claims: &BTreeMap<String, ModulePath>,
        size_cap_lines: usize,
    ) -> Result<ProposalsReport> {
        let input = DependencyGraphInput::from_report(report.clone())?;
        propose(&input, active_claims, size_cap_lines)
    }

    #[test]
    fn residual_atomic_groups_become_singleton_proposals() {
        let a = residual_source_unit("a", 1, &["a"], 10);
        let b = residual_source_unit("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![],
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose_report(&graph, &BTreeMap::new(), 10_000).unwrap();
        assert_eq!(report.residual_source_unit_count, 2);
        assert_eq!(report.proposals.len(), 2);
        assert!(
            report
                .proposals
                .iter()
                .all(|p| p.source_unit_ids.len() == 1)
        );
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

    // A constraining source-unit edge between two residual classes that the
    // quotient does NOT merge (no atomic-DAG edge, so pass-3
    // reachability never contracts them and the greedy refuses
    // orphan↔orphan merges) leaves the source cell blocked: landing it
    // alone would route the read through `residual_entry` and trip the
    // realizability gate. `landable_today` must agree with `status` —
    // a `blocked_residual_dependency` proposal is never landable.
    #[test]
    fn cross_residual_cell_edges_block_landability() {
        let a = residual_source_unit("a", 1, &["a"], 10);
        let b = residual_source_unit("b", 2, &["b"], 10);
        let edges = vec![dependency_edge("e1", "a", "b", DepKind::EagerUse, true)];
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            edges,
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose_report(&graph, &BTreeMap::new(), 10_000).unwrap();
        let blocked = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec!["a".to_string()])
            .expect("singleton proposal for a");
        assert_eq!(
            blocked.status,
            PeelCandidateStatus::BlockedResidualDependency
        );
        assert_eq!(blocked.edges_to_other_residual_classes, 1);
        assert!(!blocked.landable_today, "{blocked:?}");
        assert!(
            blocked
                .landability_notes
                .iter()
                .any(|note| note.contains("other residual classes")),
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
        let a = residual_source_unit("a", 1, &["a"], 10);
        let b = residual_source_unit("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![dependency_edge("e1", "a", "b", DepKind::EagerUse, true)],
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let json = serde_json::to_value(propose_report(&graph, &BTreeMap::new(), 10_000).unwrap())
            .unwrap();
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
        assert_eq!(blocked["source_unit_ids"], serde_json::json!(["a"]));
        assert_eq!(
            blocked["other_residual_classes_referenced"],
            serde_json::json!([peelable["proposed_module_id"]]),
        );
    }

    #[test]
    fn extension_proposal_serializes_extension_source_unit_ids() {
        let a = active_source_unit("a", 1, &["a"], 10, "ui/x");
        let b = residual_source_unit("b", 2, &["b"], 5);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![dependency_edge("e1", "b", "a", DepKind::EagerUse, true)],
            vec![
                atomic_group_for("atomic:0", &[&b]),
                atomic_group_for("atomic:1", &[&a]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let json = serde_json::to_value(
            propose_report(&graph, &claims(&[("a", "ui/x")]), 10_000).unwrap(),
        )
        .unwrap();
        assert_eq!(json["proposals"][0]["extends_module_id"], "ui/x");
        assert_eq!(
            json["proposals"][0]["extension_source_unit_ids"],
            serde_json::json!(["b"]),
        );
    }

    #[test]
    fn edges_to_active_modules_count_outgoing_to_active_claims() {
        let a = active_source_unit("a", 1, &["a"], 10, "ui/x");
        let b = residual_source_unit("b", 2, &["b"], 10);
        let edges = vec![dependency_edge("e1", "b", "a", DepKind::EagerUse, true)];
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            edges,
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:1", "atomic:0")],
        );
        let report = propose_report(&graph, &claims(&[("a", "ui/x")]), 10_000).unwrap();
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
        // - `bridge` is a residual source_unit whose only cross-module edge
        //   is into `features/foo`. The greedy absorbs `bridge`'s
        //   orphan class into `features/foo`, producing an extension
        //   class that contains a residual-origin source_unit.
        // - `consumer` is a residual source_unit with a constraining edge to
        //   `bridge` AND a constraining edge into a SECOND active module
        //   `features/baz`. Touching two modules makes the greedy
        //   refuse to absorb `consumer`, so it survives as its own
        //   residual class. Crucially there is no atomic-DAG edge
        //   `consumer → bridge`, so the seed quotient's pass-3
        //   reachability does NOT fold them into one class — the
        //   source_unit-level constraining edge `consumer → bridge` survives
        //   as a cross-class residual edge.
        //
        // `consumer`'s proposal therefore records a cross-class residual
        // reference whose target candidate is the `features/foo`
        // extension. The buggy emitter formatted that reference as
        // `auto_partition_<candidate_idx>` — an id no emitted proposal
        // carries — yielding a dangling reference.
        let foo = active_source_unit("foo", 1, &["foo"], 10, "features/foo");
        let baz = active_source_unit("baz", 2, &["baz"], 10, "features/baz");
        let bridge = residual_source_unit("bridge", 3, &["bridge"], 10);
        let consumer = residual_source_unit("consumer", 4, &["consumer"], 10);
        let graph = graph_of(
            vec![foo.clone(), baz.clone(), bridge.clone(), consumer.clone()],
            vec![
                // bridge depends on the `features/foo` module -> absorbed.
                dependency_edge("e1", "bridge", "foo", DepKind::EagerUse, true),
                // consumer depends on bridge (cross-class residual edge)
                // and on the `features/baz` module (second module ->
                // greedy refuses to absorb consumer).
                dependency_edge("e2", "consumer", "bridge", DepKind::EagerUse, true),
                dependency_edge("e3", "consumer", "baz", DepKind::EagerUse, true),
            ],
            vec![
                atomic_group_for("atomic:0", &[&foo]),
                atomic_group_for("atomic:1", &[&baz]),
                atomic_group_for("atomic:2", &[&bridge]),
                atomic_group_for("atomic:3", &[&consumer]),
            ],
            // Only the bridge -> foo reachability edge. No
            // consumer -> bridge atomic edge: pass-3 must not fold
            // consumer and bridge into one class.
            vec![atomic_edge("atomic_edge:0", "atomic:2", "atomic:0")],
        );
        let report = propose_report(
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
            consumer.other_residual_classes_referenced,
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
            for referenced in &proposal.other_residual_classes_referenced {
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
        let a = source_unit("a", 1, &["a"], 10, module_ref("domains/system/ids"));
        let b = source_unit("b", 2, &["b"], 10, module_ref("domains/system/id_helpers"));
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![
                dependency_edge("e1", "a", "b", DepKind::EagerUse, true),
                dependency_edge("e2", "b", "a", DepKind::EagerUse, true),
            ],
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report = propose_report(&graph, &BTreeMap::new(), 10_000).unwrap();
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
    fn two_source_units_of_one_module_reached_by_different_routes_do_not_self_merge() {
        // Two source_units of ONE spec module (same destination key ->
        // `spec_module_groups` contracts them into one class). Source unit
        // `a` resolves its identity via an active claim
        // (`domains/system/ids`); source_unit `b` has no claim and resolves
        // via the module table. Both routes yield the one canonical
        // `ModulePath` (the wire carries one interned key and the table
        // holds one canonical path), so the class collapses to a single
        // label and emits NO `merge_into` self-merge.
        let dest = module_ref("domains/system/ids");
        let a = source_unit("a", 1, &["a"], 10, dest.clone());
        let b = source_unit("b", 2, &["b"], 10, dest.clone());
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![
                dependency_edge("e1", "a", "b", DepKind::EagerUse, true),
                dependency_edge("e2", "b", "a", DepKind::EagerUse, true),
            ],
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![],
        );
        let report =
            propose_report(&graph, &claims(&[("a", "domains/system/ids")]), 10_000).unwrap();
        assert!(
            report.proposals.iter().all(|p| p.merge_into.is_none()),
            "expected no self-merge proposal from two spellings of one module: {report:#?}",
        );
    }

    #[test]
    fn non_residual_source_unit_with_missing_destination_is_clean_error_not_panic() {
        // A non-residual source_unit whose `destination` key is absent from
        // the module table (`quotient.nodes`) models a truncated /
        // version-skewed `dependency_graph.json.gz` deserialized from disk by
        // `propose_from_files`. `is_residual` returns false for an
        // unknown key, so the source_unit survives the residual filter and
        // reaches `active_module_label`. The proposer must surface a
        // clean `Err` (not panic) so the CLI (`debundle modules
        // propose`) reports it through `anyhow` rather than aborting.
        let a = source_unit("a", 1, &["a"], 10, module_ref("features/foo"));
        let mut graph = graph_of(
            vec![a.clone()],
            vec![],
            vec![atomic_group_for("atomic:0", &[&a])],
            vec![],
        );
        // Drop the source_unit's destination from the module table to
        // simulate the malformed report.
        graph
            .quotient
            .nodes
            .retain(|entry| entry.key != a.destination);
        assert!(graph.module(&a.destination).is_none());

        let result = propose_report(&graph, &BTreeMap::new(), 10_000);
        let err = result.expect_err("missing destination must be a clean error, not a panic");
        let message = format!("{err:#}");
        assert!(
            message.contains("malformed dependency graph") && message.contains(&a.id),
            "error must name the malformed report and the offending source unit: {message}",
        );
    }

    #[test]
    fn anonymous_import_export_proposals_stay_visible_but_not_landable() {
        let mut import = residual_source_unit("unit:import", 1, &[], 1);
        import.statement_kind = StatementKind::Import;
        let graph = graph_of(
            vec![import.clone()],
            vec![],
            vec![atomic_group_for("atomic:0", &[&import])],
            vec![],
        );
        let report = propose_report(&graph, &BTreeMap::new(), 10_000).unwrap();
        let proposal = report.proposals.first().expect("anonymous proposal");
        assert_eq!(
            proposal.anonymous_statement_source_unit_ids,
            vec!["unit:import".to_string()],
        );
        assert!(!proposal.landable_today);
        assert_eq!(
            proposal.unaddressable_anonymous_source_unit_ids,
            vec!["unit:import".to_string()],
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
        let mut effect = residual_source_unit("unit:effect", 1, &[], 1);
        effect.statement_kind = StatementKind::SideEffect;
        let graph = graph_of(
            vec![effect.clone()],
            vec![],
            vec![atomic_group_for("atomic:0", &[&effect])],
            vec![],
        );
        let input = DependencyGraphInput::from_report(graph).unwrap();
        let report = propose_with_context(
            &input,
            &BTreeMap::new(),
            10_000,
            ProposalContext {
                addressable_anonymous_source_unit_ids: Some(BTreeSet::from([
                    "unit:effect".to_string()
                ])),
            },
        )
        .unwrap();
        let proposal = report.proposals.first().expect("anonymous proposal");
        assert!(proposal.landable_today, "{proposal:?}");
        assert!(proposal.unaddressable_anonymous_source_unit_ids.is_empty());
        assert!(proposal.landability_notes.is_empty());
    }

    // Source units that individually exceed the size cap surface as
    // `ExceedsSizeCap` diagnostics rather than proposals. The seed
    // quotient's size-cap gate refuses to merge the two source_units into
    // a closure (combined 20 lines > cap=5), so each oversized
    // singleton class becomes its own diagnostic.
    #[test]
    fn oversized_singletons_become_size_cap_diagnostics() {
        let a = residual_source_unit("a", 1, &["a"], 10);
        let b = residual_source_unit("b", 2, &["b"], 10);
        let graph = graph_of(
            vec![a.clone(), b.clone()],
            vec![dependency_edge("e1", "a", "b", DepKind::EagerUse, true)],
            vec![
                atomic_group_for("atomic:0", &[&a]),
                atomic_group_for("atomic:1", &[&b]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let report = propose_report(&graph, &BTreeMap::new(), 5).unwrap();
        assert!(
            report.proposals.is_empty(),
            "oversized source units should not appear as proposals: {report:#?}",
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
