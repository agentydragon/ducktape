//! `debundle spec validate` — keep-going selector validation that reports every
//! selector that did not resolve, and what each matched template's free
//! identifiers mean, as a [`SelectorOutcomeReport`].
//!
//! The spec mode is a thin frontend over the materialize pass: it runs the
//! dry-run keep-going pipeline with reports forced into a capture directory,
//! reads the per-chunk `selector_diagnostics.json` reports back and re-emits
//! them combined on stdout in the standard `--format text|json|ndjson`
//! convention.
//!
//! The source-only mode (`--modules <modules-dir> --source-file <chunk.js>`)
//! resolves the module files against one chunk with the same resolve `run`
//! uses, without the rest of the pipeline: a fast preflight for sharding
//! selector repairs across agents. What only the pipeline knows it does not
//! report: duplicate claims across modules. A name pin on a binding the chunk
//! does not declare is `no_match` here; `run` fails it as an unmatched claim.

use std::collections::BTreeSet;
use std::path::Path;

use crate::print_section;
use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use output_layout::SELECTOR_DIAGNOSTICS_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{TransformArgs, TransformRunOptions, TransformSpecSource, run_transform_cli};
use selector_codemod::source_input::resolve_chunk_source_file;
use selector_outcome::{
    Entity, Outcome, Placement, SelectorKind, SelectorOutcome, SelectorOutcomeReport,
    SelectorValidationCounts, Severity,
};
use serde::Serialize;
use source_match::source_match_claim_member_selectors;
use spec::{AnonymousStatementSelector, MemberSelectorSpec};
use spec_modules::read_flat_transform_spec;
use spec_resolution::{
    anonymous_statement, member_from_source_match, member_from_spec, parse_anonymous_selector,
    resolve_chunk, spec_module, validate_logical_module_claims,
};
use spec_tree::compile_spec_tree;

/// Args for `debundle spec validate`. The spec source and package-root flags
/// mirror `debundle run` (`--spec` / `--tree-config` + roots) so the same
/// inputs validate and run.
#[derive(Debug, ClapArgs)]
pub struct ValidateArgs {
    // The flattened `--spec` / `--tree-config` / `--package-root` / `--fail-fast`
    // flags. Keep-going is the default; pass `--fail-fast` to stop at the first
    // supported failure instead of collecting every problem.
    #[command(flatten)]
    pub transform: TransformArgs,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson` emits one
    /// JSON object per outcome plus a final `summary` line.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,

    /// Emit the diagnostic report without failing for selector errors.
    /// By default, error outcomes cause a nonzero exit after the full report.
    #[arg(long)]
    pub report_only: bool,

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

pub fn run_validate_cmd(args: ValidateArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    let report_only = args.report_only;
    let report = if args.source_only_requested() {
        js_ast::with_swc_globals(|| run_source_only_validate(&args))?
    } else {
        run_spec_validate(args)?
    };
    if format == OutputFormat::Ndjson {
        emit_validate_ndjson(&report)?;
    } else {
        print_report(&report, format, |report, buf| report.render_text(buf, None))
            .context("writing validate output")?;
    }
    if !report_only
        && report
            .validation_summary()
            .is_some_and(|summary| !summary.success)
    {
        bail!("validation found selector errors");
    }
    Ok(())
}

fn run_spec_validate(args: ValidateArgs) -> Result<SelectorOutcomeReport> {
    let keep_going = !args.transform.fail_fast;
    let cli = args.transform.resolve()?;
    let expected_chunks = expected_validation_chunks(&cli.spec_source)?;

    // Force reports into a private capture dir: dry-run + a report dir makes
    // the materialize pass emit `selector_diagnostics.json` per chunk on
    // rejection, independent of how the spec configures `report_out_dir`.
    let capture = tempfile::tempdir().context("creating selector-diagnostics capture dir")?;
    // The keep-going pass writes the per-chunk reports *and then* fails the
    // pipeline at the end with the collected findings — that rejection is the
    // contract for `debundle run`. When reports exist, `validate` emits them
    // before deciding its exit status in `run_validate_cmd`; only a pass that
    // errored without producing a report fails here (bad spec path, parse
    // error, …).
    let pass = run_transform_cli(
        &cli,
        TransformRunOptions {
            dry_run: true,
            keep_going,
            report_dir_override: Some(capture.path().to_path_buf()),
            list_template_identifiers: true,
        },
    );

    let mut chunks = Vec::new();
    collect_chunk_reports(capture.path(), capture.path(), &mut chunks)?;
    let mut pass_error = pass.err();
    let present_chunks: BTreeSet<_> = chunks.iter().map(|(chunk, _)| chunk.clone()).collect();
    let missing_chunks: Vec<_> = expected_chunks
        .difference(&present_chunks)
        .cloned()
        .collect();
    if pass_error.is_some() {
        if chunks.is_empty() {
            return Err(pass_error.take().expect("pass error is present"))
                .context("running selector validation pass");
        }
        if !missing_chunks.is_empty() {
            return Err(pass_error.take().expect("pass error is present")).context(format!(
                "selector validation stopped before reports were written for chunks: {}",
                missing_chunks.join(", ")
            ));
        }
    }
    if !missing_chunks.is_empty() {
        bail!(
            "selector validation did not produce reports for chunks: {}",
            missing_chunks.join(", ")
        );
    }

    let mut report = SelectorOutcomeReport {
        validation: Some(SelectorValidationCounts::default()),
        ..SelectorOutcomeReport::default()
    };
    for (_, chunk) in chunks {
        let Some(validation) = chunk.validation else {
            bail!("selector diagnostics report is missing validation counts");
        };
        report
            .validation
            .as_mut()
            .expect("validation counts initialized")
            .extend(validation);
        report.outcomes.extend(chunk.outcomes);
        report.templates.extend(chunk.templates);
    }
    report.outcomes.sort();
    report.templates.sort();
    if pass_error.is_some()
        && (!keep_going
            || report
                .validation_summary()
                .is_none_or(|summary| summary.success))
    {
        return Err(pass_error.take().expect("pass error is present"))
            .context("running selector validation pass");
    }
    Ok(report)
}

fn expected_validation_chunks(source: &TransformSpecSource) -> Result<BTreeSet<String>> {
    let spec = match source {
        TransformSpecSource::Flat { path } => read_flat_transform_spec(path)?,
        TransformSpecSource::Tree(options) => compile_spec_tree(options)?,
    };
    Ok(spec.materialize_chunk_ids())
}

impl ValidateArgs {
    fn source_only_requested(&self) -> bool {
        self.modules_root.is_some()
            || self.source_file.is_some()
            || self.source_root.is_some()
            || self.chunk.is_some()
    }
}

fn run_source_only_validate(args: &ValidateArgs) -> Result<SelectorOutcomeReport> {
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
    let source_file = resolve_chunk_source_file(
        args.source_file.as_deref(),
        args.source_root.as_deref(),
        args.chunk.as_deref(),
    )?;
    let chunk = args
        .chunk
        .as_deref()
        .unwrap_or(&source_file)
        .to_string_lossy()
        .replace('\\', "/");
    validate_modules_against_source(modules_root, &source_file, &chunk)
}

fn invalid(error: &impl std::fmt::Display) -> Outcome {
    Outcome::Invalid {
        error: format!("{error:#}"),
    }
}

/// One `Invalid` record for a spec entry that could not become an entity.
fn invalid_outcome(
    chunk: &str,
    logical_module: &str,
    entity: Option<Entity>,
    selector_kind: SelectorKind,
    selector: Option<&AnonymousStatementSelector>,
    error: &impl std::fmt::Display,
) -> SelectorOutcome {
    SelectorOutcome {
        chunk: chunk.to_string(),
        placement: Some(Placement {
            logical_module: logical_module.to_string(),
            entity,
            selector_kind,
        }),
        target_binding: selector.and_then(|selector| selector.target_binding.clone()),
        selector_preview: selector
            .map(|selector| source_match::source_match_preview(&selector.match_source)),
        outcome: invalid(error),
    }
}

/// Every module file's entities, resolved together against the chunk.
fn validate_modules_against_source(
    modules_root: &Path,
    source_file: &Path,
    chunk: &str,
) -> Result<SelectorOutcomeReport> {
    let source = std::fs::read_to_string(source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;

    let mut outcomes = Vec::new();
    let mut modules = Vec::new();
    let mut validation = SelectorValidationCounts::default();
    for path in spec_modules::collect_module_files(modules_root)? {
        let module_path = spec_modules::module_path_from_file(&path, modules_root);
        let module = spec_modules::read_module_file(&path)?;
        if let Err(error) = validate_logical_module_claims(
            &module_path,
            &module.members,
            &module.source_matches,
            &module.annotations,
        ) {
            outcomes.push(invalid_outcome(
                chunk,
                &module_path,
                None,
                SelectorKind::Annotations,
                None,
                &error,
            ));
        }
        let mut members = Vec::new();
        for member in module.members {
            match member.selector.selected() {
                // A member without an export name was reported above.
                Ok(selector) => {
                    let export_name = match (&member.name, &selector) {
                        (Some(name), _) => name.clone(),
                        (None, MemberSelectorSpec::Binding(binding)) => binding.name.clone(),
                        (None, _) => {
                            validation.record(false);
                            continue;
                        }
                    };
                    members.push(member_from_spec(export_name, selector));
                }
                Err(error) => {
                    validation.record(false);
                    outcomes.push(invalid_outcome(
                        chunk,
                        &module_path,
                        member.name.map(Entity::Export),
                        SelectorKind::UnparsedMember,
                        None,
                        &error,
                    ));
                }
            }
        }
        for claim in &module.source_matches {
            match source_match_claim_member_selectors(&module_path, claim) {
                Ok(expanded) => members.extend(expanded.into_iter().map(|expanded| {
                    member_from_source_match(expanded.export_name, expanded.parsed_selector)
                })),
                Err(error) => {
                    for _ in &claim.bindings {
                        validation.record(false);
                    }
                    outcomes.push(invalid_outcome(
                        chunk,
                        &module_path,
                        None,
                        SelectorKind::SourceMatches,
                        Some(&claim.source_match().selector()),
                        &error,
                    ));
                }
            }
        }
        // Each export name resolves once; a repeat was reported above.
        let mut export_names = BTreeSet::new();
        let parsed_member_count = members.len();
        members.retain(|member| export_names.insert(member.export_name.clone()));
        for _ in members.len()..parsed_member_count {
            validation.record(false);
        }
        let mut anonymous_statements = Vec::new();
        for (index, statement) in module.anonymous_statements.into_iter().enumerate() {
            let entity = Some(Entity::AnonymousStatement(index));
            let parsed = statement
                .selector()
                .map_err(anyhow::Error::from)
                .and_then(|selector| {
                    parse_anonymous_selector(
                        &module_path,
                        format!("<source_match needle in {module_path}>"),
                        &selector,
                    )
                });
            match parsed {
                Ok(selector) => anonymous_statements.push(anonymous_statement(index, selector)),
                Err(error) => {
                    validation.record(false);
                    outcomes.push(invalid_outcome(
                        chunk,
                        &module_path,
                        entity,
                        SelectorKind::AnonymousStatement,
                        statement.selector().ok().as_ref(),
                        &error,
                    ));
                }
            }
        }
        modules.push(spec_module(module_path, members, anonymous_statements));
    }
    let source_chunk = selector_resolve::Chunk::analyze(chunk, &parsed.module);
    let resolution = resolve_chunk(&source_chunk, &modules)?;
    for resolved in resolution.outcomes {
        validation.record(matches!(
            &resolved.outcome.outcome,
            Outcome::Resolved { .. }
        ));
        if resolved.outcome.severity() != Severity::Ok {
            outcomes.push(resolved.outcome);
        }
    }
    outcomes.sort();
    let mut templates = resolution.templates;
    templates.sort();
    Ok(SelectorOutcomeReport {
        outcomes,
        templates,
        validation: Some(validation),
    })
}

/// Recursively gather every `selector_diagnostics.json` under the capture
/// directory. The materialize pass nests each report at
/// `<capture>/<chunk_id parts>/selector_diagnostics.json`.
fn collect_chunk_reports(
    root: &Path,
    dir: &Path,
    reports: &mut Vec<(String, SelectorOutcomeReport)>,
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
            collect_chunk_reports(root, &path, reports)?;
        } else if path.file_name().and_then(|name| name.to_str())
            == Some(SELECTOR_DIAGNOSTICS_REPORT)
        {
            let chunk = report_chunk_id(root, &path)?;
            let text = std::fs::read_to_string(&path)
                .with_context(|| format!("reading {}", path.display()))?;
            reports.push((
                chunk,
                serde_json::from_str(&text)
                    .with_context(|| format!("parsing selector outcomes {}", path.display()))?,
            ));
        }
    }
    Ok(())
}

fn report_chunk_id(root: &Path, report_path: &Path) -> Result<String> {
    let relative = report_path.strip_prefix(root).with_context(|| {
        format!(
            "selector report {} is outside capture root",
            report_path.display()
        )
    })?;
    let parent = relative
        .parent()
        .context("selector report path has no chunk directory")?;
    let chunk = artifact::paths::module_path_from_path(parent);
    if chunk.is_empty() {
        bail!("selector report is not nested under a chunk directory");
    }
    Ok(chunk)
}

/// One JSON object per outcome, one per listed template, then a final
/// `summary` line with the counts — the streaming shape `jq -c` consumers
/// dispatch on.
fn emit_validate_ndjson(report: &SelectorOutcomeReport) -> Result<()> {
    #[derive(Serialize)]
    struct Summary<T> {
        counts: T,
        #[serde(skip_serializing_if = "Option::is_none")]
        validation: Option<selector_outcome::SelectorValidationSummary>,
    }
    for outcome in &report.outcomes {
        print_section("outcome", outcome)?;
    }
    for template in &report.templates {
        print_section("template", template)?;
    }
    print_section(
        "summary",
        &Summary {
            counts: report.counts(),
            validation: report.validation_summary(),
        },
    )
}
