//! `debundle spec validate --keep-going` — keep-going selector validation that
//! emits a machine-readable report of every selector problem instead of stopping
//! at the first failing selector.
//!
//! The transform-backed keep-going classification lives in the materialize pass
//! (`lowering::materialize::plan_builder`), which already writes a per-chunk
//! `selector_diagnostics.json` under [`ReportEmission::OnRejection`]. This
//! verb is a thin frontend: it runs the dry-run keep-going pipeline with
//! reports forced into a capture directory, reads the per-chunk reports back
//! through the shared [`SelectorDiagnosticsReport`] contract, and re-emits a
//! combined report on stdout in the standard `--format text|json|ndjson`
//! convention.
//!
//! The source-only mode (`--modules <modules-dir> --source-file <chunk.js>`)
//! uses the in-process fact matcher directly. It intentionally does not enter
//! the global CP-SAT / OR-Tools selector assignment backend; it is a fast
//! preflight for sharding source selector repairs across agents.

use std::path::Path;

use analysis::ChunkId;
use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use output_layout::SELECTOR_DIAGNOSTICS_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli_with_options};
use selector_diagnostics::{SelectorDiagnosticEntry, SelectorDiagnosticsReport};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, SelectorIrLoweringError,
    lower_member_selector,
};
use serde::Serialize;
use source_match::{SelectorResolver, chunk_resolver::ChunkResolver};
use source_match::{selector_body_key, selector_key, source_match_claim_member_selectors};
use spec::{MemberSelectorSpec, SourceMatchClaim};

/// Args for `debundle spec validate`. The spec source and package-root flags
/// mirror `debundle run` (`--spec` / `--tree-config` + roots) so the same
/// inputs validate and run.
#[derive(Debug, ClapArgs)]
pub struct ValidateArgs {
    // The flattened `--spec` / `--tree-config` / `--package-root` / `--keep-going`
    // / `--fail-fast` flags. Keep-going is the default; pass `--fail-fast` to
    // stop at the first supported failure instead of collecting every problem.
    #[command(flatten)]
    pub transform: TransformArgs,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson` emits one
    /// JSON object per diagnostic plus a final `summary` line.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,

    /// Tree-shaped modules root to source-preflight without a full transform spec.
    #[arg(long = "modules")]
    pub modules_root: Option<std::path::PathBuf>,

    /// Direct JS chunk to source-preflight module selectors against.
    #[arg(long = "source-file")]
    pub source_file: Option<std::path::PathBuf>,

    /// Source root used with `--chunk`.
    #[arg(long = "source-root")]
    pub source_root: Option<std::path::PathBuf>,

    /// Chunk path relative to `--source-root`, e.g. `static/index.js`.
    #[arg(long = "chunk")]
    pub chunk: Option<std::path::PathBuf>,
}

/// Combined keep-going report across every chunk the spec materializes.
#[derive(Debug, Serialize)]
pub struct ValidateReport {
    /// Per-failure-class totals summed across all chunks.
    pub counts: std::collections::BTreeMap<String, usize>,
    /// Total selector problems found.
    pub total: usize,
    /// Per-chunk reports, sorted by chunk id. A chunk with no selector
    /// problems contributes no entry.
    pub chunks: Vec<SelectorDiagnosticsReport>,
}

pub fn run_validate_cmd(args: ValidateArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    if args.source_only_requested() {
        return js_ast::with_swc_globals(|| run_source_only_validate_cmd(args, format));
    }

    let keep_going = !args.transform.fail_fast;
    let cli = args.transform.resolve()?;

    // Force reports into a private capture dir: dry-run + a report dir makes
    // the materialize pass emit `selector_diagnostics.json` per chunk on
    // rejection, independent of how the spec configures `report_out_dir`.
    let capture = tempfile::tempdir().context("creating selector-diagnostics capture dir")?;
    // The keep-going pass writes the per-chunk diagnostics *and then* fails the
    // pipeline at the end with the collected findings — that rejection is the
    // contract for `debundle run`. `validate` treats the findings as data, not a
    // tool failure: when the run produced reports, we emit them and exit zero;
    // only a run that errored *without* producing any report is a real failure
    // (bad spec path, parse error, …).
    let pass = run_transform_cli_with_options(
        &cli,
        TransformRunOptions {
            dry_run: true,
            keep_going,
            report_dir_override: Some(capture.path().to_path_buf()),
        },
    );

    let mut chunks = collect_chunk_reports(capture.path())?;
    if let Err(error) = pass
        && chunks.is_empty()
    {
        return Err(error).context("running keep-going validation pass");
    }
    chunks.sort_by(|a, b| a.chunk_id.cmp(&b.chunk_id));

    let mut counts = std::collections::BTreeMap::new();
    for chunk in &chunks {
        for (category, count) in &chunk.counts {
            *counts.entry(category.clone()).or_insert(0) += count;
        }
    }
    let total = counts.values().sum();
    let report = ValidateReport {
        counts,
        total,
        chunks,
    };

    if format == OutputFormat::Ndjson {
        emit_validate_ndjson(&report)?;
        return Ok(());
    }
    print_report(&report, format, render_validate_text).context("writing validate output")
}

impl ValidateArgs {
    fn source_only_requested(&self) -> bool {
        self.modules_root.is_some()
            || self.source_file.is_some()
            || self.source_root.is_some()
            || self.chunk.is_some()
    }
}

fn run_source_only_validate_cmd(args: ValidateArgs, format: OutputFormat) -> Result<()> {
    if args.transform.spec.is_some()
        || args.transform.tree_config.is_some()
        || args.transform.tree_modules.is_some()
        || args.transform.tree_vendor_marks.is_some()
        || args.transform.tree_source_root.is_some()
        || args.transform.out_root.is_some()
    {
        bail!(
            "source-only validation uses --modules plus --source-file or --source-root + --chunk; \
             do not pass --spec or tree transform flags"
        );
    }
    let modules_root = args
        .modules_root
        .as_deref()
        .context("source-only validation requires --modules <modules-dir>")?;
    let source_file = resolve_source_only_chunk_file(
        args.source_file.as_deref(),
        args.source_root.as_deref(),
        args.chunk.as_deref(),
    )?;
    let chunk_id = source_only_chunk_id(&args, &source_file);
    let report = validate_modules_against_source(modules_root, &source_file, chunk_id)?;
    if format == OutputFormat::Ndjson {
        emit_validate_ndjson(&report)?;
        return Ok(());
    }
    print_report(&report, format, render_validate_text)
        .context("writing source-only validate output")
}

fn resolve_source_only_chunk_file(
    source_file: Option<&Path>,
    source_root: Option<&Path>,
    chunk: Option<&Path>,
) -> Result<std::path::PathBuf> {
    match (source_file, source_root, chunk) {
        (Some(source_file), _, None) => Ok(source_file.to_path_buf()),
        (None, Some(source_root), Some(chunk)) => Ok(source_root.join(chunk)),
        (Some(_), _, Some(_)) => {
            bail!("use either --source-file or --source-root with --chunk, not both")
        }
        _ => bail!("a source chunk is required: pass --source-file or --source-root + --chunk"),
    }
}

fn source_only_chunk_id(args: &ValidateArgs, source_file: &Path) -> String {
    args.chunk
        .as_deref()
        .unwrap_or(source_file)
        .to_string_lossy()
        .replace('\\', "/")
}

fn validate_modules_against_source(
    modules_root: &Path,
    source_file: &Path,
    chunk_id: String,
) -> Result<ValidateReport> {
    let source = std::fs::read_to_string(source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;
    let resolver = ChunkResolver::new(&parsed.module);

    let mut diagnostics = Vec::new();
    for path in spec_modules::collect_module_files(modules_root)? {
        let module_path = spec_modules::module_path_from_file(&path, modules_root);
        let module = spec_modules::read_module_file(&path)?;
        if let Err(error) = lowering::validate_logical_module_claims(
            &module_path,
            &module.members,
            &module.source_matches,
            &module.annotations,
        ) {
            diagnostics.push(selector_error_diagnostic(
                &chunk_id,
                &module_path,
                None,
                "annotations",
                None,
                format!("{}#annotations", path.display()),
                error.to_string(),
            ));
        }
        for (member_index, member) in module.members.into_iter().enumerate() {
            let export_name = member.name.clone();
            match member.selector.selected() {
                Ok(MemberSelectorSpec::SourceMatch(selector)) => {
                    diagnostics.extend(validate_member_source_match(
                        &resolver,
                        &chunk_id,
                        &module_path,
                        export_name.as_deref(),
                        "members.source_match",
                        format!("{}#members[{member_index}]", path.display()),
                        &selector,
                    )?);
                    diagnostics.extend(validate_native_member_source_match(
                        &chunk_id,
                        &module_path,
                        export_name.as_deref(),
                        "members.source_match",
                        format!("{}#members[{member_index}]", path.display()),
                        &selector,
                    ));
                }
                Ok(_) => {}
                Err(error) => diagnostics.push(selector_error_diagnostic(
                    &chunk_id,
                    &module_path,
                    export_name.as_deref(),
                    "members.selector",
                    None,
                    format!("{}#members[{member_index}]", path.display()),
                    error.to_string(),
                )),
            }
        }
        for (claim_index, claim) in module.source_matches.into_iter().enumerate() {
            let origin = format!("{}#source_matches[{claim_index}]", path.display());
            diagnostics.extend(validate_source_match_claim(
                &resolver,
                &chunk_id,
                &module_path,
                origin.clone(),
                &claim,
            )?);
            diagnostics.extend(validate_native_source_match_claim(
                &chunk_id,
                &module_path,
                origin,
                &claim,
            ));
        }
        for (statement_index, statement) in module.anonymous_statements.into_iter().enumerate() {
            let origin = format!("{}#anonymous_statements[{statement_index}]", path.display());
            match statement.selector() {
                Ok(selector) => {
                    diagnostics.extend(validate_anonymous_source_match(
                        &resolver,
                        &chunk_id,
                        &module_path,
                        origin.clone(),
                        &selector,
                    )?);
                    diagnostics.extend(validate_native_anonymous_source_match(
                        &chunk_id,
                        &module_path,
                        statement_index,
                        origin,
                        &selector,
                    ));
                }
                Err(error) => diagnostics.push(selector_error_diagnostic(
                    &chunk_id,
                    &module_path,
                    None,
                    "anonymous_statements.source_match",
                    None,
                    origin,
                    error.to_string(),
                )),
            }
        }
    }

    diagnostics.sort_by(|a, b| {
        (
            a.module_path.as_deref().unwrap_or(""),
            a.export_name.as_deref().unwrap_or(""),
            &a.selector_kind,
            &a.category,
        )
            .cmp(&(
                b.module_path.as_deref().unwrap_or(""),
                b.export_name.as_deref().unwrap_or(""),
                &b.selector_kind,
                &b.category,
            ))
    });
    let mut counts = std::collections::BTreeMap::new();
    for diagnostic in &diagnostics {
        *counts.entry(diagnostic.category.clone()).or_insert(0) += 1;
    }
    let total = diagnostics.len();
    let chunks = if diagnostics.is_empty() {
        Vec::new()
    } else {
        vec![SelectorDiagnosticsReport {
            chunk_id,
            counts: counts.clone(),
            diagnostics,
            coverage_notes: vec![
                "source-only validation covers member, binding_group, and anonymous_statement \
                 source_match selectors; \
                 run transform-backed validate for duplicate claims, relational selectors, \
                 and materialization constraints"
                    .to_string(),
            ],
        }]
    };
    Ok(ValidateReport {
        counts,
        total,
        chunks,
    })
}

fn validate_native_member_source_match(
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    claim_origin: String,
    selector: &spec::AnonymousStatementSelector,
) -> Vec<SelectorDiagnosticEntry> {
    let Some(export_name) = export_name else {
        return vec![native_lowering_diagnostic(
            chunk_id,
            module_path,
            None,
            selector_kind,
            Some(selector.clone()),
            claim_origin,
            "native source_match audit requires a member export name".to_string(),
            "native_selector_ir_error",
        )];
    };

    let context = MemberSelectorLoweringContext::new(ChunkId(0), module_path);
    let selector_spec = MemberSelectorSpec::SourceMatch(selector.clone());
    match lower_member_selector(&context, export_name, &selector_spec) {
        Ok(_) => Vec::new(),
        Err(error) => vec![native_lowering_error_diagnostic(
            chunk_id,
            module_path,
            Some(export_name),
            selector_kind,
            selector.clone(),
            claim_origin,
            error,
        )],
    }
}

fn validate_native_source_match_claim(
    chunk_id: &str,
    module_path: &str,
    claim_origin: String,
    claim: &SourceMatchClaim,
) -> Vec<SelectorDiagnosticEntry> {
    let selectors = match source_match_claim_member_selectors(module_path, claim) {
        Ok(selectors) => selectors,
        Err(_) => return Vec::new(),
    };
    let mut exports_by_target = std::collections::BTreeMap::new();
    for selector in &selectors {
        let Some(target_binding) = selector.selector.target_binding.as_deref() else {
            return Vec::new();
        };
        exports_by_target.insert(target_binding.to_string(), selector.export_name.clone());
    }

    let context = MemberSelectorLoweringContext::new(ChunkId(0), module_path);
    let mut builder = MemberSelectorProgramBuilder::new(context);
    for selector in &selectors {
        let selector_spec = MemberSelectorSpec::SourceMatch(selector.selector.clone());
        if let Err(error) = builder.declare_member_target_in_module(
            module_path,
            &selector.export_name,
            &selector_spec,
        ) {
            return selectors
                .into_iter()
                .map(|selector| {
                    native_lowering_error_diagnostic(
                        chunk_id,
                        module_path,
                        Some(&selector.export_name),
                        "source_matches",
                        selector.selector,
                        claim_origin.clone(),
                        error.clone(),
                    )
                })
                .collect();
        }
    }

    let group_selector = claim.source_match().selector();
    let result = builder
        .try_lower_native_source_match_group(module_path, &group_selector, &exports_by_target)
        .and_then(|lowered| {
            if lowered {
                builder.into_program().map(|_| ())
            } else {
                Err(SelectorIrLoweringError::Unsupported {
                    selector_kind: "source_matches",
                    reason: "selector shape is not yet supported by native selector IR",
                })
            }
        });

    match result {
        Ok(()) => Vec::new(),
        Err(error) => selectors
            .into_iter()
            .map(|selector| {
                native_lowering_error_diagnostic(
                    chunk_id,
                    module_path,
                    Some(&selector.export_name),
                    "source_matches",
                    selector.selector,
                    claim_origin.clone(),
                    error.clone(),
                )
            })
            .collect(),
    }
}

fn validate_native_anonymous_source_match(
    chunk_id: &str,
    module_path: &str,
    statement_index: usize,
    claim_origin: String,
    selector: &spec::AnonymousStatementSelector,
) -> Vec<SelectorDiagnosticEntry> {
    let context = MemberSelectorLoweringContext::new(ChunkId(0), module_path);
    let mut builder = MemberSelectorProgramBuilder::new(context);
    let result = builder
        .declare_native_anonymous_statement_target_in_module(module_path, statement_index, selector)
        .and_then(|_| builder.into_program().map(|_| ()));
    match result {
        Ok(()) => Vec::new(),
        Err(error) => vec![native_lowering_error_diagnostic(
            chunk_id,
            module_path,
            None,
            "anonymous_statements.source_match",
            selector.clone(),
            claim_origin,
            error,
        )],
    }
}

fn native_lowering_error_diagnostic(
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    selector: spec::AnonymousStatementSelector,
    claim_origin: String,
    error: SelectorIrLoweringError,
) -> SelectorDiagnosticEntry {
    native_lowering_diagnostic(
        chunk_id,
        module_path,
        export_name,
        selector_kind,
        Some(selector),
        claim_origin,
        error.to_string(),
        native_lowering_category(&error),
    )
}

fn native_lowering_category(error: &SelectorIrLoweringError) -> &'static str {
    match error {
        SelectorIrLoweringError::UnsupportedSourceMatch { reason, .. }
            if reason.contains("selector shape is not yet supported by native selector IR") =>
        {
            "native_source_match_lowering_unsupported"
        }
        SelectorIrLoweringError::UnsupportedSourceMatch { .. } => {
            "native_source_match_capability_error"
        }
        SelectorIrLoweringError::Unsupported { .. } => "native_source_match_lowering_unsupported",
        _ => "native_selector_ir_error",
    }
}

#[allow(clippy::too_many_arguments)]
fn native_lowering_diagnostic(
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    selector: Option<spec::AnonymousStatementSelector>,
    claim_origin: String,
    message: String,
    category: &'static str,
) -> SelectorDiagnosticEntry {
    let mut diagnostic = selector_error_diagnostic(
        chunk_id,
        module_path,
        export_name,
        selector_kind,
        selector,
        claim_origin,
        message,
    )
    .with_category(category);
    diagnostic.recommended_next_action = format!(
        "Repair or shard this native source_match lowering gap in {module_path}; re-run \
         `debundle spec validate --modules <modules-dir> --source-file {chunk_id} --format json`."
    );
    diagnostic
}

fn validate_member_source_match(
    resolver: &ChunkResolver<'_>,
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    claim_origin: String,
    selector: &spec::AnonymousStatementSelector,
) -> Result<Vec<SelectorDiagnosticEntry>> {
    let matches = resolver.member_candidates(module_path, selector);
    let matches = match matches {
        Ok(matches) => matches,
        Err(error) => {
            return Ok(vec![selector_error_diagnostic(
                chunk_id,
                module_path,
                export_name,
                selector_kind,
                Some(selector.clone()),
                claim_origin,
                format!("{error:#}"),
            )]);
        }
    };
    match matches.len() {
        1 => Ok(Vec::new()),
        0 => Ok(vec![selector_resolution_diagnostic(
            chunk_id,
            module_path,
            export_name,
            selector_kind,
            selector,
            claim_origin,
            "unresolved_selector",
            Vec::new(),
        )]),
        _ => Ok(vec![selector_resolution_diagnostic(
            chunk_id,
            module_path,
            export_name,
            selector_kind,
            selector,
            claim_origin,
            "ambiguous_selector",
            matches.iter().map(|matched| matched.body_idx).collect(),
        )]),
    }
}

fn validate_source_match_claim(
    resolver: &ChunkResolver<'_>,
    chunk_id: &str,
    module_path: &str,
    claim_origin: String,
    claim: &SourceMatchClaim,
) -> Result<Vec<SelectorDiagnosticEntry>> {
    let selectors = match source_match_claim_member_selectors(module_path, claim) {
        Ok(selectors) => selectors,
        Err(error) => {
            return Ok(vec![selector_error_diagnostic(
                chunk_id,
                module_path,
                None,
                "source_matches",
                Some(claim.source_match().selector()),
                claim_origin,
                error.to_string(),
            )]);
        }
    };

    let mut exports_by_target = std::collections::BTreeMap::new();
    for selector in &selectors {
        let Some(target_binding) = selector.selector.target_binding.as_deref() else {
            bail!("source_matches expansion for {module_path} did not set target_binding");
        };
        exports_by_target.insert(target_binding.to_string(), selector.export_name.clone());
    }

    let group_selector = claim.source_match().selector();
    let matches =
        match resolver.member_group_candidates(module_path, &group_selector, &exports_by_target) {
            Ok(matches) => matches,
            Err(error) => {
                return Ok(selectors
                    .into_iter()
                    .map(|selector| {
                        selector_error_diagnostic(
                            chunk_id,
                            module_path,
                            Some(&selector.export_name),
                            "source_matches",
                            Some(selector.selector),
                            claim_origin.clone(),
                            format!("{error:#}"),
                        )
                    })
                    .collect());
            }
        };

    match matches.len() {
        1 => Ok(Vec::new()),
        0 => Ok(selectors
            .into_iter()
            .map(|selector| {
                selector_resolution_diagnostic(
                    chunk_id,
                    module_path,
                    Some(&selector.export_name),
                    "source_matches",
                    &selector.selector,
                    claim_origin.clone(),
                    "unresolved_selector",
                    Vec::new(),
                )
            })
            .collect()),
        _ => Ok(selectors
            .into_iter()
            .map(|selector| {
                let target_binding = selector
                    .selector
                    .target_binding
                    .as_deref()
                    .expect("source_match claim expansion sets target_binding");
                let mut body_indices = matches
                    .iter()
                    .filter_map(|matched| {
                        matched
                            .bindings
                            .get(target_binding)
                            .map(|binding| binding.body_idx)
                    })
                    .collect::<Vec<_>>();
                body_indices.sort_unstable();
                body_indices.dedup();
                selector_resolution_diagnostic(
                    chunk_id,
                    module_path,
                    Some(&selector.export_name),
                    "source_matches",
                    &selector.selector,
                    claim_origin.clone(),
                    "ambiguous_selector",
                    body_indices,
                )
            })
            .collect()),
    }
}

fn validate_anonymous_source_match(
    resolver: &ChunkResolver<'_>,
    chunk_id: &str,
    module_path: &str,
    claim_origin: String,
    selector: &spec::AnonymousStatementSelector,
) -> Result<Vec<SelectorDiagnosticEntry>> {
    let matches = match resolver.anonymous_group_candidates(module_path, selector) {
        Ok(matches) => matches,
        Err(error) => {
            return Ok(vec![selector_error_diagnostic(
                chunk_id,
                module_path,
                None,
                "anonymous_statements.source_match",
                Some(selector.clone()),
                claim_origin,
                format!("{error:#}"),
            )]);
        }
    };
    match matches.len() {
        1 => Ok(Vec::new()),
        0 => Ok(vec![selector_resolution_diagnostic(
            chunk_id,
            module_path,
            None,
            "anonymous_statements.source_match",
            selector,
            claim_origin,
            "unresolved_selector",
            Vec::new(),
        )]),
        _ => Ok(vec![selector_resolution_diagnostic(
            chunk_id,
            module_path,
            None,
            "anonymous_statements.source_match",
            selector,
            claim_origin,
            "ambiguous_selector",
            matches.into_iter().flatten().collect(),
        )]),
    }
}

#[allow(clippy::too_many_arguments)]
fn selector_resolution_diagnostic(
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    selector: &spec::AnonymousStatementSelector,
    claim_origin: String,
    category: &'static str,
    body_indices: Vec<usize>,
) -> SelectorDiagnosticEntry {
    let message = match category {
        "unresolved_selector" => "source_match did not match any top-level declaration".to_string(),
        "ambiguous_selector" => format!(
            "source_match matched {} top-level declarations",
            body_indices.len()
        ),
        _ => category.to_string(),
    };
    selector_error_diagnostic(
        chunk_id,
        module_path,
        export_name,
        selector_kind,
        Some(selector.clone()),
        claim_origin,
        message,
    )
    .with_category(category)
    .with_body_indices(body_indices)
}

trait DiagnosticPatch {
    fn with_category(self, category: &str) -> Self;
    fn with_body_indices(self, body_indices: Vec<usize>) -> Self;
}

impl DiagnosticPatch for SelectorDiagnosticEntry {
    fn with_category(mut self, category: &str) -> Self {
        self.category = category.to_string();
        self
    }

    fn with_body_indices(mut self, body_indices: Vec<usize>) -> Self {
        self.body_indices = body_indices;
        self
    }
}

fn selector_error_diagnostic(
    chunk_id: &str,
    module_path: &str,
    export_name: Option<&str>,
    selector_kind: &'static str,
    selector: Option<spec::AnonymousStatementSelector>,
    claim_origin: String,
    message: String,
) -> SelectorDiagnosticEntry {
    let target_binding = selector
        .as_ref()
        .and_then(|selector| selector.target_binding.clone());
    let source_match_preview = selector
        .as_ref()
        .map(|selector| source_match::source_match_preview(&selector.match_source));
    let source_match_hash = selector.as_ref().map(selector_key);
    let source_match_body_hash = selector.as_ref().map(selector_body_key);
    SelectorDiagnosticEntry {
        category: "selector_resolution_error".to_string(),
        module_id: module_path.to_string(),
        module_path: Some(module_path.to_string()),
        export_name: export_name.map(ToOwned::to_owned),
        selector_kind: selector_kind.to_string(),
        target_binding,
        claim_origin: Some(claim_origin),
        body_indices: Vec::new(),
        first_mismatch: None,
        source_match_preview,
        source_match_hash,
        source_match_body_hash,
        duplicate_claim: None,
        root_isolation: None,
        message,
        recommended_next_action: format!(
            "Repair this selector in {module_path}; re-run `debundle spec validate --modules \
             <modules-dir> --source-file {chunk_id} --format json`."
        ),
    }
}

/// Recursively gather every `selector_diagnostics.json` under the capture
/// directory. The materialize pass nests each report at
/// `<capture>/<chunk_id parts>/selector_diagnostics.json`.
fn collect_chunk_reports(capture: &Path) -> Result<Vec<SelectorDiagnosticsReport>> {
    let mut reports = Vec::new();
    collect_chunk_reports_into(capture, &mut reports)?;
    Ok(reports)
}

fn collect_chunk_reports_into(
    dir: &Path,
    reports: &mut Vec<SelectorDiagnosticsReport>,
) -> Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    let mut entries = std::fs::read_dir(dir)
        .with_context(|| format!("reading {}", dir.display()))?
        .collect::<std::io::Result<Vec<_>>>()
        .with_context(|| format!("collecting entries from {}", dir.display()))?;
    entries.sort_by_key(std::fs::DirEntry::path);
    for entry in entries {
        let path = entry.path();
        if path.is_dir() {
            collect_chunk_reports_into(&path, reports)?;
        } else if path.file_name().and_then(|name| name.to_str())
            == Some(SELECTOR_DIAGNOSTICS_REPORT)
        {
            let text = std::fs::read_to_string(&path)
                .with_context(|| format!("reading {}", path.display()))?;
            reports.push(
                serde_json::from_str(&text)
                    .with_context(|| format!("parsing selector diagnostics {}", path.display()))?,
            );
        }
    }
    Ok(())
}

fn render_validate_text(report: &ValidateReport, buf: &mut String) {
    use std::fmt::Write;

    if report.total == 0 {
        buf.push_str("No selector problems found.");
        return;
    }
    let summary = report
        .counts
        .iter()
        .map(|(category, count)| format!("{category}={count}"))
        .collect::<Vec<_>>()
        .join(", ");
    let _ = writeln!(
        buf,
        "{} selector problem(s) across {} chunk(s): {summary}",
        report.total,
        report.chunks.len(),
    );
    for chunk in &report.chunks {
        let _ = writeln!(buf, "\nchunk {}:", chunk.chunk_id);
        for diagnostic in &chunk.diagnostics {
            let export = diagnostic.export_name.as_deref().unwrap_or("-");
            let module = diagnostic
                .module_path
                .as_deref()
                .unwrap_or(&diagnostic.module_id);
            let _ = writeln!(
                buf,
                "  [{}] {module} as `{export}` ({}): {}",
                diagnostic.category, diagnostic.selector_kind, diagnostic.message,
            );
            let _ = writeln!(buf, "    -> {}", diagnostic.recommended_next_action);
        }
    }
}

/// One JSON object per diagnostic tagged with its chunk, then a final
/// `summary` line — the streaming shape `jq -c` consumers dispatch on.
fn emit_validate_ndjson(report: &ValidateReport) -> Result<()> {
    #[derive(Serialize)]
    struct DiagnosticLine<'a> {
        section: &'a str,
        chunk_id: &'a str,
        #[serde(flatten)]
        diagnostic: &'a selector_diagnostics::SelectorDiagnosticEntry,
    }
    #[derive(Serialize)]
    struct SummaryLine<'a> {
        section: &'a str,
        total: usize,
        counts: &'a std::collections::BTreeMap<String, usize>,
    }
    for chunk in &report.chunks {
        for diagnostic in &chunk.diagnostics {
            println!(
                "{}",
                serde_json::to_string(&DiagnosticLine {
                    section: "diagnostic",
                    chunk_id: &chunk.chunk_id,
                    diagnostic,
                })?
            );
        }
    }
    println!(
        "{}",
        serde_json::to_string(&SummaryLine {
            section: "summary",
            total: report.total,
            counts: &report.counts,
        })?
    );
    Ok(())
}
