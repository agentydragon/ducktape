//! Agent-facing read-only peel-planning workbench.
//!
//! These subcommands are for agents maintaining a debundle spec. They expose
//! stable JSON operations over the dependency graph and spec tree instead of
//! human-oriented "views".

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt::{Display, Formatter};
use std::fs;
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

use super::propose::{
    DEFAULT_SIZE_CAP_LINES, ModuleProposal, ProposalDiagnostic, ProposalOptions, ProposalsReport,
    propose_from_files, propose_from_graph,
};
use anonymous_resolution::{
    SourceClaimSet, resolve_source_claims, resolve_source_claims_of, resolve_source_file,
};
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, ValueEnum};
use serde::Serialize;

use analysis::{
    AtomicGroupEdgeReport, AtomicGroupReport, BindingReport, DepKind, DependencyEdgeReport,
    DependencyGraphReport, QuotientEdgeReport, SourceLocation, SourceUnitReport,
};
use spec_modules::{
    collect_module_files, default_binding_patches_path, load_binding_patch_members,
    module_path_from_file, read_module_claims, read_module_file,
};

#[derive(Debug, Clone, ClapArgs)]
pub struct CommonArgs {
    /// Path to `dependency_graph.json` (debundler analysis output).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub dependency_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct PlanWorkArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Tree config for chunk-scoped module claims in a multi-chunk spec.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub tree_config: Option<PathBuf>,

    /// Hard line ceiling per emitted proposal.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Root used to resolve relative `source_location.source_path`
    /// values when annotating anonymous-statement proposal addressability.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Maximum number of proposals and diagnostics to emit. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct AtomicGroupsArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of atomic groups to emit. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Filter to atomic groups containing at least one residual source unit.
    #[arg(long = "residual-only")]
    pub residual_only: bool,

    /// Filter to atomic groups with at least one renamed export.
    #[arg(long = "readable-only")]
    pub readable_only: bool,

    /// Also group emitted atomic groups by current destination.
    #[arg(long = "by-destination")]
    pub by_destination: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct PatchPlanArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of rows to keep. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Also run the proposal proposer to populate matching proposal ids.
    /// This is intentionally opt-in because it is expensive on large graphs.
    #[arg(long = "include-proposals")]
    pub include_proposals: bool,

    /// Root used to resolve relative `source_location.source_path`
    /// values when coverage checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct GraphSummaryArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Hard line ceiling per emitted proposal.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Maximum number of largest residual atomic groups to emit. Zero means unlimited.
    #[arg(long, default_value_t = 10)]
    pub limit: usize,

    /// Also run the proposal proposer to include proposal/diagnostic counts.
    /// This is intentionally opt-in because it is expensive on large graphs.
    #[arg(long = "include-proposals")]
    pub include_proposals: bool,

    /// Root used to resolve relative `source_location.source_path`
    /// values when annotating anonymous-statement proposal addressability.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone)]
pub struct ExplainArgs {
    pub selection: SelectionKind,
    /// Maximum number of rows to emit per report section. Zero means unlimited.
    pub limit: usize,
    /// Which dependency-graph edge directions to include in the report.
    pub direction: ExplainDirection,
    /// Keep edges whose typed reason matches any listed kind; empty means all.
    pub reasons: Vec<DepKind>,
    /// Also run the proposal proposer to annotate matching proposals and
    /// diagnostics. This is intentionally opt-in because it is expensive on
    /// large graphs.
    pub include_proposals: bool,
}

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
#[value(rename_all = "snake_case")]
pub enum ExplainDirection {
    Incoming,
    Outgoing,
    Both,
}

pub const DEFAULT_DESCRIBE_LIMIT: usize = 20;

#[derive(Debug, Clone)]
pub struct SourceSelectionArgs {
    pub common: CommonArgs,
    pub selection: SelectionKind,
    /// Hard line ceiling used when resolving a proposal selection.
    pub size_cap_lines: usize,
    /// Root used to resolve relative `source_location.source_path` values.
    pub source_root: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct QueryReport {
    pub kind: QueryKind,
    pub value: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    SourceUnit,
    Module,
    Binding,
    Proposal,
    AtomicGroup,
    Diagnostic,
}

/// What `describe` / `inspect-source --id` can resolve; each variant names its query kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectionKind {
    /// One source-unit ID from `dependency_graph.json`.
    SourceUnit(String),
    /// Every source unit claimed by this module path.
    ModulePath(String),
    /// Every source unit assigned to this module ID.
    Module(String),
    /// The source unit that declares this input binding id.
    Binding(String),
    /// A proposer proposal by `proposed_module_id`.
    Proposal(String),
    /// One atomic group id from `dependency_graph.json`.
    AtomicGroup(String),
    /// One proposer diagnostic by `diagnostic_id`.
    Diagnostic(String),
}

impl SelectionKind {
    fn value(&self) -> &str {
        match self {
            Self::SourceUnit(v)
            | Self::ModulePath(v)
            | Self::Module(v)
            | Self::Binding(v)
            | Self::Proposal(v)
            | Self::AtomicGroup(v)
            | Self::Diagnostic(v) => v,
        }
    }

    fn query_kind(&self) -> QueryKind {
        match self {
            Self::SourceUnit(_) => QueryKind::SourceUnit,
            Self::ModulePath(_) => QueryKind::Module,
            Self::Module(_) => QueryKind::Module,
            Self::Binding(_) => QueryKind::Binding,
            Self::Proposal(_) => QueryKind::Proposal,
            Self::AtomicGroup(_) => QueryKind::AtomicGroup,
            Self::Diagnostic(_) => QueryKind::Diagnostic,
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AtomicGroupsReport {
    pub atomic_groups: Vec<AtomicGroupReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub destination_groups: Vec<DestinationGroup>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DestinationGroup {
    pub destination: String,
    pub atomic_group_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PlanWorkReport {
    #[serde(flatten)]
    pub report: ProposalsReport,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExplainReport {
    pub query: QueryReport,
    pub source_unit_ids: Vec<String>,
    pub source_units: Vec<SourceUnitReport>,
    pub neighbor_source_units: Vec<SourceUnitReport>,
    pub bindings: Vec<BindingReport>,
    pub binding_homes: Vec<BindingHomeReport>,
    pub incoming_edges: Vec<DependencyEdgeReport>,
    pub outgoing_edges: Vec<DependencyEdgeReport>,
    pub atomic_groups: Vec<AtomicGroupReport>,
    pub incoming_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub outgoing_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub quotient_edges: Vec<QuotientEdgeReport>,
    /// Bindings claimed by a module-path selection that do not
    /// appear in the dependency graph. Always empty for other selections.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unknown_binding_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module_proposals: Option<Vec<ModuleProposal>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module_proposal_diagnostics: Option<Vec<ProposalDiagnostic>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanReport {
    pub rows: Vec<PatchPlanRow>,
    pub summary: PatchPlanSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanSummary {
    pub total_patch_sets: usize,
    pub complete_patch_sets: usize,
    pub split_patch_sets: usize,
    pub unknown_binding_count: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanRow {
    pub path: String,
    pub file: String,
    pub status: PatchPlanStatus,
    pub requested_binding_ids: Vec<String>,
    pub unknown_binding_ids: Vec<String>,
    pub atomic_group_ids: Vec<String>,
    pub complete_atomic_group_ids: Vec<String>,
    pub split_atomic_group_ids: Vec<String>,
    pub missing_binding_ids: Vec<String>,
    pub missing_source_unit_ids: Vec<String>,
    pub missing_anonymous_source_unit_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub matching_proposal_ids: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum PatchPlanStatus {
    CompleteAtomicGroups,
    SplitAtomicGroups,
    UnknownBindings,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GraphSummaryReport {
    pub source_unit_count: usize,
    pub dependency_edge_count: usize,
    pub atomic_group_count: usize,
    pub residual_atomic_group_count: usize,
    pub atomic_edge_count: usize,
    pub module_count: usize,
    pub module_edge_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proposal_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostic_count: Option<usize>,
    pub largest_residual_atomic_groups: Vec<AtomicGroupSummary>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AtomicGroupSummary {
    pub atomic_group_id: String,
    pub size_lines_estimate: usize,
    pub members: Vec<BindingReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LimitReport {
    pub limit: usize,
    pub sections: BTreeMap<&'static str, LimitSectionReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LimitSectionReport {
    pub total: usize,
    pub emitted: usize,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct BindingHomeReport {
    pub binding: String,
    pub name: String,
    pub source_kind: BindingHomeSourceKind,
    pub path: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum BindingHomeSourceKind {
    Module,
    BindingPatch,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SourceSelectionReport {
    pub query: QueryReport,
    pub source_unit_ids: Vec<String>,
    /// Bindings claimed by a module-path selection that do not
    /// appear in the dependency graph. Always empty for other selections.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unknown_binding_ids: Vec<String>,
    pub source_units: Vec<SelectedSourceUnitSource>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SelectedSourceUnitSource {
    pub source_unit_id: String,
    pub source_path: String,
    pub resolved_path: String,
    pub statement_ordinal: usize,
    pub source_location: SourceLocation,
}

/// Uniform output format selector for every read-only query command,
/// per docs/cli.md § "Output format". `text` is the default when
/// stdout is a tty; `json` is the default when stdout is piped.
/// `ndjson` emits one JSON value per line so streaming consumers
/// (`jq -c`, downstream pipes) don't have to buffer.
#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
#[value(rename_all = "lowercase")]
pub enum OutputFormat {
    Text,
    Json,
    Ndjson,
}

impl OutputFormat {
    /// Resolve a user-supplied `--format` against the tty default.
    pub fn resolve(opt: Option<Self>) -> Self {
        if let Some(f) = opt {
            return f;
        }
        if std::io::stdout().is_terminal() {
            Self::Text
        } else {
            Self::Json
        }
    }
}

/// Emit `value` in the requested `format`. `text_render` produces the
/// human-readable rendering when `format` resolves to text.
pub fn print_report<T, F>(value: &T, format: OutputFormat, text_render: F) -> Result<()>
where
    T: Serialize,
    F: FnOnce(&T, &mut String),
{
    match format {
        OutputFormat::Text => {
            let mut buf = String::new();
            text_render(value, &mut buf);
            if !buf.is_empty() && !buf.ends_with('\n') {
                buf.push('\n');
            }
            print!("{buf}");
            Ok(())
        }
        OutputFormat::Json => {
            println!("{}", serde_json::to_string_pretty(value)?);
            Ok(())
        }
        OutputFormat::Ndjson => {
            // For ndjson on a non-array shape, fall back to one
            // compact JSON document per call. Commands whose primary
            // output is an array (atoms, scc, modules list) override
            // and emit one line per item.
            println!("{}", serde_json::to_string(value)?);
            Ok(())
        }
    }
}

/// For a command whose primary output is an array: when `format` is `Ndjson`,
/// emit one compact JSON line per item and return `true` (the caller should
/// stop). Otherwise emit nothing and return `false`, so the caller falls through
/// to [`print_report`] for the text/json rendering of the whole report.
pub fn print_ndjson_list<T: Serialize>(items: &[T], format: OutputFormat) -> Result<bool> {
    if format != OutputFormat::Ndjson {
        return Ok(false);
    }
    for item in items {
        println!("{}", serde_json::to_string(item)?);
    }
    Ok(true)
}

pub fn run_plan_work_report(args: &PlanWorkArgs) -> Result<PlanWorkReport> {
    let mut report = propose_from_files(&ProposalOptions {
        dependency_graph_path: args.common.dependency_graph_path.clone(),
        modules_root: args.common.modules_root.clone(),
        tree_config: args.tree_config.clone(),
        source_root: args.source_root.clone(),
        size_cap_lines: args.size_cap_lines,
    })?;
    let mut sections = BTreeMap::new();
    sort_proposal_diagnostics(&mut report.diagnostics);
    apply_limit_with_metadata(
        &mut report.proposals,
        args.limit,
        &mut sections,
        "proposals",
    );
    apply_limit_with_metadata(
        &mut report.diagnostics,
        args.limit,
        &mut sections,
        "diagnostics",
    );
    Ok(PlanWorkReport {
        report,
        limits: limit_report(args.limit, sections),
    })
}

pub fn run_atomic_groups_report(args: &AtomicGroupsArgs) -> Result<AtomicGroupsReport> {
    let graph = load_graph(&args.common.dependency_graph_path)?;
    let mut atomic_groups = graph.atomic_graph.nodes.clone();
    if args.residual_only {
        atomic_groups.retain(|atomic_group| {
            atomic_group
                .destinations
                .iter()
                .any(|destination| graph.is_unassigned_or_entry(destination))
        });
    }
    if args.readable_only {
        atomic_groups.retain(|atomic_group| {
            atomic_group
                .members
                .iter()
                .any(|member| member.binding != member.export_name)
        });
    }
    atomic_groups.sort_by_key(|atomic_group| {
        (
            atomic_group
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX),
            atomic_group.id.clone(),
        )
    });
    apply_limit(&mut atomic_groups, args.limit);
    let destination_groups = if args.by_destination {
        group_atomic_groups_by_destination(&graph, &atomic_groups)
    } else {
        Vec::new()
    };
    Ok(AtomicGroupsReport {
        atomic_groups,
        destination_groups,
    })
}

pub fn run_patch_plan_report(args: &PatchPlanArgs) -> Result<PatchPlanReport> {
    let graph = load_graph(&args.common.dependency_graph_path)?;
    let propose = if args.include_proposals {
        Some(propose_from_graph(
            &graph,
            &ProposalOptions {
                dependency_graph_path: args.common.dependency_graph_path.clone(),
                modules_root: args.common.modules_root.clone(),
                tree_config: None,
                source_root: args.source_root.clone(),
                size_cap_lines: DEFAULT_SIZE_CAP_LINES,
            },
        )?)
    } else {
        None
    };
    let mut rows = patch_plan_rows(
        &graph,
        &args.common.dependency_graph_path,
        &args.common.modules_root,
        args.source_root.as_deref(),
        propose.as_ref(),
    )?;
    rows.sort_by_key(|row| (row.status, row.path.clone()));
    let summary = PatchPlanSummary {
        total_patch_sets: rows.len(),
        complete_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::CompleteAtomicGroups)
            .count(),
        split_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::SplitAtomicGroups)
            .count(),
        unknown_binding_count: rows.iter().map(|row| row.unknown_binding_ids.len()).sum(),
    };
    let mut sections = BTreeMap::new();
    apply_limit_with_metadata(&mut rows, args.limit, &mut sections, "rows");
    Ok(PatchPlanReport {
        rows,
        summary,
        limits: limit_report(args.limit, sections),
    })
}

pub fn run_graph_summary_report(args: &GraphSummaryArgs) -> Result<GraphSummaryReport> {
    let graph = load_graph(&args.common.dependency_graph_path)?;
    let propose = if args.include_proposals {
        Some(propose_from_graph(
            &graph,
            &ProposalOptions {
                dependency_graph_path: args.common.dependency_graph_path.clone(),
                modules_root: args.common.modules_root.clone(),
                tree_config: None,
                source_root: args.source_root.clone(),
                size_cap_lines: args.size_cap_lines,
            },
        )?)
    } else {
        None
    };
    let mut largest_residual_atomic_groups: Vec<AtomicGroupSummary> = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| {
            atomic_group
                .destinations
                .iter()
                .any(|destination| graph.is_unassigned_or_entry(destination))
        })
        .map(|atomic_group| AtomicGroupSummary {
            atomic_group_id: atomic_group.id.clone(),
            size_lines_estimate: atomic_group.size_lines_estimate,
            members: atomic_group.members.clone(),
        })
        .collect();
    largest_residual_atomic_groups.sort_by_key(|atomic_group| {
        (
            std::cmp::Reverse(atomic_group.size_lines_estimate),
            atomic_group.atomic_group_id.clone(),
        )
    });
    apply_limit(&mut largest_residual_atomic_groups, args.limit);
    Ok(GraphSummaryReport {
        source_unit_count: graph.nodes.len(),
        dependency_edge_count: graph.edges.len(),
        atomic_group_count: graph.atomic_graph.nodes.len(),
        residual_atomic_group_count: graph
            .atomic_graph
            .nodes
            .iter()
            .filter(|atomic_group| {
                atomic_group
                    .destinations
                    .iter()
                    .any(|destination| graph.is_unassigned_or_entry(destination))
            })
            .count(),
        atomic_edge_count: graph.atomic_graph.edges.len(),
        module_count: graph.quotient.nodes.len(),
        module_edge_count: graph.quotient.edges.len(),
        proposal_count: propose.as_ref().map(|report| report.proposals.len()),
        diagnostic_count: propose.as_ref().map(|report| report.diagnostics.len()),
        largest_residual_atomic_groups,
    })
}

macro_rules! apply_limits {
    ($limit:expr, $sections:expr, $(($vec:expr, $name:expr)),+ $(,)?) => {
        $(
            apply_limit_with_metadata(&mut $vec, $limit, &mut $sections, $name);
        )+
    };
}

/// One command's loaded graph and shared authoring lookups. This cache never
/// survives the invocation or suppresses the caller's compatibility check.
pub struct ExplainSession {
    graph: DependencyGraphReport,
    common: CommonArgs,
    source_root: Option<PathBuf>,
    size_cap_lines: usize,
    binding_homes: Option<Vec<BindingHomeReport>>,
    proposals: Option<ProposalsReport>,
}

impl ExplainSession {
    pub fn load(
        common: CommonArgs,
        source_root: Option<PathBuf>,
        size_cap_lines: usize,
    ) -> Result<Self> {
        Ok(Self {
            graph: load_graph(&common.dependency_graph_path)?,
            common,
            source_root,
            size_cap_lines,
            binding_homes: None,
            proposals: None,
        })
    }

    pub fn graph(&self) -> &DependencyGraphReport {
        &self.graph
    }

    pub fn explain(&mut self, args: &ExplainArgs) -> Result<ExplainReport> {
        if self.binding_homes.is_none() {
            self.binding_homes = Some(binding_homes(&self.common.modules_root)?);
        }
        let needs_proposals = args.include_proposals
            || matches!(
                args.selection,
                SelectionKind::Proposal(_) | SelectionKind::Diagnostic(_)
            );
        if needs_proposals && self.proposals.is_none() {
            self.proposals = Some(propose_from_graph(
                &self.graph,
                &ProposalOptions {
                    dependency_graph_path: self.common.dependency_graph_path.clone(),
                    modules_root: self.common.modules_root.clone(),
                    tree_config: None,
                    source_root: self.source_root.clone(),
                    size_cap_lines: self.size_cap_lines,
                },
            )?);
        }
        explain_from_graph(
            args,
            &self.graph,
            &self.common,
            self.source_root.as_deref(),
            self.size_cap_lines,
            self.binding_homes.as_deref().expect("loaded above"),
            if needs_proposals {
                self.proposals.as_ref()
            } else {
                None
            },
        )
    }
}

fn explain_from_graph(
    args: &ExplainArgs,
    graph: &DependencyGraphReport,
    common: &CommonArgs,
    source_root: Option<&Path>,
    size_cap_lines: usize,
    homes: &[BindingHomeReport],
    propose: Option<&ProposalsReport>,
) -> Result<ExplainReport> {
    let selection = &args.selection;
    let query = query_report(selection);
    let include_proposal_sections = propose.is_some();
    let ResolvedSelection {
        source_unit_ids,
        mut unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        &graph,
        common,
        size_cap_lines,
        source_root,
        propose,
    )?;
    let source_unit_set: BTreeSet<String> = source_unit_ids.iter().cloned().collect();
    let mut source_units = source_units_for_ids(&graph, &source_unit_set);

    let mut neighbor_ids = BTreeSet::new();
    let mut incoming_edges: Vec<DependencyEdgeReport> = graph
        .edges
        .iter()
        .filter(|edge| source_unit_set.contains(&edge.target))
        .inspect(|edge| {
            if !source_unit_set.contains(&edge.source) {
                neighbor_ids.insert(edge.source.clone());
            }
        })
        .cloned()
        .collect();
    let mut outgoing_edges: Vec<DependencyEdgeReport> = graph
        .edges
        .iter()
        .filter(|edge| source_unit_set.contains(&edge.source))
        .inspect(|edge| {
            if !source_unit_set.contains(&edge.target) {
                neighbor_ids.insert(edge.target.clone());
            }
        })
        .cloned()
        .collect();
    let mut neighbor_source_units = source_units_for_ids(&graph, &neighbor_ids);
    let selected_atomic_group_ids = atomic_group_ids_for_source_unit_set(&graph, &source_unit_set);
    let mut atomic_groups: Vec<AtomicGroupReport> = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| selected_atomic_group_ids.contains(&atomic_group.id))
        .cloned()
        .collect();
    let mut incoming_atomic_edges: Vec<AtomicGroupEdgeReport> = graph
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_atomic_group_ids.contains(&edge.target))
        .cloned()
        .collect();
    let mut outgoing_atomic_edges: Vec<AtomicGroupEdgeReport> = graph
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_atomic_group_ids.contains(&edge.source))
        .cloned()
        .collect();

    let mut bindings: Vec<BindingReport> = source_units
        .iter()
        .flat_map(|source_unit| source_unit.declared_bindings.iter().cloned())
        .collect();
    bindings.sort();
    bindings.dedup();
    let binding_ids: BTreeSet<String> = bindings
        .iter()
        .map(|binding| binding.binding.to_string())
        .collect();
    let mut binding_homes = homes
        .iter()
        .filter(|home| binding_ids.contains(&home.binding))
        .cloned()
        .collect();

    let selected_destinations: BTreeSet<analysis::ModuleKey> = source_units
        .iter()
        .map(|source_unit| source_unit.destination.clone())
        .collect();
    let mut quotient_edges: Vec<QuotientEdgeReport> = graph
        .quotient
        .edges
        .iter()
        .filter(|edge| {
            selected_destinations.contains(&edge.source)
                || selected_destinations.contains(&edge.target)
        })
        .cloned()
        .collect();

    let (mut module_proposals, mut module_proposal_diagnostics): (
        Vec<ModuleProposal>,
        Vec<ProposalDiagnostic>,
    ) = if let Some(propose) = propose {
        (
            propose
                .proposals
                .iter()
                .filter(|proposal| overlaps(&proposal.source_unit_ids, &source_unit_set))
                .cloned()
                .collect(),
            propose
                .diagnostics
                .iter()
                .filter(|diagnostic| overlaps(&diagnostic.source_unit_ids, &source_unit_set))
                .cloned()
                .collect(),
        )
    } else {
        (Vec::new(), Vec::new())
    };

    let matches_reason = |kinds: &[DepKind]| {
        args.reasons.is_empty() || kinds.iter().any(|kind| args.reasons.contains(kind))
    };
    if matches!(args.direction, ExplainDirection::Outgoing) {
        incoming_edges.clear();
        incoming_atomic_edges.clear();
    }
    if matches!(args.direction, ExplainDirection::Incoming) {
        outgoing_edges.clear();
        outgoing_atomic_edges.clear();
    }
    incoming_edges.retain(|edge| matches_reason(std::slice::from_ref(&edge.edge_kind)));
    outgoing_edges.retain(|edge| matches_reason(std::slice::from_ref(&edge.edge_kind)));
    incoming_atomic_edges.retain(|edge| matches_reason(&edge.edge_kinds));
    outgoing_atomic_edges.retain(|edge| matches_reason(&edge.edge_kinds));
    quotient_edges.retain(|edge| {
        let incoming = selected_destinations.contains(&edge.target);
        let outgoing = selected_destinations.contains(&edge.source);
        let direction_matches = match args.direction {
            ExplainDirection::Incoming => incoming,
            ExplainDirection::Outgoing => outgoing,
            ExplainDirection::Both => incoming || outgoing,
        };
        direction_matches && matches_reason(&edge.edge_kinds)
    });

    let mut neighbor_ids = BTreeSet::new();
    for edge in &incoming_edges {
        if !source_unit_set.contains(&edge.source) {
            neighbor_ids.insert(edge.source.clone());
        }
    }
    for edge in &outgoing_edges {
        if !source_unit_set.contains(&edge.target) {
            neighbor_ids.insert(edge.target.clone());
        }
    }
    neighbor_source_units.retain(|source_unit| neighbor_ids.contains(&source_unit.id));

    source_units.sort_by(|left, right| left.id.cmp(&right.id));
    neighbor_source_units.sort_by(|left, right| left.id.cmp(&right.id));
    bindings.sort();
    binding_homes.sort();
    incoming_edges.sort_by(|left, right| left.id.cmp(&right.id));
    outgoing_edges.sort_by(|left, right| left.id.cmp(&right.id));
    atomic_groups.sort_by(|left, right| left.id.cmp(&right.id));
    incoming_atomic_edges.sort_by(|left, right| left.id.cmp(&right.id));
    outgoing_atomic_edges.sort_by(|left, right| left.id.cmp(&right.id));
    quotient_edges.sort_by(|left, right| left.id.cmp(&right.id));
    unknown_binding_ids.sort_unstable();
    module_proposals.sort_by(|left, right| left.proposed_module_id.cmp(&right.proposed_module_id));
    module_proposal_diagnostics.sort_by(|left, right| left.diagnostic_id.cmp(&right.diagnostic_id));

    let mut preferred_source_unit_ids = BTreeSet::new();
    match selection {
        SelectionKind::SourceUnit(source_unit_id) => {
            preferred_source_unit_ids.insert(source_unit_id.clone());
        }
        SelectionKind::Binding(binding_id) => {
            move_to_front(&mut bindings, |binding| {
                binding.binding.as_str() == binding_id || binding.export_name.as_str() == binding_id
            });
            move_to_front(&mut binding_homes, |home| {
                home.binding == *binding_id || home.name == *binding_id
            });
            preferred_source_unit_ids.extend(
                source_units
                    .iter()
                    .filter(|source_unit| {
                        source_unit.declared_bindings.iter().any(|binding| {
                            binding.binding.as_str() == binding_id
                                || binding.export_name.as_str() == binding_id
                        })
                    })
                    .map(|source_unit| source_unit.id.clone()),
            );
        }
        SelectionKind::AtomicGroup(atomic_group_id) => {
            move_to_front(&mut atomic_groups, |group| group.id == *atomic_group_id);
            if let Some(group) = atomic_groups
                .iter()
                .find(|group| group.id == *atomic_group_id)
            {
                preferred_source_unit_ids.extend(group.source_unit_ids.iter().cloned());
            }
        }
        SelectionKind::Proposal(proposal_id) => {
            move_to_front(&mut module_proposals, |proposal| {
                proposal.proposed_module_id == *proposal_id
            });
            if let Some(proposal) = module_proposals
                .iter()
                .find(|proposal| proposal.proposed_module_id == *proposal_id)
            {
                preferred_source_unit_ids.extend(proposal.source_unit_ids.iter().cloned());
            }
        }
        SelectionKind::Diagnostic(diagnostic_id) => {
            move_to_front(&mut module_proposal_diagnostics, |diagnostic| {
                diagnostic.diagnostic_id == *diagnostic_id
            });
            if let Some(diagnostic) = module_proposal_diagnostics
                .iter()
                .find(|diagnostic| diagnostic.diagnostic_id == *diagnostic_id)
            {
                preferred_source_unit_ids.extend(diagnostic.source_unit_ids.iter().cloned());
            }
        }
        SelectionKind::ModulePath(_) | SelectionKind::Module(_) => {}
    }

    source_units.sort_by_key(|source_unit| !preferred_source_unit_ids.contains(&source_unit.id));
    let mut limited_source_unit_ids = source_unit_ids;
    limited_source_unit_ids.sort();
    limited_source_unit_ids.sort_by_key(|id| !preferred_source_unit_ids.contains(id));

    let mut sections = BTreeMap::new();
    apply_limits!(
        args.limit,
        sections,
        (limited_source_unit_ids, "source_unit_ids"),
        (source_units, "source_units"),
        (neighbor_source_units, "neighbor_source_units"),
        (bindings, "bindings"),
        (binding_homes, "binding_homes"),
        (incoming_edges, "incoming_edges"),
        (outgoing_edges, "outgoing_edges"),
        (atomic_groups, "atomic_groups"),
        (incoming_atomic_edges, "incoming_atomic_edges"),
        (outgoing_atomic_edges, "outgoing_atomic_edges"),
        (quotient_edges, "quotient_edges"),
        (unknown_binding_ids, "unknown_binding_ids"),
        (module_proposals, "module_proposals"),
        (module_proposal_diagnostics, "module_proposal_diagnostics"),
    );

    Ok(ExplainReport {
        query,
        source_unit_ids: limited_source_unit_ids,
        source_units,
        neighbor_source_units,
        bindings,
        binding_homes,
        incoming_edges,
        outgoing_edges,
        atomic_groups,
        incoming_atomic_edges,
        outgoing_atomic_edges,
        quotient_edges,
        unknown_binding_ids,
        module_proposals: include_proposal_sections.then_some(module_proposals),
        module_proposal_diagnostics: include_proposal_sections
            .then_some(module_proposal_diagnostics),
        limits: Some(LimitReport {
            limit: args.limit,
            sections,
        }),
    })
}

pub fn run_source_selection_report(args: &SourceSelectionArgs) -> Result<SourceSelectionReport> {
    let graph = load_graph(&args.common.dependency_graph_path)?;
    let selection = &args.selection;
    let query = query_report(selection);
    let ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        &graph,
        &args.common,
        args.size_cap_lines,
        args.source_root.as_deref(),
        None,
    )?;
    let source_unit_set: BTreeSet<String> = source_unit_ids.iter().cloned().collect();
    let source_units = source_units_for_ids(&graph, &source_unit_set);
    let mut selected = Vec::new();
    let mut resolved_paths: BTreeMap<String, String> = BTreeMap::new();
    for source_unit in source_units {
        let location = source_unit
            .source_location
            .as_ref()
            .with_context(|| format!("source unit {} has no source location", source_unit.id))?;
        let source_path = &location.source_path;
        let resolved_path = match resolved_paths.get(source_path) {
            Some(path) => path.clone(),
            None => {
                let path = resolve_source_file(
                    source_path,
                    args.source_root.as_deref(),
                    &args.common.dependency_graph_path,
                    &args.common.modules_root,
                )?
                .display()
                .to_string();
                resolved_paths.insert(source_path.clone(), path.clone());
                path
            }
        };
        selected.push(SelectedSourceUnitSource {
            source_unit_id: source_unit.id.clone(),
            source_path: source_path.clone(),
            resolved_path,
            statement_ordinal: source_unit.statement_ordinal.0,
            source_location: location.clone(),
        });
    }
    selected.sort_by(|a, b| {
        (&a.source_path, a.statement_ordinal, &a.source_unit_id).cmp(&(
            &b.source_path,
            b.statement_ordinal,
            &b.source_unit_id,
        ))
    });
    Ok(SourceSelectionReport {
        query,
        source_unit_ids,
        unknown_binding_ids,
        source_units: selected,
    })
}

fn load_graph(path: &Path) -> Result<DependencyGraphReport> {
    serde_json::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

fn apply_limit<T>(records: &mut Vec<T>, limit: usize) {
    if limit > 0 {
        records.truncate(limit);
    }
}

fn apply_limit_with_metadata<T>(
    records: &mut Vec<T>,
    limit: usize,
    sections: &mut BTreeMap<&'static str, LimitSectionReport>,
    section: &'static str,
) {
    let total = records.len();
    apply_limit(records, limit);
    let emitted = records.len();
    sections.insert(
        section,
        LimitSectionReport {
            total,
            emitted,
            truncated: emitted < total,
        },
    );
}

fn move_to_front<T>(records: &mut [T], mut matches: impl FnMut(&T) -> bool) {
    if let Some(index) = records.iter().position(&mut matches) {
        records[..=index].rotate_right(1);
    }
}

fn limit_report(
    limit: usize,
    sections: BTreeMap<&'static str, LimitSectionReport>,
) -> Option<LimitReport> {
    (limit > 0).then_some(LimitReport { limit, sections })
}

fn sort_proposal_diagnostics(diagnostics: &mut [ProposalDiagnostic]) {
    diagnostics.sort_by_key(|diagnostic| {
        (
            diagnostic.reason,
            diagnostic
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX),
            diagnostic.source_unit_ids.len(),
            diagnostic.diagnostic_id.clone(),
        )
    });
}

fn group_atomic_groups_by_destination(
    graph: &DependencyGraphReport,
    atomic_groups: &[AtomicGroupReport],
) -> Vec<DestinationGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for atomic_group in atomic_groups {
        for destination in &atomic_group.destinations {
            // Group by the human-facing path (the module's identity),
            // resolved once via the module table; fall back to the raw
            // key if the table lacks the entry.
            let label = graph
                .module(destination)
                .map(|entry| entry.path.to_string())
                .unwrap_or_else(|| destination.0.clone());
            groups
                .entry(label)
                .or_default()
                .push(atomic_group.id.clone());
        }
    }
    groups
        .into_iter()
        .map(|(destination, atomic_group_ids)| DestinationGroup {
            destination,
            atomic_group_ids,
        })
        .collect()
}

fn source_units_for_ids(
    graph: &DependencyGraphReport,
    source_unit_ids: &BTreeSet<String>,
) -> Vec<SourceUnitReport> {
    graph
        .nodes
        .iter()
        .filter(|source_unit| source_unit_ids.contains(&source_unit.id))
        .cloned()
        .collect()
}

fn atomic_group_ids_for_source_unit_set(
    graph: &DependencyGraphReport,
    source_unit_ids: &BTreeSet<String>,
) -> BTreeSet<String> {
    graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| {
            atomic_group
                .source_unit_ids
                .iter()
                .any(|source_unit| source_unit_ids.contains(source_unit))
        })
        .map(|atomic_group| atomic_group.id.clone())
        .collect()
}

fn overlaps(source_unit_ids: &[String], selected: &BTreeSet<String>) -> bool {
    source_unit_ids
        .iter()
        .any(|source_unit_id| selected.contains(source_unit_id))
}

fn patch_plan_rows(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    propose: Option<&ProposalsReport>,
) -> Result<Vec<PatchPlanRow>> {
    let binding_to_source_units = binding_to_source_units(graph);
    let atomic_group_by_source_unit = atomic_group_by_source_unit(graph);
    let atomic_group_by_id: BTreeMap<String, &AtomicGroupReport> = graph
        .atomic_graph
        .nodes
        .iter()
        .map(|atomic_group| (atomic_group.id.clone(), atomic_group))
        .collect();
    let patch_sets = load_patch_sets(modules_root)?;
    // Resolve every patch set's source-backed claims in ONE batched call: the
    // resolver parses each claimed source file once per call.
    let resolved_by_set = resolve_source_claims(
        graph,
        dependency_graph_path,
        modules_root,
        source_root,
        &patch_sets
            .iter()
            .map(|patch_set| SourceClaimSet {
                module_path: &patch_set.file,
                member_selectors: &patch_set.member_selectors,
                source_matches: &patch_set.source_matches,
                anonymous_selectors: &patch_set.anonymous_selectors,
            })
            .collect::<Vec<_>>(),
    )?;
    patch_sets
        .into_iter()
        .zip(resolved_by_set)
        .map(|(patch_set, resolved)| {
            let claimed_bindings: BTreeSet<String> = patch_set
                .bindings
                .iter()
                .chain(&resolved.bindings)
                .cloned()
                .collect();
            let mut requested_binding_ids: Vec<String> = claimed_bindings.iter().cloned().collect();
            requested_binding_ids.sort();

            let mut requested_source_unit_ids = BTreeSet::<String>::new();
            let mut unknown_binding_ids = Vec::<String>::new();
            for binding in &requested_binding_ids {
                if let Some(source_unit_ids) = binding_to_source_units.get(binding) {
                    requested_source_unit_ids.extend(source_unit_ids.iter().cloned());
                } else {
                    unknown_binding_ids.push(binding.clone());
                }
            }
            for source_unit in &resolved.anonymous_source_units {
                if let Some(node) = graph.nodes.get(source_unit.0) {
                    requested_source_unit_ids.insert(node.id.clone());
                }
            }

            let mut atomic_group_ids: BTreeSet<String> = BTreeSet::new();
            for source_unit_id in &requested_source_unit_ids {
                if let Some(atomic_group_id) = atomic_group_by_source_unit.get(source_unit_id) {
                    atomic_group_ids.insert(atomic_group_id.clone());
                }
            }

            let mut complete_atomic_group_ids = Vec::<String>::new();
            let mut split_atomic_group_ids = Vec::<String>::new();
            let mut missing_binding_ids = BTreeSet::<String>::new();
            let mut missing_source_unit_ids = BTreeSet::<String>::new();
            let mut missing_anonymous_source_unit_ids = BTreeSet::<String>::new();
            for atomic_group_id in &atomic_group_ids {
                let Some(atomic_group) = atomic_group_by_id.get(atomic_group_id) else {
                    continue;
                };
                let atomic_group_bindings: BTreeSet<String> = atomic_group
                    .members
                    .iter()
                    .map(|m| m.binding.to_string())
                    .collect();
                let atomic_group_source_units: BTreeSet<String> =
                    atomic_group.source_unit_ids.iter().cloned().collect();
                let bindings_complete = atomic_group_bindings
                    .iter()
                    .all(|binding| claimed_bindings.contains(binding));
                let source_units_complete = atomic_group_source_units
                    .iter()
                    .all(|source_unit_id| requested_source_unit_ids.contains(source_unit_id));
                if bindings_complete && source_units_complete {
                    complete_atomic_group_ids.push(atomic_group_id.clone());
                } else {
                    split_atomic_group_ids.push(atomic_group_id.clone());
                    missing_binding_ids.extend(
                        atomic_group_bindings
                            .into_iter()
                            .filter(|binding| !claimed_bindings.contains(binding)),
                    );
                    missing_source_unit_ids.extend(atomic_group_source_units.into_iter().filter(
                        |source_unit_id| !requested_source_unit_ids.contains(source_unit_id),
                    ));
                    missing_anonymous_source_unit_ids.extend(
                        atomic_group
                            .anonymous_statement_source_unit_ids
                            .iter()
                            .filter(|source_unit_id| {
                                !requested_source_unit_ids.contains(*source_unit_id)
                            })
                            .cloned(),
                    );
                }
            }
            let status = if !split_atomic_group_ids.is_empty() {
                PatchPlanStatus::SplitAtomicGroups
            } else if !unknown_binding_ids.is_empty() {
                PatchPlanStatus::UnknownBindings
            } else {
                PatchPlanStatus::CompleteAtomicGroups
            };
            let matching_proposal_ids =
                propose.map(|propose| matching_proposal_ids(propose, &requested_source_unit_ids));
            Ok(PatchPlanRow {
                path: patch_set.path,
                file: patch_set.file.display().to_string(),
                status,
                requested_binding_ids,
                unknown_binding_ids,
                atomic_group_ids: atomic_group_ids.into_iter().collect(),
                complete_atomic_group_ids,
                split_atomic_group_ids,
                missing_binding_ids: missing_binding_ids.into_iter().collect(),
                missing_source_unit_ids: missing_source_unit_ids.into_iter().collect(),
                missing_anonymous_source_unit_ids: missing_anonymous_source_unit_ids
                    .into_iter()
                    .collect(),
                matching_proposal_ids,
            })
        })
        .collect()
}

#[derive(Debug, Clone)]
struct PatchSet {
    path: String,
    file: PathBuf,
    bindings: BTreeSet<String>,
    member_selectors: Vec<(String, spec::MemberSelectorSpec)>,
    anonymous_selectors: BTreeSet<spec::AnonymousStatementSelector>,
    source_matches: Vec<spec::SourceMatchClaim>,
}

fn load_patch_sets(modules_root: &Path) -> Result<Vec<PatchSet>> {
    let mut sets = Vec::<PatchSet>::new();
    let binding_patches_path = default_binding_patches_path(modules_root);
    let patch_bindings: BTreeSet<String> = load_binding_patch_members(modules_root)?
        .into_iter()
        .filter_map(|member| member.selector.binding.map(|binding| binding.name))
        .collect();
    if !patch_bindings.is_empty() {
        sets.push(PatchSet {
            path: "binding_patches".to_string(),
            file: binding_patches_path,
            bindings: patch_bindings,
            member_selectors: Vec::new(),
            anonymous_selectors: BTreeSet::new(),
            source_matches: Vec::new(),
        });
    }
    for file in collect_module_files(modules_root)? {
        let claims = read_module_claims(&file)?;
        if claims.is_empty() {
            continue;
        }
        sets.push(PatchSet {
            path: module_path_from_file(&file, modules_root),
            file,
            bindings: claims.bindings,
            member_selectors: claims.member_selectors,
            anonymous_selectors: claims.anonymous_selectors,
            source_matches: claims.source_matches,
        });
    }
    Ok(sets)
}

/// Map each declared (minified) binding name to all source-unit IDs that declare it.
///
/// A repeated top-level `var` is one binding with several declaration sites,
/// and the dependency graph retains each site as a source-unit node. Binding-keyed claims
/// therefore cover every matching source unit so a co-declaration atomic group is
/// reported complete; the selector resolver uses the first site as its single
/// placement representative.
fn binding_to_source_units(graph: &DependencyGraphReport) -> BTreeMap<String, Vec<String>> {
    let mut out = BTreeMap::new();
    for node in &graph.nodes {
        for binding in &node.declared_bindings {
            out.entry(binding.binding.to_string())
                .or_insert_with(Vec::new)
                .push(node.id.clone());
        }
    }
    out
}

fn atomic_group_by_source_unit(graph: &DependencyGraphReport) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for atomic_group in &graph.atomic_graph.nodes {
        for source_unit_id in &atomic_group.source_unit_ids {
            out.insert(source_unit_id.clone(), atomic_group.id.clone());
        }
    }
    out
}

fn matching_proposal_ids(
    propose: &ProposalsReport,
    requested_source_unit_ids: &BTreeSet<String>,
) -> Vec<String> {
    if requested_source_unit_ids.is_empty() {
        return Vec::new();
    }
    propose
        .proposals
        .iter()
        .filter(|proposal| {
            let proposal_source_units: BTreeSet<String> =
                proposal.source_unit_ids.iter().cloned().collect();
            requested_source_unit_ids.is_subset(&proposal_source_units)
        })
        .map(|proposal| proposal.proposed_module_id.clone())
        .collect()
}

fn query_report(selection: &SelectionKind) -> QueryReport {
    QueryReport {
        kind: selection.query_kind(),
        value: selection.value().to_string(),
    }
}

/// Find every source-unit node whose `declared_bindings` matches `name`.
///
/// `name` is compared against both the minified `BindingReport.binding`
/// and the readable `BindingReport.export_name`. Matches by minified
/// name come first, so when both forms happen to spell `name` on
/// different bindings (extremely rare — readable names are picked to
/// avoid collisions with live minified ids), the minified match wins
/// the back-compat order; both still appear in the returned slice so
/// callers can detect ambiguity if they care.
///
/// This is the one shared resolver every CLI verb that accepts a
/// binding name from the dependency graph (`cluster`, `scc --binding`,
/// `describe`, `inspect-source --id`) routes through. Spec-edit verbs
/// (`bindings rename` / `assign` / `unassign` / `comment`) use
/// [`crate::binding::resolve_unambiguous`] instead — they need YAML
/// member positions, not dependency-graph node refs.
pub fn resolve_binding_source_units<'a>(
    graph: &'a DependencyGraphReport,
    name: &str,
) -> Vec<&'a SourceUnitReport> {
    let mut by_minified: Vec<&SourceUnitReport> = Vec::new();
    let mut by_readable: Vec<&SourceUnitReport> = Vec::new();
    for node in &graph.nodes {
        let mut matched_minified = false;
        let mut matched_readable = false;
        for binding in &node.declared_bindings {
            if binding.binding == *name {
                matched_minified = true;
            }
            if binding.export_name == *name {
                matched_readable = true;
            }
        }
        if matched_minified {
            by_minified.push(node);
        } else if matched_readable {
            by_readable.push(node);
        }
    }
    by_minified.extend(by_readable);
    by_minified
}

/// Result of resolving a selection to source-unit IDs.
/// `unknown_binding_ids` is non-empty only for module-path selections
/// whose YAML claims bindings the dependency graph does not declare.
#[derive(Debug, Clone)]
struct ResolvedSelection {
    source_unit_ids: Vec<String>,
    unknown_binding_ids: Vec<String>,
}

#[derive(Debug)]
pub struct SourceUnitNotFound {
    source_unit_id: String,
}

impl Display for SourceUnitNotFound {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "source-unit ID {:?} not found in dependency graph",
            self.source_unit_id
        )
    }
}

impl Error for SourceUnitNotFound {}

fn resolve_source_unit_ids(
    selection: &SelectionKind,
    graph: &DependencyGraphReport,
    common: &CommonArgs,
    size_cap_lines: usize,
    source_root: Option<&Path>,
    propose: Option<&ProposalsReport>,
) -> Result<ResolvedSelection> {
    let mut unknown_binding_ids: Vec<String> = Vec::new();
    let mut source_unit_ids: Vec<String> = match selection {
        SelectionKind::SourceUnit(source_unit_id) => {
            if graph.nodes.iter().any(|node| node.id == *source_unit_id) {
                vec![source_unit_id.clone()]
            } else {
                return Err(SourceUnitNotFound {
                    source_unit_id: source_unit_id.clone(),
                }
                .into());
            }
        }
        SelectionKind::ModulePath(module_path) => {
            let (resolved, unknown) =
                resolve_module_path_source_unit_ids(graph, common, source_root, module_path)?;
            unknown_binding_ids = unknown;
            resolved
        }
        SelectionKind::Module(module_id) => {
            let source_unit_ids: Vec<String> = graph
                .nodes
                .iter()
                .filter(|node| node.destination.as_str() == *module_id)
                .map(|node| node.id.clone())
                .collect();
            if source_unit_ids.is_empty()
                && !graph
                    .quotient
                    .nodes
                    .iter()
                    .any(|module| module.key.as_str() == *module_id)
            {
                bail!("module id {module_id:?} not found in dependency graph");
            }
            source_unit_ids
        }
        SelectionKind::Binding(binding_id) => resolve_binding_source_units(graph, binding_id)
            .into_iter()
            .map(|node| node.id.clone())
            .collect(),
        SelectionKind::Proposal(proposal_id) => {
            let proposal_source_unit_ids = if let Some(propose) = propose {
                source_unit_ids_for_proposal(propose, proposal_id)
            } else {
                let propose = propose_from_graph(
                    graph,
                    &ProposalOptions {
                        dependency_graph_path: common.dependency_graph_path.clone(),
                        modules_root: common.modules_root.clone(),
                        tree_config: None,
                        source_root: source_root.map(Path::to_path_buf),
                        size_cap_lines,
                    },
                )?;
                source_unit_ids_for_proposal(&propose, proposal_id)
            };
            if let Some(source_unit_ids) = proposal_source_unit_ids {
                source_unit_ids
            } else {
                bail!(
                    "proposal id {proposal_id:?} not found in current module proposals; run `debundle modules propose` to list current proposal ids"
                );
            }
        }
        SelectionKind::AtomicGroup(atomic_group_id) => graph
            .atomic_graph
            .nodes
            .iter()
            .find(|atomic_group| atomic_group.id == *atomic_group_id)
            .map(|atomic_group| atomic_group.source_unit_ids.clone())
            .unwrap_or_default(),
        SelectionKind::Diagnostic(diagnostic_id) => {
            let diagnostic_source_unit_ids = if let Some(propose) = propose {
                source_unit_ids_for_diagnostic(propose, diagnostic_id)
            } else {
                let propose = propose_from_graph(
                    graph,
                    &ProposalOptions {
                        dependency_graph_path: common.dependency_graph_path.clone(),
                        modules_root: common.modules_root.clone(),
                        tree_config: None,
                        source_root: source_root.map(Path::to_path_buf),
                        size_cap_lines,
                    },
                )?;
                source_unit_ids_for_diagnostic(&propose, diagnostic_id)
            };
            if let Some(source_unit_ids) = diagnostic_source_unit_ids {
                source_unit_ids
            } else {
                bail!(
                    "diagnostic id {diagnostic_id:?} not found in current module proposal diagnostics; run `debundle modules propose` to list current diagnostic ids"
                );
            }
        }
    };
    source_unit_ids.sort();
    source_unit_ids.dedup();
    if source_unit_ids.is_empty() {
        bail!("selection did not resolve to any source-unit IDs");
    }
    Ok(ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    })
}

/// Resolve a module-path selection to `(source_unit_ids, unknown_binding_ids)`.
/// Bindings the YAML claims but the dependency graph does not declare are
/// returned (not swallowed) so callers can surface partial misses;
/// only the all-unknown case is a hard error.
fn resolve_module_path_source_unit_ids(
    graph: &DependencyGraphReport,
    common: &CommonArgs,
    source_root: Option<&Path>,
    module_path: &str,
) -> Result<(Vec<String>, Vec<String>)> {
    let yaml_path = common.modules_root.join(format!("{module_path}.yaml"));
    let claims = read_module_claims(&yaml_path)
        .with_context(|| format!("reading module YAML {}", yaml_path.display()))?;
    if claims.is_empty() {
        bail!("module {module_path:?} has no members or anonymous_statements; nothing to describe");
    }

    let binding_to_source_units = binding_to_source_units(graph);
    let mut source_unit_ids = BTreeSet::<String>::new();
    let mut unknown_binding_ids = Vec::<String>::new();
    for binding in &claims.bindings {
        if let Some(binding_source_unit_ids) = binding_to_source_units.get(binding) {
            source_unit_ids.extend(binding_source_unit_ids.iter().cloned());
        } else {
            unknown_binding_ids.push(binding.clone());
        }
    }
    // The module's claims resolve jointly with every other module's, as in
    // `run`.
    let module_files = collect_module_files(&common.modules_root)?;
    let all_claims = module_files
        .iter()
        .map(|file| read_module_claims(file))
        .collect::<Result<Vec<_>>>()?;
    let index = module_files
        .iter()
        .position(|file| module_path_from_file(file, &common.modules_root) == module_path)
        .with_context(|| format!("module {module_path:?} is not in the modules tree"))?;
    let resolved = resolve_source_claims_of(
        graph,
        &common.dependency_graph_path,
        &common.modules_root,
        source_root,
        &module_files
            .iter()
            .zip(&all_claims)
            .map(|(file, claims)| SourceClaimSet {
                module_path: file,
                member_selectors: &claims.member_selectors,
                source_matches: &claims.source_matches,
                anonymous_selectors: &claims.anonymous_selectors,
            })
            .collect::<Vec<_>>(),
        index,
    )?;
    for binding in &resolved.bindings {
        if let Some(binding_source_unit_ids) = binding_to_source_units.get(binding) {
            source_unit_ids.extend(binding_source_unit_ids.iter().cloned());
        } else {
            unknown_binding_ids.push(binding.clone());
        }
    }
    for source_unit in &resolved.anonymous_source_units {
        if let Some(node) = graph.nodes.get(source_unit.0) {
            source_unit_ids.insert(node.id.clone());
        }
    }

    if source_unit_ids.is_empty() && !unknown_binding_ids.is_empty() {
        bail!(
            "module {module_path:?} declares bindings that do not appear in the dependency graph: {}",
            unknown_binding_ids.join(", ")
        );
    }
    Ok((source_unit_ids.into_iter().collect(), unknown_binding_ids))
}

fn source_unit_ids_for_proposal(
    propose: &ProposalsReport,
    proposal_id: &str,
) -> Option<Vec<String>> {
    propose
        .proposals
        .iter()
        .find(|proposal| proposal.proposed_module_id == *proposal_id)
        .map(|proposal| proposal.source_unit_ids.clone())
}

fn source_unit_ids_for_diagnostic(
    propose: &ProposalsReport,
    diagnostic_id: &str,
) -> Option<Vec<String>> {
    propose
        .diagnostics
        .iter()
        .find(|diagnostic| diagnostic.diagnostic_id == *diagnostic_id)
        .map(|diagnostic| diagnostic.source_unit_ids.clone())
}

fn binding_homes(modules_root: &Path) -> Result<Vec<BindingHomeReport>> {
    let mut homes = BTreeSet::new();
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            let Some(binding_selector) = member.selector.binding else {
                continue;
            };
            let binding = binding_selector.name;
            homes.insert(BindingHomeReport {
                binding,
                name: member.name.unwrap_or_default(),
                source_kind: BindingHomeSourceKind::Module,
                path: module_path.clone(),
            });
        }
    }
    let patches_path = default_binding_patches_path(modules_root);
    for member in load_binding_patch_members(modules_root)? {
        let Some(binding_selector) = member.selector.binding else {
            continue;
        };
        let binding = binding_selector.name;
        homes.insert(BindingHomeReport {
            binding,
            name: member.name.unwrap_or_default(),
            source_kind: BindingHomeSourceKind::BindingPatch,
            path: patches_path.display().to_string(),
        });
    }
    Ok(homes.into_iter().collect())
}
