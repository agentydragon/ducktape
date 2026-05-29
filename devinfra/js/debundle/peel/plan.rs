//! Agent-facing read-only peel-planning workbench.
//!
//! These subcommands are for agents maintaining a debundle spec. They expose
//! stable JSON operations over the owner graph and spec tree instead of
//! human-oriented "views".

use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

use super::factorize::{FactorizeDiagnosticReport, FactorizeProposal, PeelFactorizeOptions};
use super::factorize::{PeelFactorizeReport, analyze_peel_factorize};
use anonymous_resolution::{AnonymousStatementClaimSet, resolve_anonymous_statement_claims};
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, Subcommand, ValueEnum};
use serde::Serialize;

use analysis::{
    AtomicUnitEdgeReport, AtomicUnitReport, BindingReport, OwnerGraphEdgeReport,
    OwnerGraphNodeReport, OwnerGraphReport, QuotientEdgeReport, SourceLocation,
};
use spec_modules::{
    collect_module_files, default_binding_patches_path, load_binding_patch_members,
    module_path_from_file, read_module_claims, read_module_file,
};

#[derive(Debug, ClapArgs)]
pub struct PeelArgs {
    #[command(subcommand)]
    command: PeelCommand,
}

#[derive(Debug, Subcommand)]
enum PeelCommand {
    /// Deprecated alias for `debundle modules propose`.
    #[command(name = "plan-work")]
    PlanWork(PlanWorkArgs),
    /// Deprecated alias for `debundle atoms`.
    Units(UnitsArgs),
    /// Deprecated alias for `debundle coverage`.
    #[command(name = "patch-plan")]
    PatchPlan(PatchPlanArgs),
    /// Deprecated alias for `debundle describe <id>`.
    Explain(ExplainArgs),
    /// Deprecated alias for `debundle show-source <id>`.
    #[command(name = "source-slice")]
    SourceSlice(SourceSliceArgs),
    /// Deprecated alias for `debundle graph-summary`.
    #[command(name = "graph-summary")]
    GraphSummary(GraphSummaryArgs),
}

#[derive(Debug, Clone, ClapArgs)]
pub struct CommonArgs {
    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct PlanWorkArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Hard line ceiling per emitted proposal.
    #[arg(long = "size-cap-lines", default_value_t = 10_000)]
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
pub struct UnitsArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of units to emit. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Filter to units containing at least one residual owner.
    #[arg(long = "residual-only")]
    pub residual_only: bool,

    /// Filter to units with at least one renamed export.
    #[arg(long = "readable-only")]
    pub readable_only: bool,

    /// Also group emitted units by current destination.
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

    /// Also run the proposal factorizer to populate matching proposal ids.
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
    #[arg(long = "size-cap-lines", default_value_t = 10_000)]
    pub size_cap_lines: usize,

    /// Maximum number of largest residual units to emit. Zero means unlimited.
    #[arg(long, default_value_t = 10)]
    pub limit: usize,

    /// Also run the proposal factorizer to include proposal/diagnostic counts.
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

#[derive(Debug, Clone, ClapArgs)]
pub struct ExplainArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    #[command(flatten)]
    pub selection: SelectionArgs,

    /// Hard line ceiling used when resolving `--proposal-id`.
    #[arg(long = "size-cap-lines", default_value_t = 10_000)]
    pub size_cap_lines: usize,

    /// Root used to resolve relative `source_location.source_path`
    /// values when module-path selections claim anonymous statements.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Maximum number of rows to emit per report section. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Also run the proposal factorizer to annotate matching proposals and
    /// diagnostics. This is intentionally opt-in because it is expensive on
    /// large graphs.
    #[arg(long = "include-proposals")]
    pub include_proposals: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct SourceSliceArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    #[command(flatten)]
    pub selection: SelectionArgs,

    /// Hard line ceiling used when resolving `--proposal-id`.
    #[arg(long = "size-cap-lines", default_value_t = 10_000)]
    pub size_cap_lines: usize,

    /// Extra source lines to include around the selected owner span.
    #[arg(long = "context-lines", default_value_t = 20)]
    pub context_lines: usize,

    /// Root used to resolve relative `source_location.source_path` values.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct SelectionArgs {
    /// Select one owner id from `owner_graph.json`.
    #[arg(long = "owner-id")]
    pub owner_id: Option<String>,

    /// Select every owner claimed by this module path.
    #[arg(long = "module-path")]
    pub module_path: Option<String>,

    /// Select every owner assigned to this module id.
    #[arg(long = "module-id")]
    pub module_id: Option<String>,

    /// Select the owner that declares this input binding id.
    #[arg(long = "binding-id")]
    pub binding_id: Option<String>,

    /// Select a factorizer proposal by `proposed_module_id`.
    #[arg(long = "proposal-id")]
    pub proposal_id: Option<String>,

    /// Select one atomic unit id from `owner_graph.json`.
    #[arg(long = "unit-id")]
    pub unit_id: Option<String>,

    /// Select one factorizer diagnostic by `diagnostic_id`.
    #[arg(long = "diagnostic-id")]
    pub diagnostic_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct QueryReport {
    pub kind: QueryKind,
    pub value: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    Owner,
    Module,
    Binding,
    Proposal,
    Unit,
    Diagnostic,
}

#[derive(Debug, Clone)]
enum SelectionKind {
    Owner(String),
    ModulePath(String),
    Module(String),
    Binding(String),
    Proposal(String),
    Unit(String),
    Diagnostic(String),
}

impl SelectionKind {
    fn value(&self) -> &str {
        match self {
            Self::Owner(v)
            | Self::ModulePath(v)
            | Self::Module(v)
            | Self::Binding(v)
            | Self::Proposal(v)
            | Self::Unit(v)
            | Self::Diagnostic(v) => v,
        }
    }

    fn query_kind(&self) -> QueryKind {
        match self {
            Self::Owner(_) => QueryKind::Owner,
            Self::ModulePath(_) => QueryKind::Module,
            Self::Module(_) => QueryKind::Module,
            Self::Binding(_) => QueryKind::Binding,
            Self::Proposal(_) => QueryKind::Proposal,
            Self::Unit(_) => QueryKind::Unit,
            Self::Diagnostic(_) => QueryKind::Diagnostic,
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct UnitsReport {
    pub units: Vec<AtomicUnitReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub groups: Vec<UnitGroup>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct UnitGroup {
    pub destination: String,
    pub unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PlanWorkReport {
    #[serde(flatten)]
    pub report: PeelFactorizeReport,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExplainReport {
    pub query: QueryReport,
    pub owner_ids: Vec<String>,
    pub owners: Vec<OwnerGraphNodeReport>,
    pub neighbor_owners: Vec<OwnerGraphNodeReport>,
    pub bindings: Vec<BindingReport>,
    pub binding_homes: Vec<BindingHomeReport>,
    pub incoming_edges: Vec<OwnerGraphEdgeReport>,
    pub outgoing_edges: Vec<OwnerGraphEdgeReport>,
    pub atomic_units: Vec<AtomicUnitReport>,
    pub incoming_atomic_edges: Vec<AtomicUnitEdgeReport>,
    pub outgoing_atomic_edges: Vec<AtomicUnitEdgeReport>,
    pub quotient_edges: Vec<QuotientEdgeReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub factorize_proposals: Option<Vec<FactorizeProposal>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub factorize_diagnostics: Option<Vec<FactorizeDiagnosticReport>>,
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
    pub unit_ids: Vec<String>,
    pub complete_unit_ids: Vec<String>,
    pub split_unit_ids: Vec<String>,
    pub missing_binding_ids: Vec<String>,
    pub missing_owner_ids: Vec<String>,
    pub missing_anonymous_owner_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub matching_proposal_ids: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum PatchPlanStatus {
    CompleteUnits,
    SplitUnits,
    UnknownBindings,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GraphSummaryReport {
    pub owner_count: usize,
    pub owner_edge_count: usize,
    pub atomic_unit_count: usize,
    pub residual_atomic_unit_count: usize,
    pub atomic_edge_count: usize,
    pub module_count: usize,
    pub module_edge_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proposal_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostic_count: Option<usize>,
    pub largest_residual_units: Vec<UnitSummary>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct UnitSummary {
    pub unit_id: String,
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
pub struct SourceSliceReport {
    pub query: QueryReport,
    pub owner_ids: Vec<String>,
    pub slices: Vec<SourceSlice>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SourceSlice {
    pub source_path: String,
    pub resolved_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub context_start_line: usize,
    pub context_end_line: usize,
    pub text: String,
}

pub fn run_peel(args: PeelArgs) -> Result<()> {
    match args.command {
        PeelCommand::PlanWork(args) => {
            deprecation_notice("peel plan-work", "modules propose");
            print_json(&run_plan_work_report(&args)?).context("writing plan-work JSON")
        }
        PeelCommand::Units(args) => {
            deprecation_notice("peel units", "atoms");
            print_json(&run_units_report(&args)?).context("writing units JSON")
        }
        PeelCommand::PatchPlan(args) => {
            deprecation_notice("peel patch-plan", "coverage");
            print_json(&run_patch_plan_report(&args)?).context("writing patch-plan JSON")
        }
        PeelCommand::Explain(args) => {
            deprecation_notice("peel explain", "describe <id>");
            print_json(&run_explain_report(&args)?).context("writing explain JSON")
        }
        PeelCommand::SourceSlice(args) => {
            deprecation_notice("peel source-slice", "show-source <id>");
            print_json(&run_source_slice_report(&args)?).context("writing source-slice JSON")
        }
        PeelCommand::GraphSummary(args) => {
            deprecation_notice("peel graph-summary", "graph-summary");
            print_json(&run_graph_summary_report(&args)?).context("writing graph-summary JSON")
        }
    }
}

fn deprecation_notice(old: &str, new: &str) {
    eprintln!(
        "warning: `debundle {old}` is deprecated; use `debundle {new}` instead. The `peel` \
         namespace will be removed in a future release."
    );
}

pub fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
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

/// Emit a vec of items as ndjson when `format == Ndjson`, or as a
/// single pretty JSON / text-rendered document otherwise. Most query
/// commands wrap a `Vec<T>` (with optional summary) so this is the
/// shape the streaming format helps with.
pub fn print_ndjson_items<T: Serialize>(items: &[T]) -> Result<()> {
    for item in items {
        println!("{}", serde_json::to_string(item)?);
    }
    Ok(())
}

pub fn run_plan_work_report(args: &PlanWorkArgs) -> Result<PlanWorkReport> {
    let mut report = analyze_peel_factorize(&PeelFactorizeOptions {
        owner_graph_path: args.common.owner_graph_path.clone(),
        modules_root: args.common.modules_root.clone(),
        source_root: args.source_root.clone(),
        size_cap_lines: args.size_cap_lines,
    })?;
    let mut sections = BTreeMap::new();
    if args.limit > 0 {
        sort_factorize_diagnostics(&mut report.diagnostics);
    }
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

pub fn run_units_report(args: &UnitsArgs) -> Result<UnitsReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let mut units = graph.atomic_graph.nodes.clone();
    if args.residual_only {
        units.retain(|unit| {
            unit.destinations
                .iter()
                .any(|destination| graph.is_residual(destination))
        });
    }
    if args.readable_only {
        units.retain(|unit| {
            unit.members
                .iter()
                .any(|member| member.binding != member.export_name)
        });
    }
    units.sort_by_key(|unit| {
        (
            unit.source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX),
            unit.id.clone(),
        )
    });
    apply_limit(&mut units, args.limit);
    let groups = if args.by_destination {
        group_units_by_destination(&graph, &units)
    } else {
        Vec::new()
    };
    Ok(UnitsReport { units, groups })
}

pub fn run_patch_plan_report(args: &PatchPlanArgs) -> Result<PatchPlanReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let factorize = if args.include_proposals {
        Some(analyze_peel_factorize(&PeelFactorizeOptions {
            owner_graph_path: args.common.owner_graph_path.clone(),
            modules_root: args.common.modules_root.clone(),
            source_root: args.source_root.clone(),
            size_cap_lines: 10_000,
        })?)
    } else {
        None
    };
    let mut rows = patch_plan_rows(
        &graph,
        &args.common.owner_graph_path,
        &args.common.modules_root,
        args.source_root.as_deref(),
        factorize.as_ref(),
    )?;
    rows.sort_by_key(|row| (row.status, row.path.clone()));
    let summary = PatchPlanSummary {
        total_patch_sets: rows.len(),
        complete_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::CompleteUnits)
            .count(),
        split_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::SplitUnits)
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
    let graph = load_graph(&args.common.owner_graph_path)?;
    let factorize = if args.include_proposals {
        Some(analyze_peel_factorize(&PeelFactorizeOptions {
            owner_graph_path: args.common.owner_graph_path.clone(),
            modules_root: args.common.modules_root.clone(),
            source_root: args.source_root.clone(),
            size_cap_lines: args.size_cap_lines,
        })?)
    } else {
        None
    };
    let mut largest_residual_units: Vec<UnitSummary> = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|unit| {
            unit.destinations
                .iter()
                .any(|destination| graph.is_residual(destination))
        })
        .map(|unit| UnitSummary {
            unit_id: unit.id.clone(),
            size_lines_estimate: unit.size_lines_estimate,
            members: unit.members.clone(),
        })
        .collect();
    largest_residual_units.sort_by_key(|unit| {
        (
            std::cmp::Reverse(unit.size_lines_estimate),
            unit.unit_id.clone(),
        )
    });
    apply_limit(&mut largest_residual_units, args.limit);
    Ok(GraphSummaryReport {
        owner_count: graph.nodes.len(),
        owner_edge_count: graph.edges.len(),
        atomic_unit_count: graph.atomic_graph.nodes.len(),
        residual_atomic_unit_count: graph
            .atomic_graph
            .nodes
            .iter()
            .filter(|unit| {
                unit.destinations
                    .iter()
                    .any(|destination| graph.is_residual(destination))
            })
            .count(),
        atomic_edge_count: graph.atomic_graph.edges.len(),
        module_count: graph.quotient.nodes.len(),
        module_edge_count: graph.quotient.edges.len(),
        proposal_count: factorize.as_ref().map(|report| report.proposals.len()),
        diagnostic_count: factorize.as_ref().map(|report| report.diagnostics.len()),
        largest_residual_units,
    })
}

macro_rules! apply_limits {
    ($limit:expr, $sections:expr, $(($vec:expr, $name:expr)),+ $(,)?) => {
        $(
            apply_limit_with_metadata(&mut $vec, $limit, &mut $sections, $name);
        )+
    };
}

pub fn run_explain_report(args: &ExplainArgs) -> Result<ExplainReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let selection = args.selection.selection_kind()?;
    let query = query_report(&selection);
    let selection_needs_factorize = matches!(
        &selection,
        SelectionKind::Proposal(_) | SelectionKind::Diagnostic(_)
    );
    let factorize = if args.include_proposals || selection_needs_factorize {
        Some(analyze_peel_factorize(&PeelFactorizeOptions {
            owner_graph_path: args.common.owner_graph_path.clone(),
            modules_root: args.common.modules_root.clone(),
            source_root: None,
            size_cap_lines: args.size_cap_lines,
        })?)
    } else {
        None
    };
    let include_factorize_sections = factorize.is_some();
    let owner_ids = resolve_owner_ids(
        &selection,
        &graph,
        &args.common,
        args.size_cap_lines,
        args.source_root.as_deref(),
        factorize.as_ref(),
    )?;
    let owner_set: BTreeSet<String> = owner_ids.iter().cloned().collect();
    let mut owners = owners_for_ids(&graph, &owner_set);

    let mut neighbor_ids = BTreeSet::new();
    let mut incoming_edges: Vec<OwnerGraphEdgeReport> = graph
        .edges
        .iter()
        .filter(|edge| owner_set.contains(&edge.target))
        .inspect(|edge| {
            if !owner_set.contains(&edge.source) {
                neighbor_ids.insert(edge.source.clone());
            }
        })
        .cloned()
        .collect();
    let mut outgoing_edges: Vec<OwnerGraphEdgeReport> = graph
        .edges
        .iter()
        .filter(|edge| owner_set.contains(&edge.source))
        .inspect(|edge| {
            if !owner_set.contains(&edge.target) {
                neighbor_ids.insert(edge.target.clone());
            }
        })
        .cloned()
        .collect();
    let mut neighbor_owners = owners_for_ids(&graph, &neighbor_ids);
    let selected_unit_ids = atomic_unit_ids_for_owner_set(&graph, &owner_set);
    let mut atomic_units = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|unit| selected_unit_ids.contains(&unit.id))
        .cloned()
        .collect();
    let mut incoming_atomic_edges: Vec<AtomicUnitEdgeReport> = graph
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_unit_ids.contains(&edge.target))
        .cloned()
        .collect();
    let mut outgoing_atomic_edges: Vec<AtomicUnitEdgeReport> = graph
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_unit_ids.contains(&edge.source))
        .cloned()
        .collect();

    let mut bindings: Vec<BindingReport> = owners
        .iter()
        .flat_map(|owner| owner.declared_bindings.iter().cloned())
        .collect();
    bindings.sort();
    bindings.dedup();
    let binding_ids: BTreeSet<String> = bindings
        .iter()
        .map(|binding| binding.binding.to_string())
        .collect();
    let mut binding_homes = binding_homes(&args.common.modules_root, &binding_ids)?;

    let selected_destinations: BTreeSet<analysis::ModuleKey> = owners
        .iter()
        .map(|owner| owner.destination.clone())
        .collect();
    let mut quotient_edges = graph
        .quotient
        .edges
        .iter()
        .filter(|edge| {
            selected_destinations.contains(&edge.source)
                || selected_destinations.contains(&edge.target)
        })
        .cloned()
        .collect();

    let (mut factorize_proposals, mut factorize_diagnostics) = if let Some(factorize) = factorize {
        (
            factorize
                .proposals
                .into_iter()
                .filter(|proposal| overlaps(&proposal.owner_ids, &owner_set))
                .collect(),
            factorize
                .diagnostics
                .into_iter()
                .filter(|diagnostic| overlaps(&diagnostic.owner_ids, &owner_set))
                .collect(),
        )
    } else {
        (Vec::new(), Vec::new())
    };
    let mut limited_owner_ids = owner_ids;

    let mut sections = BTreeMap::new();
    apply_limits!(
        args.limit,
        sections,
        (limited_owner_ids, "owner_ids"),
        (owners, "owners"),
        (neighbor_owners, "neighbor_owners"),
        (bindings, "bindings"),
        (binding_homes, "binding_homes"),
        (incoming_edges, "incoming_edges"),
        (outgoing_edges, "outgoing_edges"),
        (atomic_units, "atomic_units"),
        (incoming_atomic_edges, "incoming_atomic_edges"),
        (outgoing_atomic_edges, "outgoing_atomic_edges"),
        (quotient_edges, "quotient_edges"),
        (factorize_proposals, "factorize_proposals"),
        (factorize_diagnostics, "factorize_diagnostics"),
    );

    Ok(ExplainReport {
        query,
        owner_ids: limited_owner_ids,
        owners,
        neighbor_owners,
        bindings,
        binding_homes,
        incoming_edges,
        outgoing_edges,
        atomic_units,
        incoming_atomic_edges,
        outgoing_atomic_edges,
        quotient_edges,
        factorize_proposals: include_factorize_sections.then_some(factorize_proposals),
        factorize_diagnostics: include_factorize_sections.then_some(factorize_diagnostics),
        limits: limit_report(args.limit, sections),
    })
}

pub fn run_source_slice_report(args: &SourceSliceArgs) -> Result<SourceSliceReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let selection = args.selection.selection_kind()?;
    let query = query_report(&selection);
    let owner_ids = resolve_owner_ids(
        &selection,
        &graph,
        &args.common,
        args.size_cap_lines,
        args.source_root.as_deref(),
        None,
    )?;
    let owner_set: BTreeSet<String> = owner_ids.iter().cloned().collect();
    let owners = owners_for_ids(&graph, &owner_set);
    let spans = source_spans(&owners)?;
    let mut slices = Vec::new();
    for (source_path, location) in spans {
        let resolved = resolve_source_file(
            &source_path,
            args.source_root.as_deref(),
            &args.common.owner_graph_path,
            &args.common.modules_root,
        )?;
        let (context_start_line, context_end_line, text) = read_source_text(
            &resolved,
            location.start_line,
            location.end_line,
            args.context_lines,
        )
        .with_context(|| format!("reading source slice from {}", resolved.display()))?;
        slices.push(SourceSlice {
            source_path,
            resolved_path: resolved.display().to_string(),
            start_line: location.start_line,
            end_line: location.end_line,
            context_start_line,
            context_end_line,
            text,
        });
    }
    Ok(SourceSliceReport {
        query,
        owner_ids,
        slices,
    })
}

fn load_graph(path: &Path) -> Result<OwnerGraphReport> {
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
    if limit == 0 {
        return;
    }
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

fn limit_report(
    limit: usize,
    sections: BTreeMap<&'static str, LimitSectionReport>,
) -> Option<LimitReport> {
    (limit > 0).then_some(LimitReport { limit, sections })
}

fn sort_factorize_diagnostics(diagnostics: &mut [FactorizeDiagnosticReport]) {
    diagnostics.sort_by_key(|diagnostic| {
        (
            diagnostic.reason,
            diagnostic
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX),
            diagnostic.owner_ids.len(),
            diagnostic.diagnostic_id.clone(),
        )
    });
}

fn group_units_by_destination(
    graph: &OwnerGraphReport,
    units: &[AtomicUnitReport],
) -> Vec<UnitGroup> {
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for unit in units {
        for destination in &unit.destinations {
            // Group by the human-facing path (the module's identity),
            // resolved once via the module table; fall back to the raw
            // key if the table lacks the entry.
            let label = graph
                .module(destination)
                .map(|entry| entry.path.to_string())
                .unwrap_or_else(|| destination.0.clone());
            groups.entry(label).or_default().push(unit.id.clone());
        }
    }
    groups
        .into_iter()
        .map(|(destination, unit_ids)| UnitGroup {
            destination,
            unit_ids,
        })
        .collect()
}

fn owners_for_ids(
    graph: &OwnerGraphReport,
    owner_ids: &BTreeSet<String>,
) -> Vec<OwnerGraphNodeReport> {
    graph
        .nodes
        .iter()
        .filter(|owner| owner_ids.contains(&owner.id))
        .cloned()
        .collect()
}

fn atomic_unit_ids_for_owner_set(
    graph: &OwnerGraphReport,
    owner_ids: &BTreeSet<String>,
) -> BTreeSet<String> {
    graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|unit| unit.owner_ids.iter().any(|owner| owner_ids.contains(owner)))
        .map(|unit| unit.id.clone())
        .collect()
}

fn overlaps(owner_ids: &[String], selected: &BTreeSet<String>) -> bool {
    owner_ids.iter().any(|owner_id| selected.contains(owner_id))
}

fn patch_plan_rows(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    factorize: Option<&PeelFactorizeReport>,
) -> Result<Vec<PatchPlanRow>> {
    let binding_to_owner = binding_to_owner(graph);
    let unit_by_owner = unit_by_owner(graph);
    let unit_by_id: BTreeMap<String, &AtomicUnitReport> = graph
        .atomic_graph
        .nodes
        .iter()
        .map(|unit| (unit.id.clone(), unit))
        .collect();
    load_patch_sets(modules_root)?
        .into_iter()
        .map(|patch_set| {
            let mut requested_binding_ids: Vec<String> =
                patch_set.bindings.iter().cloned().collect();
            requested_binding_ids.sort();

            let mut requested_owner_ids = BTreeSet::<String>::new();
            let mut unknown_binding_ids = Vec::<String>::new();
            for binding in &requested_binding_ids {
                if let Some(owner_id) = binding_to_owner.get(binding) {
                    requested_owner_ids.insert(owner_id.clone());
                } else {
                    unknown_binding_ids.push(binding.clone());
                }
            }
            let claim_sets = [AnonymousStatementClaimSet {
                module_path: &patch_set.file,
                match_sources: &patch_set.anonymous_match_sources,
            }];
            let anonymous_owners = resolve_anonymous_statement_claims(
                graph,
                owner_graph_path,
                modules_root,
                source_root,
                &claim_sets,
            )?;
            for owner in &anonymous_owners[0] {
                if let Some(node) = graph.nodes.get(owner.0) {
                    requested_owner_ids.insert(node.id.clone());
                }
            }

            let mut unit_ids: BTreeSet<String> = BTreeSet::new();
            for owner_id in &requested_owner_ids {
                if let Some(unit_id) = unit_by_owner.get(owner_id) {
                    unit_ids.insert(unit_id.clone());
                }
            }

            let mut complete_unit_ids = Vec::<String>::new();
            let mut split_unit_ids = Vec::<String>::new();
            let mut missing_binding_ids = BTreeSet::<String>::new();
            let mut missing_owner_ids = BTreeSet::<String>::new();
            let mut missing_anonymous_owner_ids = BTreeSet::<String>::new();
            for unit_id in &unit_ids {
                let Some(unit) = unit_by_id.get(unit_id) else {
                    continue;
                };
                let unit_bindings: BTreeSet<String> =
                    unit.members.iter().map(|m| m.binding.to_string()).collect();
                let unit_owners: BTreeSet<String> = unit.owner_ids.iter().cloned().collect();
                let bindings_complete = unit_bindings
                    .iter()
                    .all(|binding| patch_set.bindings.contains(binding));
                let owners_complete = unit_owners
                    .iter()
                    .all(|owner_id| requested_owner_ids.contains(owner_id));
                if bindings_complete && owners_complete {
                    complete_unit_ids.push(unit_id.clone());
                } else {
                    split_unit_ids.push(unit_id.clone());
                    missing_binding_ids.extend(
                        unit_bindings
                            .into_iter()
                            .filter(|binding| !patch_set.bindings.contains(binding)),
                    );
                    missing_owner_ids.extend(
                        unit_owners
                            .into_iter()
                            .filter(|owner_id| !requested_owner_ids.contains(owner_id)),
                    );
                    missing_anonymous_owner_ids.extend(
                        unit.anonymous_statement_owner_ids
                            .iter()
                            .filter(|owner_id| !requested_owner_ids.contains(*owner_id))
                            .cloned(),
                    );
                }
            }
            let status = if !split_unit_ids.is_empty() {
                PatchPlanStatus::SplitUnits
            } else if !unknown_binding_ids.is_empty() {
                PatchPlanStatus::UnknownBindings
            } else {
                PatchPlanStatus::CompleteUnits
            };
            let matching_proposal_ids =
                factorize.map(|factorize| matching_proposal_ids(factorize, &requested_owner_ids));
            Ok(PatchPlanRow {
                path: patch_set.path,
                file: patch_set.file.display().to_string(),
                status,
                requested_binding_ids,
                unknown_binding_ids,
                unit_ids: unit_ids.into_iter().collect(),
                complete_unit_ids,
                split_unit_ids,
                missing_binding_ids: missing_binding_ids.into_iter().collect(),
                missing_owner_ids: missing_owner_ids.into_iter().collect(),
                missing_anonymous_owner_ids: missing_anonymous_owner_ids.into_iter().collect(),
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
    anonymous_match_sources: BTreeSet<String>,
}

fn load_patch_sets(modules_root: &Path) -> Result<Vec<PatchSet>> {
    let mut sets = Vec::<PatchSet>::new();
    let binding_patches_path = default_binding_patches_path(modules_root);
    let patch_bindings: BTreeSet<String> = load_binding_patch_members(modules_root)?
        .into_iter()
        .map(|member| member.selector.binding.name)
        .collect();
    if !patch_bindings.is_empty() {
        sets.push(PatchSet {
            path: "binding_patches".to_string(),
            file: binding_patches_path,
            bindings: patch_bindings,
            anonymous_match_sources: BTreeSet::new(),
        });
    }
    for file in collect_module_files(modules_root)? {
        let claims = read_module_claims(&file)?;
        if !claims.has_claims() {
            continue;
        }
        sets.push(PatchSet {
            path: module_path_from_file(&file, modules_root),
            file,
            bindings: claims.bindings,
            anonymous_match_sources: claims.anonymous_match_sources,
        });
    }
    Ok(sets)
}

fn binding_to_owner(graph: &OwnerGraphReport) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for node in &graph.nodes {
        for binding in &node.declared_bindings {
            out.insert(binding.binding.to_string(), node.id.clone());
        }
    }
    out
}

fn unit_by_owner(graph: &OwnerGraphReport) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for unit in &graph.atomic_graph.nodes {
        for owner_id in &unit.owner_ids {
            out.insert(owner_id.clone(), unit.id.clone());
        }
    }
    out
}

fn matching_proposal_ids(
    factorize: &PeelFactorizeReport,
    requested_owner_ids: &BTreeSet<String>,
) -> Vec<String> {
    if requested_owner_ids.is_empty() {
        return Vec::new();
    }
    factorize
        .proposals
        .iter()
        .filter(|proposal| {
            let proposal_owners: BTreeSet<String> = proposal.owner_ids.iter().cloned().collect();
            requested_owner_ids.is_subset(&proposal_owners)
        })
        .map(|proposal| proposal.proposed_module_id.clone())
        .collect()
}

impl SelectionArgs {
    fn selection_kind(&self) -> Result<SelectionKind> {
        let selected = [
            self.owner_id.as_ref(),
            self.module_path.as_ref(),
            self.module_id.as_ref(),
            self.binding_id.as_ref(),
            self.proposal_id.as_ref(),
            self.unit_id.as_ref(),
            self.diagnostic_id.as_ref(),
        ]
        .into_iter()
        .filter(|value| value.is_some())
        .count();
        if selected != 1 {
            bail!(
                "select exactly one of --owner-id, --module-path, --module-id, --binding-id, --proposal-id, --unit-id, or --diagnostic-id (got {selected})"
            );
        }
        if let Some(owner_id) = &self.owner_id {
            Ok(SelectionKind::Owner(owner_id.clone()))
        } else if let Some(module_path) = &self.module_path {
            Ok(SelectionKind::ModulePath(module_path.clone()))
        } else if let Some(module_id) = &self.module_id {
            Ok(SelectionKind::Module(module_id.clone()))
        } else if let Some(binding_id) = &self.binding_id {
            Ok(SelectionKind::Binding(binding_id.clone()))
        } else if let Some(proposal_id) = &self.proposal_id {
            Ok(SelectionKind::Proposal(proposal_id.clone()))
        } else if let Some(unit_id) = &self.unit_id {
            Ok(SelectionKind::Unit(unit_id.clone()))
        } else if let Some(diagnostic_id) = &self.diagnostic_id {
            Ok(SelectionKind::Diagnostic(diagnostic_id.clone()))
        } else {
            unreachable!("selected count already validated")
        }
    }
}

fn query_report(selection: &SelectionKind) -> QueryReport {
    QueryReport {
        kind: selection.query_kind(),
        value: selection.value().to_string(),
    }
}

/// Find every owner node whose `declared_bindings` matches `name`.
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
/// binding name from the owner graph (`cluster`, `scc --binding`,
/// `describe`, `show-source`) routes through. Spec-edit verbs
/// (`bindings rename` / `assign` / `unassign` / `comment`) use
/// [`crate::binding::resolve_unambiguous`] instead — they need YAML
/// member positions, not owner-graph node refs.
pub fn resolve_binding_owners<'a>(
    graph: &'a OwnerGraphReport,
    name: &str,
) -> Vec<&'a OwnerGraphNodeReport> {
    let mut by_minified: Vec<&OwnerGraphNodeReport> = Vec::new();
    let mut by_readable: Vec<&OwnerGraphNodeReport> = Vec::new();
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

fn resolve_owner_ids(
    selection: &SelectionKind,
    graph: &OwnerGraphReport,
    common: &CommonArgs,
    size_cap_lines: usize,
    source_root: Option<&Path>,
    factorize: Option<&PeelFactorizeReport>,
) -> Result<Vec<String>> {
    let mut owner_ids: Vec<String> = match selection {
        SelectionKind::Owner(owner_id) => {
            if graph.nodes.iter().any(|node| node.id == *owner_id) {
                vec![owner_id.clone()]
            } else {
                bail!("owner id {owner_id:?} not found in owner graph");
            }
        }
        SelectionKind::ModulePath(module_path) => {
            resolve_module_path_owner_ids(graph, common, source_root, module_path)?
        }
        SelectionKind::Module(module_id) => {
            let owner_ids: Vec<String> = graph
                .nodes
                .iter()
                .filter(|node| node.destination.as_str() == *module_id)
                .map(|node| node.id.clone())
                .collect();
            if owner_ids.is_empty()
                && !graph
                    .quotient
                    .nodes
                    .iter()
                    .any(|module| module.key.as_str() == *module_id)
            {
                bail!("module id {module_id:?} not found in owner graph");
            }
            owner_ids
        }
        SelectionKind::Binding(binding_id) => resolve_binding_owners(graph, binding_id)
            .into_iter()
            .map(|node| node.id.clone())
            .collect(),
        SelectionKind::Proposal(proposal_id) => {
            let proposal_owner_ids = if let Some(factorize) = factorize {
                owner_ids_for_proposal(factorize, proposal_id)
            } else {
                let factorize = analyze_peel_factorize(&PeelFactorizeOptions {
                    owner_graph_path: common.owner_graph_path.clone(),
                    modules_root: common.modules_root.clone(),
                    source_root: source_root.map(Path::to_path_buf),
                    size_cap_lines,
                })?;
                owner_ids_for_proposal(&factorize, proposal_id)
            };
            if let Some(owner_ids) = proposal_owner_ids {
                owner_ids
            } else {
                bail!(
                    "proposal id {proposal_id:?} not found in current module proposals; run `debundle modules propose` to list current proposal ids"
                );
            }
        }
        SelectionKind::Unit(unit_id) => graph
            .atomic_graph
            .nodes
            .iter()
            .find(|unit| unit.id == *unit_id)
            .map(|unit| unit.owner_ids.clone())
            .unwrap_or_default(),
        SelectionKind::Diagnostic(diagnostic_id) => {
            let diagnostic_owner_ids = if let Some(factorize) = factorize {
                owner_ids_for_diagnostic(factorize, diagnostic_id)
            } else {
                let factorize = analyze_peel_factorize(&PeelFactorizeOptions {
                    owner_graph_path: common.owner_graph_path.clone(),
                    modules_root: common.modules_root.clone(),
                    source_root: source_root.map(Path::to_path_buf),
                    size_cap_lines,
                })?;
                owner_ids_for_diagnostic(&factorize, diagnostic_id)
            };
            if let Some(owner_ids) = diagnostic_owner_ids {
                owner_ids
            } else {
                bail!(
                    "diagnostic id {diagnostic_id:?} not found in current module proposal diagnostics; run `debundle modules propose` to list current diagnostic ids"
                );
            }
        }
    };
    owner_ids.sort();
    owner_ids.dedup();
    if owner_ids.is_empty() {
        bail!("selection did not resolve to any owner ids");
    }
    Ok(owner_ids)
}

fn resolve_module_path_owner_ids(
    graph: &OwnerGraphReport,
    common: &CommonArgs,
    source_root: Option<&Path>,
    module_path: &str,
) -> Result<Vec<String>> {
    let yaml_path = common.modules_root.join(format!("{module_path}.yaml"));
    let claims = read_module_claims(&yaml_path)
        .with_context(|| format!("reading module YAML {}", yaml_path.display()))?;
    if !claims.has_claims() {
        bail!("module {module_path:?} has no members or anonymous_statements; nothing to describe");
    }

    let binding_to_owner = binding_to_owner(graph);
    let mut owner_ids = BTreeSet::<String>::new();
    let mut unknown_binding_ids = Vec::<String>::new();
    for binding in &claims.bindings {
        if let Some(owner_id) = binding_to_owner.get(binding) {
            owner_ids.insert(owner_id.clone());
        } else {
            unknown_binding_ids.push(binding.clone());
        }
    }

    let claim_sets = [AnonymousStatementClaimSet {
        module_path: &yaml_path,
        match_sources: &claims.anonymous_match_sources,
    }];
    let anonymous_owners = resolve_anonymous_statement_claims(
        graph,
        &common.owner_graph_path,
        &common.modules_root,
        source_root,
        &claim_sets,
    )?;
    for owner in &anonymous_owners[0] {
        if let Some(node) = graph.nodes.get(owner.0) {
            owner_ids.insert(node.id.clone());
        }
    }

    if owner_ids.is_empty() && !unknown_binding_ids.is_empty() {
        bail!(
            "module {module_path:?} declares bindings that do not appear in the owner graph: {}",
            unknown_binding_ids.join(", ")
        );
    }
    Ok(owner_ids.into_iter().collect())
}

fn owner_ids_for_proposal(
    factorize: &PeelFactorizeReport,
    proposal_id: &str,
) -> Option<Vec<String>> {
    factorize
        .proposals
        .iter()
        .find(|proposal| proposal.proposed_module_id == *proposal_id)
        .map(|proposal| proposal.owner_ids.clone())
}

fn owner_ids_for_diagnostic(
    factorize: &PeelFactorizeReport,
    diagnostic_id: &str,
) -> Option<Vec<String>> {
    factorize
        .diagnostics
        .iter()
        .find(|diagnostic| diagnostic.diagnostic_id == *diagnostic_id)
        .map(|diagnostic| diagnostic.owner_ids.clone())
}

fn binding_homes(
    modules_root: &Path,
    binding_ids: &BTreeSet<String>,
) -> Result<Vec<BindingHomeReport>> {
    let mut homes = BTreeSet::new();
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            let binding = member.selector.binding.name;
            if binding_ids.contains(&binding) {
                homes.insert(BindingHomeReport {
                    binding,
                    name: member.name.unwrap_or_default(),
                    source_kind: BindingHomeSourceKind::Module,
                    path: module_path.clone(),
                });
            }
        }
    }
    let patches_path = default_binding_patches_path(modules_root);
    for member in load_binding_patch_members(modules_root)? {
        let binding = member.selector.binding.name;
        if binding_ids.contains(&binding) {
            homes.insert(BindingHomeReport {
                binding,
                name: member.name.unwrap_or_default(),
                source_kind: BindingHomeSourceKind::BindingPatch,
                path: patches_path.display().to_string(),
            });
        }
    }
    Ok(homes.into_iter().collect())
}

fn source_spans(owners: &[OwnerGraphNodeReport]) -> Result<BTreeMap<String, SourceLocation>> {
    let mut spans: BTreeMap<String, SourceLocation> = BTreeMap::new();
    for owner in owners {
        let Some(location) = &owner.source_location else {
            continue;
        };
        spans
            .entry(location.source_path.clone())
            .and_modify(|span| span.expand_to(location))
            .or_insert_with(|| location.clone());
    }
    if spans.is_empty() {
        bail!("selected owners do not have source locations");
    }
    Ok(spans)
}

fn resolve_source_file(
    source_path: &str,
    source_root: Option<&Path>,
    owner_graph_path: &Path,
    modules_root: &Path,
) -> Result<PathBuf> {
    let mut candidates = Vec::new();
    let source = PathBuf::from(source_path);
    if source.is_absolute() {
        candidates.push(source);
    } else {
        if let Some(root) = source_root {
            candidates.push(root.join(source_path));
        }
        if let Ok(cwd) = env::current_dir() {
            candidates.push(cwd.join(source_path));
        }
        push_relative_candidate(&mut candidates, owner_graph_path.parent(), source_path);
        push_relative_candidate(
            &mut candidates,
            owner_graph_path.parent().and_then(Path::parent),
            source_path,
        );
        push_relative_candidate(&mut candidates, modules_root.parent(), source_path);
        push_relative_candidate(
            &mut candidates,
            modules_root.parent().and_then(Path::parent),
            source_path,
        );
    }
    dedup_paths(&mut candidates);
    for candidate in &candidates {
        if candidate.is_file() {
            return Ok(candidate.clone());
        }
    }
    bail!(
        "could not resolve source path {source_path:?}; pass --source-root. Tried: {}",
        candidates
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn push_relative_candidate(candidates: &mut Vec<PathBuf>, root: Option<&Path>, source_path: &str) {
    if let Some(root) = root {
        candidates.push(root.join(source_path));
    }
}

fn dedup_paths(paths: &mut Vec<PathBuf>) {
    let mut seen = BTreeSet::new();
    paths.retain(|path| seen.insert(path.display().to_string()));
}

fn read_source_text(
    path: &Path,
    start_line: usize,
    end_line: usize,
    context_lines: usize,
) -> Result<(usize, usize, String)> {
    let body = fs::read_to_string(path)?;
    let lines: Vec<&str> = body.lines().collect();
    if lines.is_empty() {
        return Ok((1, 0, String::new()));
    }
    let context_start_line = start_line.saturating_sub(context_lines).max(1);
    let context_end_line = end_line.saturating_add(context_lines).min(lines.len());
    let start_index = context_start_line.saturating_sub(1).min(lines.len());
    let end_index = context_end_line.min(lines.len());
    let text = if start_index <= end_index {
        lines[start_index..end_index].join("\n")
    } else {
        String::new()
    };
    Ok((context_start_line, context_end_line, text))
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use analysis::{
        AtomicGraphReport, AtomicUnitEdgeReport, AtomicUnitReport, DepKind, ModuleKey,
        OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport,
        Purity, QuotientSccReport, SourceLocation, StatementKind, StatementOrdinal,
    };
    use tempfile::TempDir;

    use super::super::test_utils;
    use super::*;

    fn write(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn owner(id: &str, ordinal: usize, binding: &str, export_name: &str) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal),
            source_location: Some(SourceLocation {
                source_path: "static/index.js".to_string(),
                start_line: ordinal + 1,
                end_line: ordinal + 1,
            }),
            declared_bindings: vec![test_utils::member(binding, export_name)],
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: test_utils::module_ref("residual", true),
        }
    }

    fn atomic_unit(id: &str, owners: &[&OwnerGraphNodeReport]) -> AtomicUnitReport {
        let mut owner_ids = Vec::new();
        let mut members = Vec::new();
        let mut destinations = BTreeMap::<ModuleKey, ModuleKey>::new();
        let mut start_line = usize::MAX;
        let mut end_line = 0usize;
        let mut size_lines_estimate = 0usize;
        for owner in owners {
            owner_ids.push(owner.id.clone());
            members.extend(owner.declared_bindings.clone());
            destinations.insert(owner.destination.clone(), owner.destination.clone());
            if let Some(location) = &owner.source_location {
                start_line = start_line.min(location.start_line);
                end_line = end_line.max(location.end_line);
                size_lines_estimate += location.end_line + 1 - location.start_line;
            }
        }
        AtomicUnitReport {
            id: id.to_string(),
            owner_ids,
            members,
            anonymous_statement_owner_ids: Vec::new(),
            destinations: destinations.into_values().collect(),
            causes: Vec::new(),
            size_lines_estimate,
            source_line_range: Some([start_line, end_line]),
            ordinal_span: 0,
        }
    }

    fn atomic_edge(
        id: &str,
        source: &str,
        target: &str,
        owner_edge_id: &str,
    ) -> AtomicUnitEdgeReport {
        AtomicUnitEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kinds: vec![DepKind::EagerUse],
            owner_edge_ids: vec![owner_edge_id.to_string()],
            constrains_init_order: true,
        }
    }

    fn graph_fixture() -> OwnerGraphReport {
        let zz = owner("owner:0", 1, "ZZ", "PaymentError");
        let aa = owner("owner:1", 2, "aa", "aa");
        let nodes = vec![zz.clone(), aa.clone()];
        let module_nodes = test_utils::module_table(nodes.iter().map(|n| &n.destination));
        OwnerGraphReport {
            chunk_id: "static/index".to_string(),
            nodes,
            edges: vec![OwnerGraphEdgeReport {
                id: "edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:0".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: Some("ZZ".into()),
                statement_ordinal: StatementOrdinal(2),
                constrains_init_order: true,
                role: None,
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: module_nodes,
                edges: Vec::new(),
                sccs: Vec::<QuotientSccReport>::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: vec![
                    atomic_unit("atomic:0", &[&zz]),
                    atomic_unit("atomic:1", &[&aa]),
                ],
                edges: vec![atomic_edge(
                    "atomic_edge:0",
                    "atomic:1",
                    "atomic:0",
                    "edge:0",
                )],
            },
        }
    }

    fn fixture_with_graph(graph: OwnerGraphReport) -> (TempDir, CommonArgs) {
        let temp = tempfile::tempdir().unwrap();
        let graph_path = temp.path().join("owner_graph.json");
        let modules_root = temp.path().join("spec/modules");
        write(&graph_path, &serde_json::to_string_pretty(&graph).unwrap());
        write(
            &temp.path().join("spec/binding_patches.yaml"),
            "members:\n  - name: PaymentError\n    selector:\n      binding:\n        name: ZZ\n",
        );
        write(&modules_root.join(".keep"), "");
        write(
            &temp.path().join("static/index.js"),
            "const first = 1;\nconst ZZ = class PaymentError {};\nconst aa = ZZ;\n",
        );
        (
            temp,
            CommonArgs {
                owner_graph_path: graph_path,
                modules_root,
            },
        )
    }

    fn fixture() -> (TempDir, CommonArgs) {
        fixture_with_graph(graph_fixture())
    }

    #[test]
    fn plan_work_limit_keeps_sorted_prefix_and_reports_totals() {
        let (_temp, common) = fixture();
        let report = run_plan_work_report(&PlanWorkArgs {
            common,
            size_cap_lines: 10_000,
            source_root: None,
            limit: 1,
            format: None,
        })
        .unwrap();

        assert_eq!(report.report.proposals.len(), 1);
        assert!(report.report.proposals[0].landable_today);
        let limits = report.limits.unwrap();
        assert_eq!(limits.limit, 1);
        assert_eq!(limits.sections["proposals"].total, 1);
        assert_eq!(limits.sections["proposals"].emitted, 1);
        assert!(!limits.sections["proposals"].truncated);
    }

    #[test]
    fn units_emits_units_and_groups() {
        let (_temp, common) = fixture();
        let report = run_units_report(&UnitsArgs {
            common,
            limit: 0,
            residual_only: true,
            readable_only: true,
            by_destination: true,
            format: None,
        })
        .unwrap();
        assert_eq!(report.units.len(), 1);
        assert_eq!(report.units[0].members[0].binding, "ZZ");
        assert_eq!(report.groups.len(), 1);
    }

    #[test]
    fn patch_plan_reports_split_atomic_units() {
        let (_temp, common) = fixture();
        let report = run_patch_plan_report(&PatchPlanArgs {
            common,
            limit: 0,
            include_proposals: false,
            source_root: None,
            format: None,
        })
        .unwrap();
        let row = report
            .rows
            .iter()
            .find(|row| row.path == "binding_patches")
            .expect("binding patch row");
        assert_eq!(row.status, PatchPlanStatus::CompleteUnits);
        assert_eq!(row.complete_unit_ids, vec!["atomic:0".to_string()]);
        assert_eq!(row.matching_proposal_ids, None);
    }

    #[test]
    fn patch_plan_includes_matching_proposals_on_request() {
        let (_temp, common) = fixture();
        let report = run_patch_plan_report(&PatchPlanArgs {
            common,
            limit: 0,
            include_proposals: true,
            source_root: None,
            format: None,
        })
        .unwrap();
        let row = report
            .rows
            .iter()
            .find(|row| row.path == "binding_patches")
            .expect("binding patch row");
        assert_eq!(
            row.matching_proposal_ids,
            Some(vec!["auto_partition_0000".to_string()])
        );
    }

    #[test]
    fn explain_binding_includes_graph_and_spec_context() {
        let (_temp, common) = fixture();
        let report = run_explain_report(&ExplainArgs {
            common,
            selection: SelectionArgs {
                owner_id: None,
                module_path: None,
                module_id: None,
                binding_id: Some("ZZ".to_string()),
                proposal_id: None,
                unit_id: None,
                diagnostic_id: None,
            },
            size_cap_lines: 10_000,
            source_root: None,
            limit: 0,
            include_proposals: true,
            format: None,
        })
        .unwrap();
        assert_eq!(report.owner_ids, vec!["owner:0"]);
        assert_eq!(report.incoming_edges.len(), 1);
        assert_eq!(report.neighbor_owners[0].id, "owner:1");
        assert_eq!(
            report.binding_homes[0].source_kind,
            BindingHomeSourceKind::BindingPatch
        );
        assert_eq!(report.atomic_units[0].id, "atomic:0");
        assert_eq!(
            report.factorize_proposals.as_ref().unwrap()[0].proposed_module_id,
            "auto_partition_0000"
        );
    }

    #[test]
    fn explain_binding_skips_factorizer_by_default() {
        let (_temp, common) = fixture();
        let report = run_explain_report(&ExplainArgs {
            common,
            selection: SelectionArgs {
                owner_id: None,
                module_path: None,
                module_id: None,
                binding_id: Some("ZZ".to_string()),
                proposal_id: None,
                unit_id: None,
                diagnostic_id: None,
            },
            size_cap_lines: 10_000,
            source_root: None,
            limit: 0,
            include_proposals: false,
            format: None,
        })
        .unwrap();

        assert_eq!(report.owner_ids, vec!["owner:0"]);
        assert!(report.factorize_proposals.is_none());
        assert!(report.factorize_diagnostics.is_none());
    }

    #[test]
    fn explain_limit_applies_per_section_and_reports_totals() {
        let mut graph = graph_fixture();
        graph.nodes.push(owner("owner:2", 3, "bb", "bb"));
        graph.nodes.push(owner("owner:3", 4, "cc", "cc"));
        graph.edges.push(OwnerGraphEdgeReport {
            id: "edge:1".to_string(),
            source: "owner:2".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: Some("ZZ".into()),
            statement_ordinal: StatementOrdinal(3),
            constrains_init_order: true,
            role: None,
        });
        graph.edges.push(OwnerGraphEdgeReport {
            id: "edge:2".to_string(),
            source: "owner:3".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: Some("ZZ".into()),
            statement_ordinal: StatementOrdinal(4),
            constrains_init_order: true,
            role: None,
        });

        let (_temp, common) = fixture_with_graph(graph);
        let report = run_explain_report(&ExplainArgs {
            common,
            selection: SelectionArgs {
                owner_id: None,
                module_path: None,
                module_id: None,
                binding_id: Some("ZZ".to_string()),
                proposal_id: None,
                unit_id: None,
                diagnostic_id: None,
            },
            size_cap_lines: 10_000,
            source_root: None,
            limit: 1,
            include_proposals: false,
            format: None,
        })
        .unwrap();

        assert_eq!(report.incoming_edges.len(), 1);
        assert_eq!(report.neighbor_owners.len(), 1);
        let limits = report.limits.unwrap();
        assert_eq!(limits.sections["incoming_edges"].total, 3);
        assert_eq!(limits.sections["incoming_edges"].emitted, 1);
        assert!(limits.sections["incoming_edges"].truncated);
        assert_eq!(limits.sections["neighbor_owners"].total, 3);
        assert_eq!(limits.sections["neighbor_owners"].emitted, 1);
        assert!(limits.sections["neighbor_owners"].truncated);
        assert_eq!(limits.sections["owner_ids"].total, 1);
        assert!(!limits.sections["owner_ids"].truncated);
    }

    #[test]
    fn source_slice_reads_context_from_resolved_source_root() {
        let (temp, common) = fixture();
        let report = run_source_slice_report(&SourceSliceArgs {
            common,
            selection: SelectionArgs {
                owner_id: Some("owner:0".to_string()),
                module_path: None,
                module_id: None,
                binding_id: None,
                proposal_id: None,
                unit_id: None,
                diagnostic_id: None,
            },
            size_cap_lines: 10_000,
            context_lines: 1,
            source_root: Some(temp.path().to_path_buf()),
            format: None,
        })
        .unwrap();
        assert_eq!(report.slices.len(), 1);
        assert_eq!(report.slices[0].context_start_line, 1);
        assert_eq!(report.slices[0].context_end_line, 3);
        assert!(report.slices[0].text.contains("class PaymentError"));
    }
}
