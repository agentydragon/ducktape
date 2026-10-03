//! Graph/source inspection commands and their ID resolution and rendering.
use crate::emit_report;
use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use peel::propose::DEFAULT_SIZE_CAP_LINES;
use peel::{
    CommonArgs as PeelCommonArgs, ExplainArgs, GraphSummaryArgs, OutputFormat, PatchPlanArgs,
    SelectionKind, SourceSliceArgs, UnitsArgs, run_explain_report, run_graph_summary_report,
    run_patch_plan_report, run_source_slice_report, run_units_report,
};
use selector_codemod::source_input::resolve_chunk_source_file;
use serde::Serialize;
use source_inspection::{PreparedSource, StatementRange, StatementRow};
use spec_modules::{collect_module_files, module_path_from_file};
use std::path::PathBuf;

/// Args for `debundle describe <id>`.
///
/// `<id>` is dispatched on shape: `owner:NNN`, `atomic:NNN`,
/// `diagnostic:...`, `auto_partition_NNNN`/`extend:...`, a module path
/// (resolves to `<modules>/<id>.yaml`), or otherwise a binding
/// (minified or readable name).
#[derive(Debug, ClapArgs)]
pub(super) struct DescribeArgs {
    /// Identifier to describe.
    pub id: String,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Hard line ceiling used when resolving proposal-id references.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Maximum number of rows to emit per report section. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Also run the module proposer to annotate matching proposals and
    /// diagnostics. This is intentionally opt-in because it is expensive on
    /// large graphs.
    #[arg(long = "include-proposals")]
    pub include_proposals: bool,

    /// Root used to resolve relative `source_location.source_path` values.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle show-source <id>`.
#[derive(Debug, ClapArgs)]
pub(super) struct ShowSourceArgs {
    /// Identifier to print source text for.
    pub id: String,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Hard line ceiling used when resolving proposal-id references.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Extra source lines around the selected owner span.
    #[arg(long = "context-lines", default_value_t = 20)]
    pub context_lines: usize,

    /// Root used to resolve relative `source_location.source_path` values.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle inspect-source`.
#[derive(Debug, ClapArgs)]
pub(super) struct InspectSourceArgs {
    /// Read this JavaScript source file directly.
    #[arg(long = "source-file", conflicts_with = "chunk")]
    pub source_file: Option<PathBuf>,

    /// Root used with `--chunk` to resolve a source file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Source path relative to `--source-root`, e.g. `static/index.js`.
    #[arg(long)]
    pub chunk: Option<PathBuf>,

    /// Select one zero-based statement index or an inclusive `START..END` range.
    #[arg(long, conflicts_with = "around_binding")]
    pub statements: Option<StatementRange>,

    /// Select the unique top-level item declaring this binding.
    #[arg(long)]
    pub around_binding: Option<String>,

    /// Number of neighboring top-level items to show on each side of the binding.
    #[arg(long = "context-statements", default_value_t = 2)]
    pub context_statements: usize,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Serialize)]
struct InspectSourceReport {
    source_file: String,
    total_statements: usize,
    index_basis: &'static str,
    statements: Vec<StatementRow>,
}

/// Dispatch an `<id>` argument into the [`SelectionKind`] it names. Module
/// paths and logical module ids resolve through the same owner-graph/spec
/// claim path as other structured IDs, so binding members and anonymous
/// statements stay in sync.
fn dispatch_id_selection(id: &str, modules_root: &std::path::Path) -> Result<SelectionKind> {
    // Prefix-based dispatch covers the structured ID kinds emitted by
    // the analysis crate.
    if id.starts_with("owner:") {
        return Ok(SelectionKind::Owner(id.to_string()));
    }
    if id.starts_with("logical:") {
        return Ok(SelectionKind::Module(id.to_string()));
    }
    if id.starts_with("atomic:") {
        return Ok(SelectionKind::Unit(id.to_string()));
    }
    if id.starts_with("diagnostic:") {
        return Ok(SelectionKind::Diagnostic(id.to_string()));
    }
    // Module-path detection: try resolving `<modules>/<id>.yaml`.
    // Spec authors sometimes have flat module paths (no `/`); the
    // existence check is the only reliable disambiguator vs. binding
    // names that happen to spell a module-like word.
    if let Some(module_path) = resolve_id_as_module_path(id, modules_root)? {
        return Ok(SelectionKind::ModulePath(module_path));
    }
    if id.starts_with("auto_partition_") || id.starts_with("extend:") {
        return Ok(SelectionKind::Proposal(id.to_string()));
    }
    // Fall through: treat as a binding name (minified or readable).
    Ok(SelectionKind::Binding(id.to_string()))
}

fn resolve_id_as_module_path(id: &str, modules_root: &std::path::Path) -> Result<Option<String>> {
    let module_id = id.strip_suffix(".yaml").unwrap_or(id);
    let candidate = modules_root.join(format!("{module_id}.yaml"));
    if candidate.is_file() {
        return Ok(Some(module_id.to_string()));
    }
    if module_id.contains('/') {
        return Ok(None);
    }

    let filename = format!("{module_id}.yaml");
    let mut matches = collect_module_files(modules_root)
        .with_context(|| format!("walking modules tree {}", modules_root.display()))?
        .into_iter()
        .filter(|path| {
            path.file_name()
                .is_some_and(|name| name == filename.as_str())
        })
        .map(|path| module_path_from_file(&path, modules_root));
    let Some(first) = matches.next() else {
        return Ok(None);
    };
    if matches.next().is_some() {
        return Ok(None);
    }
    Ok(Some(first))
}

pub(super) fn run_describe(args: DescribeArgs) -> Result<()> {
    let selection = dispatch_id_selection(&args.id, &args.common.modules_root)?;
    let inner = ExplainArgs {
        common: args.common,
        selection,
        size_cap_lines: args.size_cap_lines,
        source_root: args.source_root,
        limit: args.limit,
        include_proposals: args.include_proposals,
    };
    let report = run_explain_report(&inner)?;
    emit_report(
        args.format,
        &report,
        render_explain_text,
        "writing describe output",
    )
}

pub(super) fn run_show_source(args: ShowSourceArgs) -> Result<()> {
    let selection = dispatch_id_selection(&args.id, &args.common.modules_root)?;
    let inner = SourceSliceArgs {
        common: args.common,
        selection,
        size_cap_lines: args.size_cap_lines,
        context_lines: args.context_lines,
        source_root: args.source_root,
    };
    let report = run_source_slice_report(&inner)?;
    emit_report(
        args.format,
        &report,
        render_source_slice_text,
        "writing show-source output",
    )
}

pub(super) fn run_inspect_source(args: InspectSourceArgs) -> Result<()> {
    if args.statements.is_some() && args.around_binding.is_some() {
        bail!("use either --statements or --around-binding, not both");
    }
    let source_file = resolve_chunk_source_file(
        args.source_file.as_deref(),
        args.source_root.as_deref(),
        args.chunk.as_deref(),
    )?;
    let source = PreparedSource::load(&source_file)?;
    let statements = match args.around_binding.as_deref() {
        Some(binding) => source.statement_rows_around_binding(binding, args.context_statements)?,
        None => source.statement_rows(args.statements)?,
    };
    let report = InspectSourceReport {
        source_file: source.path().display().to_string(),
        total_statements: source.statement_count(),
        index_basis: "zero-based raw parsed Module.body index; not an owner ID",
        statements,
    };
    emit_report(
        args.format,
        &report,
        render_inspect_source_text,
        "writing inspect-source output",
    )
}

fn render_inspect_source_text(report: &InspectSourceReport, out: &mut String) {
    out.push_str(&format!(
        "{} top-level statement(s) in {}\nIndex: {}\n",
        report.total_statements, report.source_file, report.index_basis
    ));
    for statement in &report.statements {
        let bindings = if statement.bindings.is_empty() {
            "none".to_string()
        } else {
            statement.bindings.join(", ")
        };
        out.push_str(&format!(
            "\nbody[{}] bytes={}..{} location={}:{}..{}:{} bindings=[{}]\n",
            statement.index,
            statement.byte_start,
            statement.byte_end,
            statement.start_line,
            statement.start_column,
            statement.end_line,
            statement.end_column,
            bindings,
        ));
        for line in statement.pretty_source.lines() {
            out.push_str("  ");
            out.push_str(line);
            out.push('\n');
        }
    }
}

fn render_units_text(report: &peel::UnitsReport, out: &mut String) {
    out.push_str(&format!("{} atom(s)\n", report.units.len()));
    for unit in &report.units {
        let bindings: Vec<&str> = unit.members.iter().map(|m| m.binding.as_str()).collect();
        out.push_str(&format!(
            "  {}  [{}]  size={}\n",
            unit.id,
            bindings.join(", "),
            unit.size_lines_estimate
        ));
    }
}

fn render_patch_plan_text(report: &peel::PatchPlanReport, out: &mut String) {
    out.push_str(&format!(
        "{} patch set(s): {} complete, {} split, {} unknown bindings\n",
        report.summary.total_patch_sets,
        report.summary.complete_patch_sets,
        report.summary.split_patch_sets,
        report.summary.unknown_binding_count,
    ));
    for row in &report.rows {
        out.push_str(&format!("  {} [{:?}]\n", row.path, row.status));
    }
}

fn render_graph_summary_text(report: &peel::GraphSummaryReport, out: &mut String) {
    let proposal_count = report
        .proposal_count
        .map(|count| count.to_string())
        .unwrap_or_else(|| "skipped".to_string());
    let diagnostic_count = report
        .diagnostic_count
        .map(|count| count.to_string())
        .unwrap_or_else(|| "skipped".to_string());
    out.push_str(&format!(
        "owners={} edges={} atoms={} residual={} proposals={} diagnostics={}\n",
        report.owner_count,
        report.owner_edge_count,
        report.atomic_unit_count,
        report.residual_atomic_unit_count,
        proposal_count,
        diagnostic_count,
    ));
}

fn render_explain_text(report: &peel::ExplainReport, out: &mut String) {
    out.push_str(&format!(
        "{:?} {:?}\n",
        report.query.kind, report.query.value
    ));
    out.push_str(&format!("  owners: {}\n", report.owner_ids.join(", ")));
    out.push_str(&format!(
        "  bindings: {}\n",
        report
            .bindings
            .iter()
            .map(|b| b.binding.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    ));
    // Home module path per binding (CLI_DOGFOOD #6): the JSON carries
    // `binding_homes[].path`, but the text view previously dropped it,
    // leaving no way to see where a binding lives without `--format json`.
    if !report.binding_homes.is_empty() {
        out.push_str("  homes:\n");
        for home in &report.binding_homes {
            out.push_str(&format!("    {} -> {}\n", home.binding, home.path));
        }
    }
    if !report.unknown_binding_ids.is_empty() {
        out.push_str(&format!(
            "  unknown bindings (claimed but absent from owner graph): {}\n",
            report.unknown_binding_ids.join(", ")
        ));
    }
    out.push_str(&format!("  atomic_units: {}\n", report.atomic_units.len()));
    out.push_str(&format!(
        "  incoming_edges: {}, outgoing_edges: {}\n",
        report.incoming_edges.len(),
        report.outgoing_edges.len()
    ));
}

fn render_source_slice_text(report: &peel::SourceSliceReport, out: &mut String) {
    for slice in &report.slices {
        out.push_str(&format!(
            "--- {} (lines {}-{}) ---\n",
            slice.source_path, slice.context_start_line, slice.context_end_line
        ));
        out.push_str(&slice.text);
        if !slice.text.ends_with('\n') {
            out.push('\n');
        }
    }
}

pub(super) fn run_atoms(args: UnitsArgs) -> Result<()> {
    let report = run_units_report(&args)?;
    emit_report(
        args.format,
        &report,
        render_units_text,
        "writing atoms output",
    )
}

pub(super) fn run_coverage(args: PatchPlanArgs) -> Result<()> {
    let report = run_patch_plan_report(&args)?;
    emit_report(
        args.format,
        &report,
        render_patch_plan_text,
        "writing coverage output",
    )
}

pub(super) fn run_graph_summary(args: GraphSummaryArgs) -> Result<()> {
    let report = run_graph_summary_report(&args)?;
    emit_report(
        args.format,
        &report,
        render_graph_summary_text,
        "writing graph-summary output",
    )
}
