//! Agent-facing read-only peel-planning workbench.
//!
//! These subcommands are for agents maintaining a debundle spec. They expose
//! stable JSON operations over the dependency graph and spec tree instead of
//! human-oriented "views".

use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet};
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

use anonymous_resolution::{
    SourceClaimSet, resolve_source_claims, resolve_source_claims_of, resolve_source_file,
};
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, ValueEnum};
use serde::Serialize;

use analysis_vocabulary::execution_evidence::SourceLocation;
use graph::{
    AtomicGroupEdgeReport, AtomicGroupReport, BindingReport, DependencyEdgeReport,
    DependencyGraphInput, DependencyGraphReport, ProgramGraphReport, QuotientEdgeReport,
    SourceUnitReport,
};
use graph_provenance::VerifiedGraphSourceContext;
use spec_modules::{
    collect_module_files, module_path_from_file, read_module_claims, read_module_file,
};
use spec_tree::ModuleTreeScope;

#[derive(Debug, Clone, ClapArgs)]
pub struct CommonArgs {
    /// Path to `dependency_graph.json.gz` (debundler analysis output).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub dependency_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, ClapArgs)]
pub struct ProposeArgs {
    /// Path to the one chunk's dependency graph.
    #[arg(
        long = "graph",
        env = "DEBUNDLE_GRAPH",
        required_unless_present = "graphs_root",
        conflicts_with = "graphs_root"
    )]
    pub dependency_graph_path: Option<PathBuf>,

    /// Root containing a recursively discovered graph corpus. Requires
    /// --tree-config; discovery runs once for each configured chunk.
    #[arg(
        long = "graphs",
        required_unless_present = "dependency_graph_path",
        conflicts_with = "dependency_graph_path",
        requires = "tree_config"
    )]
    pub graphs_root: Option<PathBuf>,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Name the one new logical owner to append to the current import order.
    #[arg(long, conflicts_with = "graphs_root")]
    pub into: Vec<String>,
    /// Require these residual units to be extracted, with their full feasible
    /// successor closure.
    #[arg(long = "require", conflicts_with = "graphs_root")]
    pub required: Vec<String>,
    /// JSON array of units permitted to move. Must contain whole atomic groups.
    #[arg(long, conflicts_with = "graphs_root")]
    pub only_units: Option<PathBuf>,

    /// Root for the chunk's source files, used to preflight that the selected
    /// plan can be realized as authored selectors before it is executable.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Tree config for chunk-scoped module claims in a multi-chunk spec.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub tree_config: Option<PathBuf>,

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

    /// Maximum number of largest residual atomic groups to emit. Zero means unlimited.
    #[arg(long, default_value_t = 10)]
    pub limit: usize,

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
    AtomicGroup,
}

/// What `describe` / `inspect-source --id` can resolve; each variant names its query kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectionKind {
    /// One source-unit ID from `dependency_graph.json.gz`.
    SourceUnit(String),
    /// Every source unit claimed by this module path.
    ModulePath(String),
    /// Every source unit assigned to this module ID.
    Module(String),
    /// The source unit that declares this input binding id.
    Binding(String),
    /// One atomic group id from `dependency_graph.json.gz`.
    AtomicGroup(String),
}

impl SelectionKind {
    fn value(&self) -> &str {
        match self {
            Self::SourceUnit(v)
            | Self::ModulePath(v)
            | Self::Module(v)
            | Self::Binding(v)
            | Self::AtomicGroup(v) => v,
        }
    }

    fn query_kind(&self) -> QueryKind {
        match self {
            Self::SourceUnit(_) => QueryKind::SourceUnit,
            Self::ModulePath(_) => QueryKind::Module,
            Self::Module(_) => QueryKind::Module,
            Self::Binding(_) => QueryKind::Binding,
            Self::AtomicGroup(_) => QueryKind::AtomicGroup,
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

#[derive(Debug, Clone, Serialize)]
pub struct ExplainReport {
    pub native_integrity_witnesses: Vec<analysis_vocabulary::report_schema::NativeMutationWitness>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prototype_mutations:
        Vec<analysis_vocabulary::report_schema::NativePrototypeMutation>,
    pub query: QueryReport,
    pub source_unit_ids: Vec<String>,
    pub source_units: Vec<SourceUnitReport>,
    pub neighbor_source_units: Vec<SourceUnitReport>,
    pub bindings: Vec<BindingReport>,
    pub binding_homes: Vec<BindingHomeReport>,
    pub incoming_edges: Vec<DependencyEdgeReport>,
    pub outgoing_edges: Vec<DependencyEdgeReport>,
    pub edge_census: EdgeCensus,
    pub atomic_groups: Vec<AtomicGroupReport>,
    pub incoming_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub outgoing_atomic_edges: Vec<AtomicGroupEdgeReport>,
    pub quotient_edges: Vec<QuotientEdgeReport>,
    /// Bindings claimed by a module-path selection that do not
    /// appear in the dependency graph. Always empty for other selections.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unknown_binding_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EdgeCensus {
    pub incoming: Vec<EdgeCensusRow>,
    pub outgoing: Vec<EdgeCensusRow>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct EdgeCensusRow {
    pub edge_kind: DepKind,
    pub role: EdgeCensusRole,
    pub count: usize,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum EdgeCensusRole {
    Direct,
    PromotedAtInit,
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

impl LimitReport {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            sections: BTreeMap::new(),
        }
    }

    /// Truncate a section and record its original and emitted row counts.
    pub fn apply<T>(&mut self, section: &'static str, records: &mut Vec<T>) {
        let total = records.len();
        apply_limit(records, self.limit);
        let emitted = records.len();
        self.sections.insert(
            section,
            LimitSectionReport {
                total,
                emitted,
                truncated: emitted < total,
            },
        );
    }
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
#[value(rename_all = "snake_case")]
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

pub fn run_atomic_groups_report(args: &AtomicGroupsArgs) -> Result<AtomicGroupsReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
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
        group_atomic_groups_by_destination(graph, &atomic_groups)
    } else {
        Vec::new()
    };
    Ok(AtomicGroupsReport {
        atomic_groups,
        destination_groups,
    })
}

pub fn run_patch_plan_report(
    args: &PatchPlanArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<PatchPlanReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let mut rows = patch_plan_rows(
        graph,
        &args.common.dependency_graph_path,
        &args.common.modules_root,
        args.source_root.as_deref(),
        source_context,
        diagnostics,
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
    let mut limits = LimitReport::new(args.limit);
    limits.apply("rows", &mut rows);
    Ok(PatchPlanReport {
        rows,
        summary,
        limits: (args.limit > 0).then_some(limits),
    })
}

pub fn run_graph_summary_report(args: &GraphSummaryArgs) -> Result<GraphSummaryReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let mut residual_groups: Vec<_> = graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| {
            atomic_group
                .destinations
                .iter()
                .any(|destination| graph.is_unassigned_or_entry(destination))
        })
        .collect();
    let residual_atomic_group_count = residual_groups.len();
    residual_groups.sort_by_key(|group| (std::cmp::Reverse(group.size_lines_estimate), &group.id));
    apply_limit(&mut residual_groups, args.limit);
    let largest_residual_atomic_groups = residual_groups
        .into_iter()
        .map(|atomic_group| AtomicGroupSummary {
            atomic_group_id: atomic_group.id.clone(),
            size_lines_estimate: atomic_group.size_lines_estimate,
            members: atomic_group.members.clone(),
        })
        .collect();
    Ok(GraphSummaryReport {
        source_unit_count: graph.nodes.len(),
        dependency_edge_count: graph.edges.len(),
        atomic_group_count: graph.atomic_graph.nodes.len(),
        residual_atomic_group_count,
        atomic_edge_count: graph.atomic_graph.edges.len(),
        module_count: graph.quotient.nodes.len(),
        module_edge_count: graph.quotient.edges.len(),
        largest_residual_atomic_groups,
    })
}

/// One command's loaded graph and shared authoring lookups. This cache never
/// survives the invocation or suppresses the caller's compatibility check.
pub struct ExplainSession {
    graph: DependencyGraphInput,
    program_graph: ProgramGraphReport,
    common: CommonArgs,
    module_files: Vec<PathBuf>,
    source_root: Option<PathBuf>,
    source_context: Option<VerifiedGraphSourceContext>,
    binding_homes: Option<Vec<BindingHomeReport>>,
}

#[derive(Clone, Copy)]
struct SourceLookup<'a> {
    root: Option<&'a Path>,
    verified: Option<&'a VerifiedGraphSourceContext>,
}

impl ExplainSession {
    pub fn load(
        mut common: CommonArgs,
        tree_config: Option<PathBuf>,
        source_root: Option<PathBuf>,
        source_context: Option<VerifiedGraphSourceContext>,
    ) -> Result<Self> {
        // `--modules` may name the exact sole chunk subtree, which graph
        // compatibility accepts as a legacy scope. The configured resolver
        // expects the full tree root, so continue from the provenance-verified
        // root when one is available.
        if tree_config.is_some()
            && let Some(modules_root) = source_context
                .as_ref()
                .and_then(VerifiedGraphSourceContext::modules_root)
        {
            common.modules_root = modules_root.to_path_buf();
        }
        let graph = DependencyGraphInput::read(&common.dependency_graph_path)?;
        let report = graph.report();
        let module_files = if let Some(config) = &tree_config {
            let scope = ModuleTreeScope::from_config(config, &report.chunk_id)?;
            let mut module_files = Vec::new();
            for root in scope.roots() {
                let root = common.modules_root.join(root);
                if root.exists() {
                    module_files.extend(collect_module_files(&root)?);
                }
            }
            module_files.sort();
            module_files
        } else {
            collect_module_files(&common.modules_root)?
        };
        Ok(Self {
            program_graph: load_program_graph(&common.dependency_graph_path, report)?,
            graph,
            common,
            module_files,
            source_root,
            source_context,
            binding_homes: None,
        })
    }

    pub fn graph(&self) -> &DependencyGraphReport {
        self.graph.report()
    }

    pub fn module_paths(&self) -> Vec<String> {
        self.module_files
            .iter()
            .map(|path| module_path_from_file(path, &self.common.modules_root))
            .collect()
    }

    pub fn explain(
        &mut self,
        args: &ExplainArgs,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<ExplainReport> {
        if self.binding_homes.is_none() {
            self.binding_homes = Some(binding_homes(
                &self.common.modules_root,
                &self.module_files,
            )?);
        }
        explain_from_graph(self, args, diagnostics)
    }
}

fn explain_from_graph(
    session: &ExplainSession,
    args: &ExplainArgs,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ExplainReport> {
    let ExplainSession {
        graph,
        program_graph,
        common,
        source_root,
        source_context,
        binding_homes,
        ..
    } = session;
    let report = graph.report();
    let source_lookup = SourceLookup {
        root: source_root.as_deref(),
        verified: source_context.as_ref(),
    };
    let homes = binding_homes
        .as_deref()
        .expect("loaded by ExplainSession::explain");
    let selection = &args.selection;
    let query = query_report(selection);
    let ResolvedSelection {
        source_unit_ids,
        mut unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        graph,
        &ResolveSourceUnitContext {
            common,
            source_root: source_lookup.root,
            source_context: source_lookup.verified,
            module_files: &session.module_files,
        },
        diagnostics,
    )?;
    let mut source_units = source_units_for_ids(report, &source_unit_ids);

    let (mut incoming_edges, mut outgoing_edges) =
        selected_dependency_edges(graph, &source_unit_ids, args)?;
    let selected_atomic_group_ids = atomic_group_ids_for_source_unit_set(report, &source_unit_ids);
    let mut atomic_groups: Vec<AtomicGroupReport> = report
        .atomic_graph
        .nodes
        .iter()
        .filter(|atomic_group| selected_atomic_group_ids.contains(&atomic_group.id))
        .cloned()
        .collect();
    let mut incoming_atomic_edges: Vec<AtomicGroupEdgeReport> = report
        .atomic_graph
        .edges
        .iter()
        .filter(|edge| selected_atomic_group_ids.contains(&edge.target))
        .cloned()
        .collect();
    let mut outgoing_atomic_edges: Vec<AtomicGroupEdgeReport> = report
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
    let mut binding_homes: Vec<BindingHomeReport> = homes
        .iter()
        .filter(|home| binding_ids.contains(&home.binding))
        .cloned()
        .collect();

    let selected_destinations: BTreeSet<graph::ModuleKey> = source_units
        .iter()
        .map(|source_unit| source_unit.destination.clone())
        .collect();
    let mut quotient_edges: Vec<QuotientEdgeReport> = report
        .quotient
        .edges
        .iter()
        .filter(|edge| {
            selected_destinations.contains(&edge.source)
                || selected_destinations.contains(&edge.target)
        })
        .cloned()
        .collect();

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

    let edge_census = EdgeCensus {
        incoming: edge_census_rows(&incoming_edges),
        outgoing: edge_census_rows(&outgoing_edges),
    };

    let mut neighbor_ids = BTreeSet::new();
    for edge in &incoming_edges {
        if !source_unit_ids.contains(&edge.source) {
            neighbor_ids.insert(edge.source.clone());
        }
    }
    for edge in &outgoing_edges {
        if !source_unit_ids.contains(&edge.target) {
            neighbor_ids.insert(edge.target.clone());
        }
    }
    let mut neighbor_source_units = source_units_for_ids(report, &neighbor_ids);

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
        SelectionKind::ModulePath(_) | SelectionKind::Module(_) => {}
    }

    source_units.sort_by_key(|source_unit| !preferred_source_unit_ids.contains(&source_unit.id));
    let mut limited_source_unit_ids: Vec<_> = source_unit_ids.into_iter().collect();
    limited_source_unit_ids.sort_by_key(|id| !preferred_source_unit_ids.contains(id));

    let (mut native_integrity_witnesses, mut native_prototype_mutations) =
        native_integrity_context(program_graph, &source_units, &neighbor_source_units);
    let mut limits = LimitReport::new(args.limit);
    limits.apply(
        "native_integrity_witnesses",
        &mut native_integrity_witnesses,
    );
    limits.apply(
        "native_prototype_mutations",
        &mut native_prototype_mutations,
    );
    limits.apply("source_unit_ids", &mut limited_source_unit_ids);
    limits.apply("source_units", &mut source_units);
    limits.apply("neighbor_source_units", &mut neighbor_source_units);
    limits.apply("bindings", &mut bindings);
    limits.apply("binding_homes", &mut binding_homes);
    limits.apply("incoming_edges", &mut incoming_edges);
    limits.apply("outgoing_edges", &mut outgoing_edges);
    limits.apply("atomic_groups", &mut atomic_groups);
    limits.apply("incoming_atomic_edges", &mut incoming_atomic_edges);
    limits.apply("outgoing_atomic_edges", &mut outgoing_atomic_edges);
    limits.apply("quotient_edges", &mut quotient_edges);
    limits.apply("unknown_binding_ids", &mut unknown_binding_ids);

    Ok(ExplainReport {
        native_integrity_witnesses,
        native_prototype_mutations,
        query,
        source_unit_ids: limited_source_unit_ids,
        source_units,
        neighbor_source_units,
        bindings,
        binding_homes,
        incoming_edges,
        outgoing_edges,
        edge_census,
        atomic_groups,
        incoming_atomic_edges,
        outgoing_atomic_edges,
        quotient_edges,
        unknown_binding_ids,
        limits: Some(limits),
    })
}

/// Dependency edges into and out of the selected source units, filtered by
/// the requested direction and reasons.
fn selected_dependency_edges(
    graph: &DependencyGraphInput,
    source_unit_ids: &BTreeSet<String>,
    args: &ExplainArgs,
) -> Result<(Vec<DependencyEdgeReport>, Vec<DependencyEdgeReport>)> {
    let mut incoming_edges = Vec::<DependencyEdgeReport>::new();
    let mut outgoing_edges = Vec::<DependencyEdgeReport>::new();
    let direction_incoming = !matches!(args.direction, ExplainDirection::Outgoing);
    let direction_outgoing = !matches!(args.direction, ExplainDirection::Incoming);
    for source_unit_id in source_unit_ids {
        let source_unit = graph
            .source_unit_id(source_unit_id)
            .with_context(|| {
                format!(
                    "explain selection contains source unit {source_unit_id:?} absent from the checked graph input"
                )
            })?;
        if direction_incoming {
            for &edge_id in graph.graph().in_edges_of(source_unit) {
                let edge = graph.graph().edge(edge_id);
                let kind = edge.reason.kind();
                if args.reasons.is_empty() || args.reasons.contains(&kind) {
                    incoming_edges.push(graph.edge_report(edge_id).clone());
                }
            }
        }
        if direction_outgoing {
            for &edge_id in graph.graph().out_edges_of(source_unit) {
                let edge = graph.graph().edge(edge_id);
                let kind = edge.reason.kind();
                if args.reasons.is_empty() || args.reasons.contains(&kind) {
                    outgoing_edges.push(graph.edge_report(edge_id).clone());
                }
            }
        }
    }
    Ok((incoming_edges, outgoing_edges))
}

/// Native prototype mutations that touch a property the selected or neighbor
/// units check, and the integrity witnesses transitively affecting their roots.
fn native_integrity_context(
    program_graph: &ProgramGraphReport,
    source_units: &[SourceUnitReport],
    neighbor_source_units: &[SourceUnitReport],
) -> (
    Vec<analysis_vocabulary::report_schema::NativeMutationWitness>,
    Vec<analysis_vocabulary::report_schema::NativePrototypeMutation>,
) {
    let checked_properties: BTreeSet<_> = source_units
        .iter()
        .chain(neighbor_source_units)
        .filter_map(|unit| unit.synchronous_execution.as_ref())
        .flat_map(|execution| {
            execution
                .native_prototype_property_checks
                .iter()
                .map(|check| &check.property)
        })
        .collect();
    let native_prototype_mutations: Vec<_> = program_graph
        .native_prototype_mutations
        .iter()
        .filter(|mutation| {
            !checked_properties.is_empty()
                && mutation.properties.as_ref().is_none_or(|properties| {
                    properties
                        .iter()
                        .any(|property| checked_properties.contains(property))
                })
        })
        .cloned()
        .collect();
    let mut affected_roots: BTreeSet<_> = source_units
        .iter()
        .chain(neighbor_source_units)
        .filter_map(|unit| unit.synchronous_execution.as_ref())
        .flat_map(|execution| &execution.native_integrity_checks)
        .flat_map(|check| check.affected_roots.iter().copied())
        .collect();
    affected_roots.extend(
        native_prototype_mutations
            .iter()
            .flat_map(|mutation| mutation.key_dependencies.iter().copied()),
    );
    loop {
        let before = affected_roots.len();
        for witness in &program_graph.native_integrity_witnesses {
            if !witness.affected_roots.is_disjoint(&affected_roots) {
                affected_roots.extend(&witness.invalidated_key_dependencies);
            }
        }
        if before == affected_roots.len() {
            break;
        }
    }
    let native_integrity_witnesses: Vec<_> = program_graph
        .native_integrity_witnesses
        .iter()
        .filter(|witness| !witness.affected_roots.is_disjoint(&affected_roots))
        .cloned()
        .collect();
    (native_integrity_witnesses, native_prototype_mutations)
}

fn edge_census_rows(edges: &[DependencyEdgeReport]) -> Vec<EdgeCensusRow> {
    let mut counts = BTreeMap::new();
    for edge in edges {
        let role = match &edge.role {
            Some(graph::EdgeRoleReport::PromotedAtInit { .. }) => EdgeCensusRole::PromotedAtInit,
            None => EdgeCensusRole::Direct,
        };
        *counts.entry((edge.edge_kind, role)).or_insert(0) += 1;
    }
    counts
        .into_iter()
        .map(|((edge_kind, role), count)| EdgeCensusRow {
            edge_kind,
            role,
            count,
        })
        .collect()
}

pub fn run_source_selection_report(
    args: &SourceSelectionArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<SourceSelectionReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let selection = &args.selection;
    let module_files = if matches!(selection, SelectionKind::ModulePath(_)) {
        collect_module_files(&args.common.modules_root)?
    } else {
        Vec::new()
    };
    let query = query_report(selection);
    let ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        &input,
        &ResolveSourceUnitContext {
            common: &args.common,
            source_root: args.source_root.as_deref(),
            source_context,
            module_files: &module_files,
        },
        diagnostics,
    )?;
    let source_units = source_units_for_ids(graph, &source_unit_ids);
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
                    source_context,
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
        source_unit_ids: source_unit_ids.into_iter().collect(),
        unknown_binding_ids,
        source_units: selected,
    })
}

fn load_program_graph(
    graph_path: &Path,
    graph: &DependencyGraphReport,
) -> Result<ProgramGraphReport> {
    let path = graph_provenance::program_graph_path(graph_path, &graph.program_graph);
    report_io::read_json_gzip(&path)
}

fn apply_limit<T>(records: &mut Vec<T>, limit: usize) {
    if limit > 0 {
        records.truncate(limit);
    }
}

fn move_to_front<T>(records: &mut [T], mut matches: impl FnMut(&T) -> bool) {
    if let Some(index) = records.iter().position(&mut matches) {
        records[..=index].rotate_right(1);
    }
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

fn patch_plan_rows(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
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
        anonymous_resolution::SourceClaimsContext {
            graph,
            dependency_graph_path,
            modules_root,
            source_root,
            source_context,
            diagnostics,
        },
        &patch_sets
            .iter()
            .map(|patch_set| SourceClaimSet {
                module_path: &patch_set.file,
                module: &patch_set.claims,
            })
            .collect::<Vec<_>>(),
    )?;
    patch_sets
        .into_iter()
        .zip(resolved_by_set)
        .map(|(patch_set, resolved)| {
            let claimed_bindings: BTreeSet<String> = patch_set
                .claims
                .binding_pins()
                .map(str::to_owned)
                .chain(resolved.bindings)
                .collect();
            let ResolvedSelection {
                source_unit_ids: requested_source_unit_ids,
                unknown_binding_ids,
            } = resolve_claimed_units(
                graph,
                &binding_to_source_units,
                claimed_bindings.iter().map(String::as_str),
                &resolved.anonymous_source_units,
            );

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
                let missing_bindings: Vec<_> = atomic_group
                    .members
                    .iter()
                    .map(|member| member.binding.as_str())
                    .filter(|binding| !claimed_bindings.contains(*binding))
                    .collect();
                let missing_units: Vec<_> = atomic_group
                    .source_unit_ids
                    .iter()
                    .filter(|id| !requested_source_unit_ids.contains(*id))
                    .collect();
                if missing_bindings.is_empty() && missing_units.is_empty() {
                    complete_atomic_group_ids.push(atomic_group_id.clone());
                } else {
                    split_atomic_group_ids.push(atomic_group_id.clone());
                    missing_binding_ids.extend(missing_bindings.into_iter().map(str::to_owned));
                    missing_source_unit_ids.extend(missing_units.into_iter().cloned());
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
            Ok(PatchPlanRow {
                path: patch_set.path,
                file: patch_set.file.display().to_string(),
                status,
                requested_binding_ids: claimed_bindings.into_iter().collect(),
                unknown_binding_ids,
                atomic_group_ids: atomic_group_ids.into_iter().collect(),
                complete_atomic_group_ids,
                split_atomic_group_ids,
                missing_binding_ids: missing_binding_ids.into_iter().collect(),
                missing_source_unit_ids: missing_source_unit_ids.into_iter().collect(),
                missing_anonymous_source_unit_ids: missing_anonymous_source_unit_ids
                    .into_iter()
                    .collect(),
            })
        })
        .collect()
}

#[derive(Debug, Clone)]
struct PatchSet {
    path: String,
    file: PathBuf,
    claims: spec_modules::ModuleClaims,
}

fn load_patch_sets(modules_root: &Path) -> Result<Vec<PatchSet>> {
    let mut sets = Vec::<PatchSet>::new();
    for file in collect_module_files(modules_root)? {
        let claims = read_module_claims(&file)?;
        if claims.is_empty() {
            continue;
        }
        sets.push(PatchSet {
            path: module_path_from_file(&file, modules_root),
            file,
            claims,
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
    source_unit_ids: BTreeSet<String>,
    unknown_binding_ids: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
#[error("source-unit ID {source_unit_id:?} not found in dependency graph")]
pub struct SourceUnitNotFound {
    source_unit_id: String,
}

struct ResolveSourceUnitContext<'a> {
    common: &'a CommonArgs,
    source_root: Option<&'a Path>,
    source_context: Option<&'a VerifiedGraphSourceContext>,
    module_files: &'a [PathBuf],
}

fn resolve_source_unit_ids(
    selection: &SelectionKind,
    input: &DependencyGraphInput,
    context: &ResolveSourceUnitContext<'_>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ResolvedSelection> {
    let graph = input.report();
    let mut unknown_binding_ids: Vec<String> = Vec::new();
    let source_unit_ids: BTreeSet<String> = match selection {
        SelectionKind::SourceUnit(source_unit_id) => {
            if graph.nodes.iter().any(|node| node.id == *source_unit_id) {
                BTreeSet::from([source_unit_id.clone()])
            } else {
                return Err(SourceUnitNotFound {
                    source_unit_id: source_unit_id.clone(),
                }
                .into());
            }
        }
        SelectionKind::ModulePath(module_path) => {
            let resolved =
                resolve_module_path_source_unit_ids(graph, context, module_path, diagnostics)?;
            unknown_binding_ids = resolved.unknown_binding_ids;
            resolved.source_unit_ids
        }
        SelectionKind::Module(module_id) => {
            let source_unit_ids: BTreeSet<String> = graph
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
        SelectionKind::AtomicGroup(atomic_group_id) => graph
            .atomic_graph
            .nodes
            .iter()
            .find(|atomic_group| atomic_group.id == *atomic_group_id)
            .into_iter()
            .flat_map(|group| group.source_unit_ids.iter().cloned())
            .collect(),
    };
    if source_unit_ids.is_empty() {
        bail!("selection did not resolve to any source-unit IDs");
    }
    Ok(ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    })
}

/// Preserve partial misses for the report. A module with only unknown binding IDs
/// gets a diagnostic that names them.
fn resolve_module_path_source_unit_ids(
    graph: &DependencyGraphReport,
    context: &ResolveSourceUnitContext<'_>,
    module_path: &str,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ResolvedSelection> {
    let common = context.common;
    let module_files = context.module_files;
    // The module's claims resolve jointly with every other module's, as in
    // `run`.
    let all_claims = module_files
        .iter()
        .map(|file| read_module_claims(file))
        .collect::<Result<Vec<_>>>()?;
    let index = module_files
        .iter()
        .position(|file| module_path_from_file(file, &common.modules_root) == module_path)
        .with_context(|| format!("module {module_path:?} is not in the modules tree"))?;
    let claims = &all_claims[index];
    if claims.is_empty() {
        bail!("module {module_path:?} has no members or anonymous_statements; nothing to describe");
    }

    let resolved = resolve_source_claims_of(
        anonymous_resolution::SourceClaimsContext {
            graph,
            dependency_graph_path: &common.dependency_graph_path,
            modules_root: &common.modules_root,
            source_root: context.source_root,
            source_context: context.source_context,
            diagnostics,
        },
        &module_files
            .iter()
            .zip(&all_claims)
            .map(|(file, claims)| SourceClaimSet {
                module_path: file,
                module: claims,
            })
            .collect::<Vec<_>>(),
        index,
    )?;
    let pins = claims.binding_pins().collect::<BTreeSet<_>>();
    let selection = resolve_claimed_units(
        graph,
        &binding_to_source_units(graph),
        pins.into_iter()
            .chain(resolved.bindings.iter().map(String::as_str)),
        &resolved.anonymous_source_units,
    );
    if selection.source_unit_ids.is_empty() && !selection.unknown_binding_ids.is_empty() {
        bail!(
            "module {module_path:?} declares bindings that do not appear in the dependency graph: {}",
            selection.unknown_binding_ids.join(", ")
        );
    }
    Ok(selection)
}

fn resolve_claimed_units<'a>(
    graph: &DependencyGraphReport,
    binding_to_source_units: &BTreeMap<String, Vec<String>>,
    bindings: impl IntoIterator<Item = &'a str>,
    anonymous_source_units: &BTreeSet<source_identity::SourceUnitId>,
) -> ResolvedSelection {
    let mut source_unit_ids = BTreeSet::new();
    let mut unknown_binding_ids = Vec::new();
    for binding in bindings {
        if let Some(ids) = binding_to_source_units.get(binding) {
            source_unit_ids.extend(ids.iter().cloned());
        } else {
            unknown_binding_ids.push(binding.to_owned());
        }
    }
    for unit in anonymous_source_units {
        if let Some(node) = graph.nodes.get(unit.0) {
            source_unit_ids.insert(node.id.clone());
        }
    }
    ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    }
}

fn binding_homes(modules_root: &Path, module_files: &[PathBuf]) -> Result<Vec<BindingHomeReport>> {
    let mut homes = BTreeSet::new();
    for path in module_files {
        let module_path = module_path_from_file(path, modules_root);
        for member in read_module_file(path)?.members {
            let spec::MemberSelectorSpec::Binding(binding_selector) = member
                .selector
                .validate()
                .with_context(|| format!("member selector in {}", path.display()))?
            else {
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
    Ok(homes.into_iter().collect())
}
