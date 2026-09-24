//! `debundle spec validate` — keep-going selector validation that reports every
//! selector that did not resolve, as a [`SelectorOutcomeReport`].
//!
//! The spec mode is a thin frontend over the materialize pass: it runs the
//! dry-run keep-going pipeline with reports forced into a capture directory,
//! reads the per-chunk `selector_diagnostics.json` reports back and re-emits
//! them combined on stdout in the standard `--format text|json|ndjson`
//! convention.
//!
//! The source-only mode (`--modules <modules-dir> --source-file <chunk.js>`)
//! runs the shape matcher per selector, with the same candidate cap, and never
//! enters the joint CP-SAT solve: a fast preflight for sharding source selector
//! repairs across agents. Without the solve it cannot report conflicts,
//! resolution by elimination, duplicate claims or relational selectors.

use std::path::Path;

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use output_layout::SELECTOR_DIAGNOSTICS_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli_with_options};
use selector_outcome::{
    Candidate, Entity, MAX_CANDIDATES_PER_SELECTOR, Outcome, Placement, SelectorKind,
    SelectorOutcome, SelectorOutcomeReport,
};
use serde::Serialize;
use source_match::SelectorResolver;
use source_match::chunk_resolver::ChunkResolver;
use source_match::source_match_claim_member_selectors;
use spec::{AnonymousStatementSelector, MemberSelectorSpec, SourceMatchClaim};

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
        },
    );

    let mut chunks = Vec::new();
    collect_chunk_reports(capture.path(), &mut chunks)?;
    if let Err(error) = pass
        && chunks.is_empty()
    {
        return Err(error).context("running keep-going validation pass");
    }
    let mut outcomes = chunks
        .into_iter()
        .flat_map(|chunk| chunk.outcomes)
        .collect::<Vec<_>>();
    outcomes.sort();
    Ok(SelectorOutcomeReport { outcomes })
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

/// Records the outcome of every selector in one module file that did not
/// resolve on its own.
struct ModuleOutcomes<'a> {
    chunk: &'a str,
    logical_module: String,
    outcomes: &'a mut Vec<SelectorOutcome>,
}

impl ModuleOutcomes<'_> {
    fn record(
        &mut self,
        entity: Option<Entity>,
        selector_kind: SelectorKind,
        selector: Option<&AnonymousStatementSelector>,
        outcome: Outcome,
    ) {
        if matches!(outcome, Outcome::Resolved { .. }) {
            return;
        }
        self.outcomes.push(SelectorOutcome {
            chunk: self.chunk.to_string(),
            placement: Some(Placement {
                logical_module: self.logical_module.clone(),
                entity,
                selector_kind,
            }),
            target_binding: selector.and_then(|selector| selector.target_binding.clone()),
            selector_preview: selector
                .map(|selector| source_match::source_match_preview(&selector.match_source)),
            outcome,
        });
    }
}

fn invalid(error: &impl std::fmt::Display) -> Outcome {
    Outcome::Invalid {
        error: format!("{error:#}"),
    }
}

fn validate_modules_against_source(
    modules_root: &Path,
    source_file: &Path,
    chunk: &str,
) -> Result<SelectorOutcomeReport> {
    let source = std::fs::read_to_string(source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;
    let resolver = ChunkResolver::new(&parsed.module);

    let mut outcomes = Vec::new();
    for path in spec_modules::collect_module_files(modules_root)? {
        let module_path = spec_modules::module_path_from_file(&path, modules_root);
        let module = spec_modules::read_module_file(&path)?;
        if let Err(error) = lowering::validate_logical_module_claims(
            &module_path,
            &module.members,
            &module.source_matches,
            &module.annotations,
        ) {
            outcomes.push(SelectorOutcome {
                chunk: chunk.to_string(),
                placement: Some(Placement {
                    logical_module: module_path.clone(),
                    entity: None,
                    selector_kind: SelectorKind::Annotations,
                }),
                target_binding: None,
                selector_preview: None,
                outcome: invalid(&error),
            });
        }
        let mut module_outcomes = ModuleOutcomes {
            chunk,
            logical_module: module_path.clone(),
            outcomes: &mut outcomes,
        };
        for member in module.members {
            let entity = member.name.clone().map(Entity::Export);
            match member.selector.selected() {
                Ok(MemberSelectorSpec::SourceMatch(selector)) => module_outcomes.record(
                    entity,
                    SelectorKind::MemberSourceMatch,
                    Some(&selector),
                    member_outcome(&resolver, &module_path, &selector),
                ),
                Ok(_) => {}
                Err(error) => module_outcomes.record(
                    entity,
                    SelectorKind::UnparsedMember,
                    None,
                    invalid(&error),
                ),
            }
        }
        for claim in &module.source_matches {
            validate_source_match_claim(&resolver, &mut module_outcomes, &module_path, claim)?;
        }
        for (index, statement) in module.anonymous_statements.into_iter().enumerate() {
            let entity = Some(Entity::AnonymousStatement(index));
            match statement.selector() {
                Ok(selector) => {
                    let outcome = match resolver.anonymous_group_candidates(&module_path, &selector)
                    {
                        Ok(groups) => Outcome::from_matches(
                            groups
                                .into_iter()
                                .flatten()
                                .map(|owner| Candidate {
                                    owner,
                                    binding: None,
                                })
                                .collect(),
                        ),
                        Err(error) => invalid(&error),
                    };
                    module_outcomes.record(
                        entity,
                        SelectorKind::AnonymousStatement,
                        Some(&selector),
                        outcome,
                    );
                }
                Err(error) => module_outcomes.record(
                    entity,
                    SelectorKind::AnonymousStatement,
                    None,
                    invalid(&error),
                ),
            }
        }
    }
    outcomes.sort();
    Ok(SelectorOutcomeReport { outcomes })
}

fn member_outcome(
    resolver: &ChunkResolver<'_>,
    module_path: &str,
    selector: &AnonymousStatementSelector,
) -> Outcome {
    match resolver.member_candidates(module_path, selector) {
        Ok(matches) => Outcome::from_matches(
            matches
                .into_iter()
                .map(|matched| Candidate {
                    owner: matched.body_idx,
                    binding: Some(matched.binding.binding_name),
                })
                .collect(),
        ),
        Err(error) => invalid(&error),
    }
}

/// A one-binding claim resolves like a member selector; a multi-binding claim
/// matches as a group, and each binding gets the group's outcome with its own
/// candidates.
fn validate_source_match_claim(
    resolver: &ChunkResolver<'_>,
    outcomes: &mut ModuleOutcomes<'_>,
    module_path: &str,
    claim: &SourceMatchClaim,
) -> Result<()> {
    let selectors = match source_match_claim_member_selectors(module_path, claim) {
        Ok(selectors) => selectors,
        Err(error) => {
            outcomes.record(
                None,
                SelectorKind::SourceMatches,
                Some(&claim.source_match().selector()),
                invalid(&error),
            );
            return Ok(());
        }
    };
    if let [only] = selectors.as_slice() {
        outcomes.record(
            Some(Entity::Export(only.export_name.clone())),
            SelectorKind::SourceMatches,
            Some(&only.selector),
            member_outcome(resolver, module_path, &only.selector),
        );
        return Ok(());
    }

    let mut exports_by_target = std::collections::BTreeMap::new();
    for selector in &selectors {
        let Some(target_binding) = selector.selector.target_binding.as_deref() else {
            bail!("source_matches expansion for {module_path} did not set target_binding");
        };
        exports_by_target.insert(target_binding.to_string(), selector.export_name.clone());
    }
    let matches = resolver.member_group_candidates(
        module_path,
        &claim.source_match().selector(),
        &exports_by_target,
    );
    for selector in selectors {
        let outcome = match &matches {
            Err(error) => invalid(error),
            Ok(matches) if matches.len() == 1 => continue,
            Ok(matches) if matches.is_empty() => Outcome::NoMatch,
            Ok(matches) if matches.len() > MAX_CANDIDATES_PER_SELECTOR => {
                Outcome::too_broad(matches.len())
            }
            Ok(matches) => {
                let target_binding = selector
                    .selector
                    .target_binding
                    .as_deref()
                    .expect("source_match claim expansion sets target_binding");
                Outcome::ambiguous(
                    matches
                        .iter()
                        .filter_map(|matched| matched.bindings.get(target_binding))
                        .map(|binding| Candidate {
                            owner: binding.body_idx,
                            binding: Some(binding.binding.binding_name.clone()),
                        })
                        .collect(),
                    false,
                )
            }
        };
        outcomes.record(
            Some(Entity::Export(selector.export_name)),
            SelectorKind::SourceMatches,
            Some(&selector.selector),
            outcome,
        );
    }
    Ok(())
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

/// One JSON object per outcome, then a final `summary` line with the counts —
/// the streaming shape `jq -c` consumers dispatch on.
fn emit_validate_ndjson(report: &SelectorOutcomeReport) -> Result<()> {
    #[derive(Serialize)]
    struct OutcomeLine<'a> {
        section: &'static str,
        #[serde(flatten)]
        outcome: &'a SelectorOutcome,
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
    println!(
        "{}",
        serde_json::to_string(&SummaryLine {
            section: "summary",
            counts: report.counts(),
        })?
    );
    Ok(())
}
