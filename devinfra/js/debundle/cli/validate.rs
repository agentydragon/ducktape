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

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use output_layout::SELECTOR_DIAGNOSTICS_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli_with_options};
use selector_outcome::{
    Entity, Outcome, Placement, SelectorKind, SelectorOutcome, SelectorOutcomeReport, Severity,
    TemplateIdentifiers,
};
use selector_resolve::{AnonymousStatement, Member, MemberSelector, SpecModule};
use serde::Serialize;
use source_match::{ParsedSourceMatchSelector, source_match_claim_member_selectors};
use spec::{AnonymousStatementSelector, MemberSelectorSpec};

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
    let report = if args.source_only_requested() {
        js_ast::with_swc_globals(|| run_source_only_validate(&args))?
    } else {
        run_spec_validate(args)?
    };
    if format == OutputFormat::Ndjson {
        return emit_validate_ndjson(&report);
    }
    print_report(&report, format, |report, buf| report.render_text(buf, None))
        .context("writing validate output")
}

fn run_spec_validate(args: ValidateArgs) -> Result<SelectorOutcomeReport> {
    let keep_going = !args.transform.fail_fast;
    let cli = args.transform.resolve()?;

    // Force reports into a private capture dir: dry-run + a report dir makes
    // the materialize pass emit `selector_diagnostics.json` per chunk on
    // rejection, independent of how the spec configures `report_out_dir`.
    let capture = tempfile::tempdir().context("creating selector-diagnostics capture dir")?;
    // The keep-going pass writes the per-chunk reports *and then* fails the
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
            list_template_identifiers: true,
        },
    );

    let mut chunks = Vec::new();
    collect_chunk_reports(capture.path(), &mut chunks)?;
    if let Err(error) = pass
        && chunks.is_empty()
    {
        return Err(error).context("running keep-going validation pass");
    }
    let mut report = SelectorOutcomeReport::default();
    for chunk in chunks {
        report.outcomes.extend(chunk.outcomes);
        report.templates.extend(chunk.templates);
    }
    report.outcomes.sort();
    report.templates.sort();
    Ok(report)
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
    let source_file = resolve_source_only_chunk_file(
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
    for path in spec_modules::collect_module_files(modules_root)? {
        let module_path = spec_modules::module_path_from_file(&path, modules_root);
        let module = spec_modules::read_module_file(&path)?;
        if let Err(error) = lowering::validate_logical_module_claims(
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
                        (None, _) => continue,
                    };
                    match MemberSelector::from_spec(&module_path, selector) {
                        Ok(selector) => members.push(Member {
                            export_name,
                            selector,
                        }),
                        Err(error) => outcomes.push(invalid_outcome(
                            chunk,
                            &module_path,
                            Some(Entity::Export(export_name)),
                            SelectorKind::UnparsedMember,
                            None,
                            &error,
                        )),
                    }
                }
                Err(error) => outcomes.push(invalid_outcome(
                    chunk,
                    &module_path,
                    member.name.map(Entity::Export),
                    SelectorKind::UnparsedMember,
                    None,
                    &error,
                )),
            }
        }
        for claim in &module.source_matches {
            match source_match_claim_member_selectors(&module_path, claim) {
                Ok(expanded) => members.extend(expanded.into_iter().map(|expanded| Member {
                    export_name: expanded.export_name,
                    selector: MemberSelector::SourceMatch(expanded.parsed_selector),
                })),
                Err(error) => outcomes.push(invalid_outcome(
                    chunk,
                    &module_path,
                    None,
                    SelectorKind::SourceMatches,
                    Some(&claim.source_match().selector()),
                    &error,
                )),
            }
        }
        // Each export name resolves once; a repeat was reported above.
        let mut export_names = BTreeSet::new();
        members.retain(|member| export_names.insert(member.export_name.clone()));
        let mut anonymous_statements = Vec::new();
        for (index, statement) in module.anonymous_statements.into_iter().enumerate() {
            let entity = Some(Entity::AnonymousStatement(index));
            let parsed = statement
                .selector()
                .map_err(anyhow::Error::from)
                .and_then(|selector| {
                    ParsedSourceMatchSelector::parse(
                        &module_path,
                        "source_match",
                        format!("<source_match needle in {module_path}>"),
                        &selector,
                        "source_match",
                    )
                });
            match parsed {
                Ok(selector) => anonymous_statements.push(AnonymousStatement { index, selector }),
                Err(error) => outcomes.push(invalid_outcome(
                    chunk,
                    &module_path,
                    entity,
                    SelectorKind::AnonymousStatement,
                    statement.selector().ok().as_ref(),
                    &error,
                )),
            }
        }
        modules.push(SpecModule {
            path: module_path,
            members,
            anonymous_statements,
        });
    }
    let resolution = selector_resolve::Chunk::analyze(chunk, &parsed.module).resolve(&modules)?;
    outcomes.extend(
        resolution
            .outcomes
            .into_iter()
            .map(|resolved| resolved.outcome)
            .filter(|outcome| outcome.severity() != Severity::Ok),
    );
    outcomes.sort();
    let mut templates = resolution.templates;
    templates.sort();
    Ok(SelectorOutcomeReport {
        outcomes,
        templates,
    })
}

/// Recursively gather every `selector_diagnostics.json` under the capture
/// directory. The materialize pass nests each report at
/// `<capture>/<chunk_id parts>/selector_diagnostics.json`.
fn collect_chunk_reports(dir: &Path, reports: &mut Vec<SelectorOutcomeReport>) -> Result<()> {
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
            collect_chunk_reports(&path, reports)?;
        } else if path.file_name().and_then(|name| name.to_str())
            == Some(SELECTOR_DIAGNOSTICS_REPORT)
        {
            let text = std::fs::read_to_string(&path)
                .with_context(|| format!("reading {}", path.display()))?;
            reports.push(
                serde_json::from_str(&text)
                    .with_context(|| format!("parsing selector outcomes {}", path.display()))?,
            );
        }
    }
    Ok(())
}

/// One JSON object per outcome, one per listed template, then a final
/// `summary` line with the counts — the streaming shape `jq -c` consumers
/// dispatch on.
fn emit_validate_ndjson(report: &SelectorOutcomeReport) -> Result<()> {
    #[derive(Serialize)]
    struct OutcomeLine<'a> {
        section: &'static str,
        #[serde(flatten)]
        outcome: &'a SelectorOutcome,
    }
    #[derive(Serialize)]
    struct TemplateLine<'a> {
        section: &'static str,
        #[serde(flatten)]
        template: &'a TemplateIdentifiers,
    }
    #[derive(Serialize)]
    struct SummaryLine<T: Serialize> {
        section: &'static str,
        counts: T,
    }
    for outcome in &report.outcomes {
        println!(
            "{}",
            serde_json::to_string(&OutcomeLine {
                section: "outcome",
                outcome,
            })?
        );
    }
    for template in &report.templates {
        println!(
            "{}",
            serde_json::to_string(&TemplateLine {
                section: "template",
                template,
            })?
        );
    }
    println!(
        "{}",
        serde_json::to_string(&SummaryLine {
            section: "summary",
            counts: report.counts(),
        })?
    );
    Ok(())
}
