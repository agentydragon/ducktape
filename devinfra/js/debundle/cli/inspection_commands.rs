//! Graph/source inspection commands and their ID resolution and rendering.
use crate::gate::InspectionRoots;
use crate::{GraphCompatibilityScope, emit_report, verify_graph_header_compatibility};
use analysis::DepKind;
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, ValueEnum};
use peel::plan::ExplainSession;
use peel::propose::DEFAULT_SIZE_CAP_LINES;
use peel::{
    AtomicGroupsArgs, CommonArgs as PeelCommonArgs, DEFAULT_DESCRIBE_LIMIT, ExplainArgs,
    ExplainDirection, GraphSummaryArgs, OutputFormat, PatchPlanArgs, SelectionKind,
    SourceSelectionArgs, run_atomic_groups_report, run_graph_summary_report,
    run_patch_plan_report, run_source_selection_report,
};
use selector_codemod::source_input::resolve_chunk_source_file;
use serde::Serialize;
use source_identity::SourceUnitOrdinal;
use source_inspection::{PreparedSource, StatementRange, StatementRow};
use spec_modules::{collect_module_files, module_path_from_file};
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

/// Args for `debundle describe <id>`.
///
/// `<id>` is dispatched on shape: `unit:NNN`, `atomic:NNN` (an atomic group),
/// `diagnostic:...`, `auto_partition_NNNN`/`extend:...`, a module path
/// (resolves to `<modules>/<id>.yaml`), or otherwise a binding
/// (minified or readable name).
#[derive(Debug, ClapArgs)]
pub(super) struct DescribeArgs {
    /// Identifier to describe.
    #[arg(required_unless_present = "batch_file", conflicts_with = "batch_file")]
    pub id: Option<String>,

    /// JSON array of identifiers to describe together, sharing one verified graph.
    #[arg(long = "batch", value_name = "FILE")]
    pub batch_file: Option<PathBuf>,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Hard line ceiling used when resolving proposal-id references.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Maximum number of rows per report section (default 20; use --all for exhaustive results).
    #[arg(
        long,
        default_value = "20",
        default_value_if("all", "true", None),
        conflicts_with = "all"
    )]
    pub limit: Option<usize>,

    /// Emit every matching row. Conflicts with --limit.
    #[arg(long)]
    pub all: bool,

    /// Which dependency-graph edge directions to show.
    #[arg(long, value_enum, default_value = "both")]
    pub direction: ExplainDirection,

    /// Keep edges with this dependency reason; repeat to include several reasons.
    #[arg(long = "reason", value_name = "REASON")]
    pub reasons: Vec<DepKind>,

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

/// Args for `debundle inspect-source`.
#[derive(Debug, ClapArgs)]
pub(super) struct InspectSourceArgs {
    /// Resolve a binding, source unit, atom, proposal, diagnostic, or module ID.
    #[arg(long, conflicts_with_all = ["source_file", "chunk", "statements", "around_binding"])]
    pub id: Option<String>,

    /// Dependency graph for --id (or DEBUNDLE_GRAPH).
    #[arg(long, env = "DEBUNDLE_GRAPH")]
    pub graph: Option<PathBuf>,

    /// Module spec root for --id (or DEBUNDLE_MODULES).
    #[arg(long, env = "DEBUNDLE_MODULES")]
    pub modules: Option<PathBuf>,

    /// Hard line ceiling used when resolving proposal IDs.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

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

    /// Neighboring items around --around-binding (default 2) or --id (default 0).
    #[arg(long = "context-statements")]
    pub context_statements: Option<usize>,

    /// Show readable SWC output or exact source bytes.
    #[arg(long, value_enum, default_value = "pretty")]
    pub view: SourceView,

    /// Maximum bytes from any displayed source line; 0 disables this limit.
    #[arg(long = "max-line-bytes", default_value_t = 512)]
    pub max_line_bytes: usize,

    /// Maximum complete top-level items to display; 0 disables this limit.
    #[arg(long = "max-statements", default_value_t = 32)]
    pub max_statements: usize,

    /// Maximum serialized output bytes, including JSON; 0 disables this limit.
    #[arg(long = "max-output-bytes", default_value_t = 32768)]
    pub max_output_bytes: usize,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, Copy, Serialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub(super) enum SourceView {
    Pretty,
    Raw,
}

#[derive(Debug, Serialize)]
struct InspectSourceReport {
    #[serde(skip_serializing_if = "Option::is_none")]
    query: Option<peel::plan::QueryReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    selected_source_unit_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    unknown_binding_ids: Vec<String>,
    index_basis: &'static str,
    view: SourceView,
    files: Vec<InspectedFile>,
    omitted_items: usize,
}

#[derive(Debug, Serialize)]
struct InspectedFile {
    source_file: String,
    total_statements: usize,
    statements: Vec<InspectedStatement>,
}

#[derive(Debug, Serialize)]
struct InspectedStatement {
    index: usize,
    byte_start: usize,
    byte_end: usize,
    start_line: usize,
    start_column: usize,
    end_line: usize,
    end_column: usize,
    bindings: Vec<String>,
    source_unit_ids: Vec<String>,
    source: String,
    omitted_source_bytes: usize,
}

/// Dispatch an `<id>` argument into the [`SelectionKind`] it names. Module
/// paths and logical module ids resolve through the same dependency-graph/spec
/// claim path as other structured IDs, so binding members and anonymous
/// statements stay in sync.
fn dispatch_id_selection(id: &str, modules_root: &std::path::Path) -> Result<SelectionKind> {
    // Prefix-based dispatch covers the structured ID kinds emitted by
    // the analysis crate.
    if id.starts_with("unit:") {
        return Ok(SelectionKind::SourceUnit(id.to_string()));
    }
    if id.starts_with("logical:") {
        return Ok(SelectionKind::Module(id.to_string()));
    }
    if id.starts_with("atomic:") {
        return Ok(SelectionKind::AtomicGroup(id.to_string()));
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

pub(super) fn run_describe(args: DescribeArgs, roots: InspectionRoots) -> Result<()> {
    if args.limit == Some(0) {
        bail!("--limit must be greater than zero; use --all for exhaustive results");
    }
    let ids = if let Some(path) = &args.batch_file {
        let bytes = std::fs::read(path)
            .with_context(|| format!("reading describe batch {}", path.display()))?;
        let ids: Vec<String> = serde_json::from_slice(&bytes)
            .with_context(|| format!("parsing describe batch {}", path.display()))?;
        if ids.is_empty() {
            bail!("describe --batch requires a nonempty JSON array of identifiers");
        }
        ids
    } else {
        vec![args.id.clone().expect("Clap requires an ID or --batch")]
    };
    let mut session = ExplainSession::load(
        args.common.clone(),
        args.source_root.clone(),
        args.size_cap_lines,
    )?;
    let graph = session.graph();
    verify_graph_header_compatibility(
        &args.common.dependency_graph_path,
        &graph_provenance::GraphHeader {
            chunk_id: graph.chunk_id.clone(),
            provenance: graph.provenance.clone(),
        },
        &args.common.modules_root,
        args.source_root.as_deref(),
        &roots,
        GraphCompatibilityScope::AuthoredModuleTree { tree_config: None },
    )?;
    // Finish the entire batch before emitting any result, including NDJSON.
    // A failed item must not leave an apparently successful partial document.
    let results = ids
        .iter()
        .enumerate()
        .map(|(index, id)| {
            dispatch_id_selection(id, &args.common.modules_root)
                .and_then(|selection| {
                    session.explain(&ExplainArgs {
                        selection,
                        limit: if args.all { 0 } else { args.limit.unwrap_or(DEFAULT_DESCRIBE_LIMIT) },
                        direction: args.direction,
                        reasons: args.reasons.clone(),
                        include_proposals: args.include_proposals,
                    })
                })
                .with_context(|| format!("describing item {} ({id:?})", index + 1))
        })
        .collect::<Result<Vec<_>>>()?;
    if args.batch_file.is_none() {
        return emit_report(
            args.format,
            &results[0],
            render_explain_text,
            "writing describe output",
        );
    }
    let format = OutputFormat::resolve(args.format);
    if format == OutputFormat::Ndjson {
        for result in results {
            println!("{}", serde_json::to_string(&result)?);
        }
        return Ok(());
    }
    #[derive(Serialize)]
    struct BatchReport {
        results: Vec<peel::ExplainReport>,
    }
    emit_report(
        Some(format),
        &BatchReport { results },
        |batch, out| {
            for result in &batch.results {
                render_explain_text(result, out);
            }
        },
        "writing describe batch output",
    )
}

pub(super) fn run_inspect_source(args: InspectSourceArgs) -> Result<()> {
    let mut report = InspectSourceReport {
        query: None,
        selected_source_unit_count: None,
        unknown_binding_ids: Vec::new(),
        index_basis: "zero-based raw parsed Module.body index; not a source-unit ordinal",
        view: args.view,
        files: Vec::new(),
        omitted_items: 0,
    };
    if let Some(id) = &args.id {
        let graph = args.graph.clone().context("--id requires --graph")?;
        let modules = args.modules.clone().context("--id requires --modules")?;
        let selection = dispatch_id_selection(id, &modules)?;
        let selected = run_source_selection_report(&SourceSelectionArgs {
            common: PeelCommonArgs {
                dependency_graph_path: graph,
                modules_root: modules,
            },
            selection,
            size_cap_lines: args.size_cap_lines,
            source_root: args.source_root.clone(),
        })?;
        report.query = Some(selected.query);
        report.selected_source_unit_count = Some(selected.source_unit_ids.len());
        report.unknown_binding_ids = selected.unknown_binding_ids;
        let mut by_file: BTreeMap<String, Vec<peel::SelectedSourceUnitSource>> = BTreeMap::new();
        for source_unit in selected.source_units {
            by_file
                .entry(source_unit.resolved_path.clone())
                .or_default()
                .push(source_unit);
        }
        for (path, source_units) in by_file {
            let source = PreparedSource::load(&path)?;
            let mut by_index: BTreeMap<usize, BTreeSet<String>> = BTreeMap::new();
            let ordinals: Vec<_> = source_units
                .iter()
                .map(|source_unit| SourceUnitOrdinal(source_unit.statement_ordinal))
                .collect();
            for (source_unit, index) in source_units
                .iter()
                .zip(source.body_indices_for_source_unit_ordinals(&ordinals))
            {
                let index = index.with_context(|| {
                    format!(
                        "source unit {} ordinal {} is outside parsed source {}; regenerate the dependency graph",
                        source_unit.source_unit_id, source_unit.statement_ordinal, path
                    )
                })?;
                let index = index.0;
                let row = source.statement_coordinates(index)?;
                let location = &source_unit.source_location;
                // A split comma-list source unit can start inside its raw item.
                // Its recorded span must still fit within that item's span.
                let starts_before_item = location.start_line < row.start_line
                    || (location.start_line == row.start_line
                        && location
                            .start_column
                            .is_some_and(|column| column < row.start_column));
                let starts_after_item = location.start_line > row.end_line
                    || (location.start_line == row.end_line
                        && location
                            .start_column
                            .is_some_and(|column| column > row.end_column));
                if starts_before_item || starts_after_item || location.end_line > row.end_line {
                    bail!(
                        "source unit {} location {:?} does not fit body[{index}] at {}:{}..{}:{} in {}; regenerate the dependency graph from this source",
                        source_unit.source_unit_id,
                        location,
                        row.start_line,
                        row.start_column,
                        row.end_line,
                        row.end_column,
                        path
                    );
                }
                by_index
                    .entry(index)
                    .or_default()
                    .insert(source_unit.source_unit_id.clone());
            }
            append_file(
                &mut report,
                &source,
                by_index,
                args.context_statements.unwrap_or(0),
                &args,
            )?;
        }
    } else {
        let source_file = resolve_chunk_source_file(
            args.source_file.as_deref(),
            args.source_root.as_deref(),
            args.chunk.as_deref(),
        )?;
        let source = PreparedSource::load(&source_file)?;
        let range =
            match args.around_binding.as_deref() {
                Some(binding) => Some(source.statement_range_around_binding(
                    binding,
                    args.context_statements.unwrap_or(2),
                )?),
                None => args.statements,
            };
        let (start, end) = match range {
            Some(range) => {
                range.validate(source.statement_count())?;
                (range.start, range.end + 1)
            }
            None => (0, source.statement_count()),
        };
        let indices = (start..end).map(|index| (index, BTreeSet::new())).collect();
        append_file(&mut report, &source, indices, 0, &args)?;
    }
    let format = OutputFormat::resolve(args.format);
    bound_report(&mut report, format, args.max_output_bytes)?;
    emit_report(
        Some(format),
        &report,
        render_inspect_source_text,
        "writing inspect-source output",
    )
}

fn append_file(
    report: &mut InspectSourceReport,
    source: &PreparedSource,
    mut source_units_by_index: BTreeMap<usize, BTreeSet<String>>,
    context: usize,
    args: &InspectSourceArgs,
) -> Result<()> {
    let mut indices = BTreeSet::new();
    for index in source_units_by_index.keys() {
        indices.extend(
            index.saturating_sub(context)
                ..=index
                    .saturating_add(context)
                    .min(source.statement_count() - 1),
        );
    }
    let remaining = args
        .max_statements
        .saturating_sub(displayed_statement_count(report));
    let keep = if args.max_statements == 0 {
        indices.len()
    } else {
        remaining.min(indices.len())
    };
    report.omitted_items += indices.len() - keep;
    let statements = indices
        .into_iter()
        .take(keep)
        .map(|index| {
            let row = source
                .statement_rows_with_pretty(
                    Some(StatementRange {
                        start: index,
                        end: index,
                    }),
                    matches!(args.view, SourceView::Pretty),
                )?
                .remove(0);
            Ok(inspected_statement(
                row,
                source_units_by_index
                    .remove(&index)
                    .unwrap_or_default()
                    .into_iter()
                    .collect(),
                args.view,
                args.max_line_bytes,
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    report.files.push(InspectedFile {
        source_file: source.path().display().to_string(),
        total_statements: source.statement_count(),
        statements,
    });
    Ok(())
}

fn render_inspect_source_text(report: &InspectSourceReport, out: &mut String) {
    out.push_str(&format!("Index: {}\n", report.index_basis));
    out.push_str(&format!(
        "View: {}\n",
        match report.view {
            SourceView::Pretty => "pretty",
            SourceView::Raw => "raw",
        }
    ));
    if let Some(query) = &report.query {
        out.push_str(&format!(
            "Selection: {:?} {:?} ({} source_unit(s))\n",
            query.kind,
            query.value,
            report.selected_source_unit_count.unwrap_or(0)
        ));
    }
    for file in &report.files {
        out.push_str(&format!(
            "{} top-level statement(s) in {}\n",
            file.total_statements, file.source_file
        ));
        for statement in &file.statements {
            let bindings = if statement.bindings.is_empty() {
                "none".to_string()
            } else {
                statement.bindings.join(", ")
            };
            out.push_str(&format!(
                "\nbody[{}] bytes={}..{} location={}:{}..{}:{} bindings=[{}] source_units=[{}]\n",
                statement.index,
                statement.byte_start,
                statement.byte_end,
                statement.start_line,
                statement.start_column,
                statement.end_line,
                statement.end_column,
                bindings,
                statement.source_unit_ids.join(", "),
            ));
            for line in statement.source.lines() {
                out.push_str("  ");
                out.push_str(line);
                out.push('\n');
            }
            if statement.omitted_source_bytes > 0 {
                out.push_str(&format!(
                "  [omitted {} source byte(s); use --max-line-bytes or --max-output-bytes to expand]\n",
                statement.omitted_source_bytes
            ));
            }
        }
    }
    if report.omitted_items > 0 {
        out.push_str(&format!(
            "[omitted {} statement(s); use --max-statements to expand]\n",
            report.omitted_items
        ));
    }
}

fn displayed_statement_count(report: &InspectSourceReport) -> usize {
    report.files.iter().map(|file| file.statements.len()).sum()
}

fn inspected_statement(
    row: StatementRow,
    source_unit_ids: Vec<String>,
    view: SourceView,
    max_line_bytes: usize,
) -> InspectedStatement {
    let full_source = match view {
        SourceView::Pretty => row.pretty_source,
        SourceView::Raw => row.original_source,
    };
    let source = clip_source_lines(&full_source, max_line_bytes);
    InspectedStatement {
        index: row.index,
        byte_start: row.byte_start,
        byte_end: row.byte_end,
        start_line: row.start_line,
        start_column: row.start_column,
        end_line: row.end_line,
        end_column: row.end_column,
        bindings: row.bindings,
        source_unit_ids,
        omitted_source_bytes: full_source.len() - source.len(),
        source,
    }
}

fn clip_source_lines(source: &str, max_line_bytes: usize) -> String {
    if max_line_bytes == 0 {
        return source.to_string();
    }
    let mut clipped = String::new();
    for line in source.split_inclusive('\n') {
        let (body, newline) = match line.strip_suffix('\n') {
            Some(body) => (body, "\n"),
            None => (line, ""),
        };
        let mut end = body.len().min(max_line_bytes);
        while !body.is_char_boundary(end) {
            end -= 1;
        }
        clipped.push_str(&body[..end]);
        clipped.push_str(newline);
    }
    clipped
}

fn report_size(report: &InspectSourceReport, format: OutputFormat) -> Result<usize> {
    let size = match format {
        OutputFormat::Text => {
            let mut output = String::new();
            render_inspect_source_text(report, &mut output);
            output.len() + usize::from(!output.ends_with('\n'))
        }
        OutputFormat::Json => serde_json::to_string_pretty(report)?.len() + 1,
        OutputFormat::Ndjson => serde_json::to_string(report)?.len() + 1,
    };
    Ok(size)
}

fn bound_report(
    report: &mut InspectSourceReport,
    format: OutputFormat,
    max_bytes: usize,
) -> Result<()> {
    if max_bytes == 0 {
        return Ok(());
    }
    loop {
        let size = report_size(report, format)?;
        if size <= max_bytes {
            return Ok(());
        }
        let Some(file) = report
            .files
            .iter_mut()
            .rev()
            .find(|file| !file.statements.is_empty())
        else {
            bail!("inspect-source metadata exceeds --max-output-bytes={max_bytes}");
        };
        let statement = file.statements.last_mut().expect("a statement exists");
        if statement.source.is_empty() {
            file.statements.pop();
            report.omitted_items += 1;
            continue;
        }
        let mut end = statement.source.len().saturating_sub(size - max_bytes);
        if end == statement.source.len() {
            end -= 1;
        }
        while !statement.source.is_char_boundary(end) {
            end -= 1;
        }
        statement.omitted_source_bytes += statement.source.len() - end;
        statement.source.truncate(end);
    }
}

fn render_atomic_groups_text(report: &peel::AtomicGroupsReport, out: &mut String) {
    out.push_str(&format!("{} atomic group(s)\n", report.atomic_groups.len()));
    for atomic_group in &report.atomic_groups {
        let bindings: Vec<&str> = atomic_group
            .members
            .iter()
            .map(|member| member.binding.as_str())
            .collect();
        out.push_str(&format!(
            "  {}  [{}]  size={}\n",
            atomic_group.id,
            bindings.join(", "),
            atomic_group.size_lines_estimate
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
        "source_units={} edges={} atomic_groups={} residual_atomic_groups={} proposals={} diagnostics={}\n",
        report.source_unit_count,
        report.dependency_edge_count,
        report.atomic_group_count,
        report.residual_atomic_group_count,
        proposal_count,
        diagnostic_count,
    ));
}

fn render_explain_text(report: &peel::ExplainReport, out: &mut String) {
    out.push_str(&format!(
        "{:?} {:?}\n",
        report.query.kind, report.query.value
    ));
    out.push_str(&format!(
        "  source units: {}\n",
        report.source_unit_ids.join(", ")
    ));
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
            "  unknown bindings (claimed but absent from dependency graph): {}\n",
            report.unknown_binding_ids.join(", ")
        ));
    }
    out.push_str(&format!(
        "  atomic groups: {}\n",
        report.atomic_groups.len()
    ));
    out.push_str(&format!(
        "  incoming_edges: {}, outgoing_edges: {}\n",
        report.incoming_edges.len(),
        report.outgoing_edges.len()
    ));
    if let Some(limits) = &report.limits {
        for (section, limit) in &limits.sections {
            if limit.truncated {
                out.push_str(&format!(
                    "  showing {} of {} rows in {section}; use --all for the rest\n",
                    limit.emitted, limit.total
                ));
            }
        }
    }
}

pub(super) fn run_atoms(args: AtomicGroupsArgs) -> Result<()> {
    let report = run_atomic_groups_report(&args)?;
    emit_report(
        args.format,
        &report,
        render_atomic_groups_text,
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
