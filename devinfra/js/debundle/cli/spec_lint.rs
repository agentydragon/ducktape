//! `debundle spec lint` — every author annotation the engine may already
//! infer, for one exact source/spec pair (SPEC.md § Spec lint).
//!
//! It runs the `spec validate` pass (the ordinary dry-run pipeline, with the
//! shared resolver and gate) and reads back each chunk's `spec_lint.json`.
//! `--probe-analysis` additionally reruns the whole program once per
//! analysis-feeding annotation with that one annotation withheld, on the same
//! prepared input, and compares the run's reports with a baseline run.
//! Findings are warnings and suggestions only: the command never edits the
//! spec and exits 0 whenever the pass produced reports.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::time::Instant;

use analysis::analysis_inputs::{HostBehaviorKey, ProgramPremiseKey};
use analysis::reports::spec_lint::{
    ChunkSpecLint, InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintRule,
    LintedAnnotation, LoaderKeyUses, PurityHintOwner,
};
use anyhow::{Context, Result, ensure};
use clap::Args as ClapArgs;
use command_output::write_report;
use output_layout::SPEC_LINT_REPORT;
use peel::OutputFormat;
use pipeline::{AnnotationProbe, AnnotationProbeRun, load_transform_spec_source};
use serde::Serialize;
use serde_json::Value;
use spec::{MemberPurity, ReentryFreeOperation, TransformSpec};
use std::io::Write;
use transform_command::TransformArgs;

use crate::validate::run_captured_pass;

#[derive(Debug, ClapArgs)]
pub struct SpecLintArgs {
    /// The `--spec` / `--tree-config` / package-root flags of `debundle run`.
    #[command(flatten)]
    pub transform: TransformArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,

    /// Rerun the program once per `purity: pure` hint and per consumed
    /// `reentry_free_operations` entry, with that one annotation withheld, and
    /// report it `inferable` when the run is accepted with identical reports.
    /// Costs one full pipeline run per probed annotation.
    #[arg(long)]
    pub probe_analysis: bool,

    /// Probe only annotations of this chunk (repeatable); program-wide
    /// annotations are then not probed. Requires `--probe-analysis`.
    #[arg(long = "probe-chunk", requires = "probe_analysis")]
    pub probe_chunks: Vec<String>,

    /// Probe at most this many annotations; the rest are reported `inconclusive`
    /// (`not_probed`). Requires `--probe-analysis`.
    #[arg(long, requires = "probe_analysis")]
    pub max_probes: Option<usize>,
}

#[derive(Debug, Serialize)]
struct SpecLintReport {
    findings: Vec<LintFinding>,
    /// Chunks the pass stopped before linting: their annotations are unchecked,
    /// not clean.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    unlinted_chunks: Vec<String>,
    /// The pass's own failure when it stopped after linting some chunks.
    #[serde(skip_serializing_if = "Option::is_none")]
    pass_error: Option<String>,
    /// One row per withheld-annotation run of `--probe-analysis`.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    probes: Vec<ProbeTiming>,
}

#[derive(Debug, Serialize)]
struct ProbeTiming {
    #[serde(skip_serializing_if = "Option::is_none")]
    chunk: Option<String>,
    annotation: LintedAnnotation,
    outcome: ProbeOutcome,
    millis: u128,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum ProbeOutcome {
    /// Accepted with identical reports.
    Unchanged,
    /// Rejected, or accepted with different reports: the annotation is used.
    Changed,
    /// The probe could not decide.
    Inconclusive,
}

/// One annotation the probe can withhold.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ProbeTarget {
    PurityHint {
        chunk: String,
        owner: PurityHintOwner,
    },
    /// A consumed `host_behavior.reentry_free_operations` entry.
    ReentryFreeOperation(ReentryFreeOperation),
    /// A consumed `chunk_analysis_options.<chunk>.trusted_dataflow_summaries`.
    TrustedDataflowSummaries { chunk: String },
    /// A consumed `host_behavior` loader key.
    LoaderHostBehavior(HostBehaviorKey),
    /// A consumed loader `program_premises` key.
    LoaderProgramPremise(ProgramPremiseKey),
}

impl ProbeTarget {
    fn chunk(&self) -> Option<&str> {
        match self {
            Self::PurityHint { chunk, .. } | Self::TrustedDataflowSummaries { chunk } => {
                Some(chunk)
            }
            Self::ReentryFreeOperation(_)
            | Self::LoaderHostBehavior(_)
            | Self::LoaderProgramPremise(_) => None,
        }
    }

    fn annotation(&self) -> LintedAnnotation {
        match self {
            Self::PurityHint { owner, .. } => LintedAnnotation::PurityHint {
                owner: owner.clone(),
            },
            Self::ReentryFreeOperation(operation) => LintedAnnotation::ReentryFreeOperation {
                operation: *operation,
            },
            Self::LoaderHostBehavior(key) => LintedAnnotation::LoaderHostBehavior { key: *key },
            Self::LoaderProgramPremise(key) => LintedAnnotation::LoaderProgramPremise { key: *key },
            Self::TrustedDataflowSummaries { chunk } => {
                LintedAnnotation::TrustedDataflowSummaries {
                    spec_location: format!(
                        "chunk_analysis_options.{chunk}.trusted_dataflow_summaries"
                    ),
                }
            }
        }
    }

    fn finding(&self, verdict: InferenceVerdict) -> LintFinding {
        LintFinding {
            rule: LintRule::InferableAnnotation,
            chunk: self.chunk().map(str::to_owned),
            annotation: self.annotation(),
            verdict,
        }
    }

    /// A copy of `spec` without this annotation.
    fn withhold(&self, spec: &TransformSpec) -> Result<TransformSpec> {
        let mut spec = spec.clone();
        match self {
            Self::PurityHint { chunk, owner } => {
                let annotation = match owner {
                    PurityHintOwner::LogicalMember(member) => spec
                        .logical_modules
                        .get_mut(chunk)
                        .and_then(|modules| modules.get_mut(&member.module))
                        .and_then(|module| module.annotations.get_mut(&member.name)),
                    PurityHintOwner::ChunkRename { name } => spec
                        .chunk_renames
                        .get_mut(chunk)
                        .and_then(|renames| renames.annotations.get_mut(name)),
                    PurityHintOwner::InputBinding { .. } => None,
                }
                .with_context(|| format!("purity hint {self:?} vanished from the spec"))?;
                annotation.purity = MemberPurity::default();
            }
            Self::ReentryFreeOperation(operation) => {
                ensure!(
                    spec.host_behavior.reentry_free_operations.remove(operation),
                    "reentry-free operation {operation:?} vanished from the spec"
                );
            }
            Self::LoaderHostBehavior(key) => {
                let host = &mut spec.host_behavior;
                let removed = match key {
                    HostBehaviorKey::LoaderResultForEvaluatingModule => {
                        host.loader_result_for_evaluating_module.take().is_some()
                    }
                    HostBehaviorKey::SynchronousLoadRunsAppCallbacks => {
                        host.synchronous_load_runs_app_callbacks.take().is_some()
                    }
                    HostBehaviorKey::SynchronousLoadDrainsMicrotasks => {
                        host.synchronous_load_drains_microtasks.take().is_some()
                    }
                    HostBehaviorKey::ReentryFreeEmbeddedLoader(loader) => {
                        host.reentry_free_embedded_loaders.remove(loader)
                    }
                    _ => false,
                };
                ensure!(removed, "loader key {key:?} vanished from the spec");
            }
            Self::LoaderProgramPremise(key) => {
                ensure!(
                    *key == ProgramPremiseKey::NonLiteralLoaderCalls
                        && spec.program_premises.non_literal_loader_calls
                            != spec::NonLiteralLoaderCalls::Unknown,
                    "loader premise {key:?} vanished from the spec"
                );
                spec.program_premises.non_literal_loader_calls = Default::default();
            }
            Self::TrustedDataflowSummaries { chunk } => {
                let options = spec
                    .chunk_analysis_options
                    .get_mut(chunk)
                    .filter(|options| options.trusted_dataflow_summaries)
                    .with_context(|| {
                        format!("trusted_dataflow_summaries for {chunk:?} vanished from the spec")
                    })?;
                options.trusted_dataflow_summaries = false;
            }
        }
        Ok(spec)
    }
}

pub fn run(
    stderr: &mut dyn std::io::Write,
    args: SpecLintArgs,
    output: &mut dyn Write,
) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    if let Some(max_probes) = args.max_probes {
        ensure!(max_probes > 0, "--max-probes must be positive");
    }
    let cli = args.transform.context.clone().resolve()?;
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let pass = run_captured_pass(stderr, args.transform.clone())?;
    let reports: Vec<(String, ChunkSpecLint)> = pass.chunk_reports(SPEC_LINT_REPORT)?;
    if reports.is_empty()
        && let Some(error) = pass.error
    {
        return Err(error).context("running the spec lint pass");
    }
    let unlinted_chunks: Vec<String> = pass
        .expected_chunks
        .iter()
        .filter(|chunk| !reports.iter().any(|(linted, _)| linted == *chunk))
        .cloned()
        .collect();
    let consumed: BTreeSet<_> = reports
        .iter()
        .flat_map(|(_, chunk)| chunk.consumed_reentry_free_operations.iter().copied())
        .collect();
    let loader_uses = reports
        .iter()
        .map(|(_, chunk)| chunk.loader_key_uses.clone())
        .fold(LoaderKeyUses::default(), LoaderKeyUses::join);
    let provenance_verdict = |consumed: bool| {
        if !unlinted_chunks.is_empty() {
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::UnlintedChunks,
            }
        } else if consumed {
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::Consumed,
            }
        } else {
            InferenceVerdict::Inferable {
                reason: InferableReason::NeverConsumed,
            }
        }
    };
    let mut findings: Vec<LintFinding> = reports
        .into_iter()
        .flat_map(|(_, chunk)| chunk.findings)
        .collect();
    for operation in &spec.host_behavior.reentry_free_operations {
        findings.push(LintFinding::reentry_free_operation(
            *operation,
            provenance_verdict(consumed.contains(operation)),
        ));
    }
    for key in authored_loader_host_keys(&spec.host_behavior) {
        let consumed = match &loader_uses {
            LoaderKeyUses::Recorded { host_keys, .. } => host_keys.contains(&key),
            LoaderKeyUses::Unattributed => true,
        };
        findings.push(LintFinding::program_wide(
            LintedAnnotation::LoaderHostBehavior { key },
            provenance_verdict(consumed),
        ));
    }
    if spec.program_premises.non_literal_loader_calls != spec::NonLiteralLoaderCalls::Unknown {
        let key = ProgramPremiseKey::NonLiteralLoaderCalls;
        // Spec validation requires this premise beside an `napi` embedded
        // loader, so withholding it is never a valid edit then.
        let consumed = spec
            .host_behavior
            .reentry_free_embedded_loaders
            .contains(&spec::EmbeddedLoader::Napi)
            || match &loader_uses {
                LoaderKeyUses::Recorded { premise_keys, .. } => premise_keys.contains(&key),
                LoaderKeyUses::Unattributed => true,
            };
        findings.push(LintFinding::program_wide(
            LintedAnnotation::LoaderProgramPremise { key },
            provenance_verdict(consumed),
        ));
    }
    let mut report = SpecLintReport {
        findings,
        unlinted_chunks,
        pass_error: pass.error.map(|error| format!("{error:#}")),
        probes: Vec::new(),
    };
    if args.probe_analysis {
        probe_annotations(stderr, &args, &cli, &spec, &mut report)?;
    }
    write_report(&report, format, output, |report, out| {
        out.push_str(&render_text(report)?);
        Ok(())
    })
    .context("writing spec lint output")
}

/// The loader keys the spec sets to a value the loader closure accepts:
/// declared but `unknown` values are treated as undeclared and never consumed.
fn authored_loader_host_keys(host: &spec::HostBehavior) -> Vec<HostBehaviorKey> {
    let mut keys = Vec::new();
    if host.loader_result_for_evaluating_module.is_some() {
        keys.push(HostBehaviorKey::LoaderResultForEvaluatingModule);
    }
    if host.synchronous_load_runs_app_callbacks.is_some() {
        keys.push(HostBehaviorKey::SynchronousLoadRunsAppCallbacks);
    }
    if host.synchronous_load_drains_microtasks.is_some() {
        keys.push(HostBehaviorKey::SynchronousLoadDrainsMicrotasks);
    }
    keys.extend(
        host.reentry_free_embedded_loaders
            .iter()
            .map(|loader| HostBehaviorKey::ReentryFreeEmbeddedLoader(*loader)),
    );
    keys
}

/// Withhold each selected annotation in turn and compare the run with the
/// baseline: every `purity: pure` hint, and every provenance-decided premise
/// some analysis consumed (an unconsumed one is already exact). A probed
/// annotation's finding replaces its cheap lead.
fn probe_annotations(
    stderr: &mut dyn std::io::Write,
    args: &SpecLintArgs,
    cli: &pipeline::TransformRequest,
    spec: &TransformSpec,
    report: &mut SpecLintReport,
) -> Result<()> {
    let chunks: BTreeSet<_> = args.probe_chunks.iter().cloned().collect();
    let consumed = report.findings.iter().filter_map(|finding| {
        if !matches!(
            finding.verdict,
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::Consumed,
            }
        ) {
            return None;
        }
        match (&finding.annotation, &finding.chunk) {
            (LintedAnnotation::ReentryFreeOperation { operation }, _) => {
                Some(ProbeTarget::ReentryFreeOperation(*operation))
            }
            (LintedAnnotation::LoaderHostBehavior { key }, _) => {
                Some(ProbeTarget::LoaderHostBehavior(*key))
            }
            (LintedAnnotation::LoaderProgramPremise { key }, _) => {
                Some(ProbeTarget::LoaderProgramPremise(*key))
            }
            (LintedAnnotation::TrustedDataflowSummaries { .. }, Some(chunk)) => {
                Some(ProbeTarget::TrustedDataflowSummaries {
                    chunk: chunk.clone(),
                })
            }
            _ => None,
        }
    });
    let targets: Vec<_> = purity_targets(spec)
        .into_iter()
        .chain(consumed.collect::<Vec<_>>())
        .filter(|target| {
            chunks.is_empty() || target.chunk().is_some_and(|chunk| chunks.contains(chunk))
        })
        // Provenance already decided these exactly.
        .filter(|target| {
            !report.findings.iter().any(|finding| {
                finding.annotation == target.annotation()
                    && finding.chunk.as_deref() == target.chunk()
                    && matches!(finding.verdict, InferenceVerdict::Inferable { .. })
            })
        })
        .collect();
    if targets.is_empty() {
        return Ok(());
    }
    let probe = AnnotationProbe::prepare(cli, spec)?;
    let scratch = tempfile::tempdir().context("creating probe scratch dir")?;
    let baseline_root = scratch.path().join("baseline");
    let mut probed = Vec::new();
    let baseline = match command_diagnostics::run(stderr, |diagnostics| {
        probe.run(probe.spec(), &baseline_root, diagnostics)
    })? {
        AnnotationProbeRun::Accepted => Some(report_tree(&baseline_root)?),
        AnnotationProbeRun::Rejected(error) | AnnotationProbeRun::Inconclusive(error) => {
            let error = format!("{error:#}");
            for target in &targets {
                probed.push(target.finding(InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::BaselineNotAccepted {
                        error: error.clone(),
                    },
                }));
            }
            None
        }
    };
    if let Some(baseline) = baseline {
        let budget = args.max_probes.unwrap_or(usize::MAX);
        for (index, target) in targets.iter().enumerate() {
            if index >= budget {
                probed.push(target.finding(InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::NotProbed,
                }));
                continue;
            }
            let root = scratch.path().join(format!("probe-{index}"));
            let started = Instant::now();
            let verdict = match command_diagnostics::run(stderr, |diagnostics| {
                probe.run(&target.withhold(probe.spec())?, &root, diagnostics)
            })? {
                AnnotationProbeRun::Accepted if report_tree(&root)? == baseline => {
                    Some(InferenceVerdict::Inferable {
                        reason: InferableReason::WithheldRunUnchanged,
                    })
                }
                AnnotationProbeRun::Accepted | AnnotationProbeRun::Rejected(_) => None,
                AnnotationProbeRun::Inconclusive(error) => Some(InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::ProbeFailed {
                        error: format!("{error:#}"),
                    },
                }),
            };
            report.probes.push(ProbeTiming {
                chunk: target.chunk().map(str::to_owned),
                annotation: target.annotation(),
                outcome: match &verdict {
                    Some(InferenceVerdict::Inferable { .. }) => ProbeOutcome::Unchanged,
                    Some(InferenceVerdict::Inconclusive { .. }) => ProbeOutcome::Inconclusive,
                    None => ProbeOutcome::Changed,
                },
                millis: started.elapsed().as_millis(),
            });
            if let Some(verdict) = verdict {
                probed.push(target.finding(verdict));
            }
            std::fs::remove_dir_all(&root)
                .with_context(|| format!("removing probe dir {}", root.display()))?;
        }
    }
    // The probe supersedes every cheap lead for the annotations it targeted.
    report.findings.retain(|finding| {
        !targets.iter().any(|target| {
            finding.annotation == target.annotation()
                && finding.chunk.as_deref() == target.chunk()
                && matches!(finding.verdict, InferenceVerdict::Inconclusive { .. })
        })
    });
    report.findings.extend(probed);
    Ok(())
}

/// Every authored `purity: pure` annotation, in spec order.
fn purity_targets(spec: &TransformSpec) -> Vec<ProbeTarget> {
    let modules = spec.logical_modules.iter().flat_map(|(chunk, modules)| {
        modules.iter().flat_map(move |(module, logical)| {
            logical
                .annotations
                .iter()
                .filter(|(_, annotation)| annotation.purity == MemberPurity::Pure)
                .map(move |(name, _)| ProbeTarget::PurityHint {
                    chunk: chunk.clone(),
                    owner: PurityHintOwner::LogicalMember(spec::ResolvedMemberSelector {
                        module: module.clone(),
                        name: name.clone(),
                    }),
                })
        })
    });
    let renames = spec.chunk_renames.iter().flat_map(|(chunk, renames)| {
        renames
            .annotations
            .iter()
            .filter(|(_, annotation)| annotation.purity == MemberPurity::Pure)
            .map(move |(name, _)| ProbeTarget::PurityHint {
                chunk: chunk.clone(),
                owner: PurityHintOwner::ChunkRename { name: name.clone() },
            })
    });
    modules.chain(renames).collect()
}

/// Every report a run wrote under `root`, keyed by relative path, decoded
/// from JSON (and gzip) where possible, with `root` spelled `<root>`. Excluded
/// are the hint diagnostics a withheld hint changes by construction:
/// `spec_lint.json` and `modules.json`'s `redundant_purity_hints`.
fn report_tree(root: &Path) -> Result<BTreeMap<PathBuf, String>> {
    let root_spelling = root.display().to_string();
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in
            std::fs::read_dir(&dir).with_context(|| format!("reading {}", dir.display()))?
        {
            let path = entry?.path();
            if path.is_dir() {
                pending.push(path);
                continue;
            }
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            if name == SPEC_LINT_REPORT {
                continue;
            }
            let text = if name.ends_with(".json.gz") {
                serde_json::to_string(&report_io::read_json_gzip::<Value>(&path)?)?
            } else {
                let bytes =
                    std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
                match serde_json::from_slice::<Value>(&bytes) {
                    Ok(mut value) => {
                        if name == "modules.json"
                            && let Some(object) = value.as_object_mut()
                        {
                            object.remove("redundant_purity_hints");
                        }
                        serde_json::to_string(&value)?
                    }
                    Err(_) => String::from_utf8_lossy(&bytes).into_owned(),
                }
            };
            files.insert(
                path.strip_prefix(root)?.to_path_buf(),
                text.replace(&root_spelling, "<root>"),
            );
        }
    }
    Ok(files)
}

fn render_text(report: &SpecLintReport) -> Result<String> {
    let mut out = String::new();
    for finding in &report.findings {
        let (severity, verdict) = match &finding.verdict {
            InferenceVerdict::Inferable { reason } => (
                "warning",
                format!(
                    "inferable ({}); remove it in a reviewed spec edit and revalidate",
                    reason.explanation()
                ),
            ),
            InferenceVerdict::Inconclusive { reason } => {
                ("note", format!("inconclusive ({})", reason.explanation()))
            }
        };
        let subject = match &finding.annotation {
            LintedAnnotation::PurityHint {
                owner: PurityHintOwner::LogicalMember(member),
            } => format!(
                "`purity: pure` on module `{}` member `{}`",
                member.module, member.name
            ),
            LintedAnnotation::PurityHint {
                owner: PurityHintOwner::ChunkRename { name },
            } => format!("`purity: pure` on chunk_renames annotation `{name}`"),
            LintedAnnotation::PurityHint {
                owner: PurityHintOwner::InputBinding { binding },
            } => format!("`purity: pure` on input binding `{binding}`"),
            LintedAnnotation::AdmissionOverride {
                spec_location,
                check,
            } => format!("admission override `{check}` at {spec_location}"),
            LintedAnnotation::LoaderHostBehavior { key } => {
                format!("`host_behavior` loader key {}", serde_json::to_string(key)?)
            }
            LintedAnnotation::LoaderProgramPremise { key } => {
                format!("`program_premises` loader key \"{key}\"")
            }
            LintedAnnotation::TrustedDataflowSummaries { spec_location } => {
                format!("`trusted_dataflow_summaries` at {spec_location}")
            }
            LintedAnnotation::ReentryFreeOperation { operation } => {
                format!("`host_behavior.reentry_free_operations` entry `{operation}`")
            }
            LintedAnnotation::CommutingStatements {
                spec_location,
                a,
                b,
            } => {
                format!("{spec_location}: endpoints {a} and {b}")
            }
        };
        let scope = finding
            .chunk
            .as_ref()
            .map_or_else(|| "program".to_owned(), |chunk| format!("chunk {chunk}"));
        let _ = writeln!(
            out,
            "{severity}[inferable_annotation]: {scope}: {subject}: {verdict}"
        );
    }
    if let Some(error) = &report.pass_error {
        let _ = writeln!(
            out,
            "note: the lint pass failed after linting some chunks: {error}"
        );
    }
    for chunk in &report.unlinted_chunks {
        let _ = writeln!(
            out,
            "note: chunk {chunk} was not linted: the pass stopped earlier"
        );
    }
    if !report.probes.is_empty() {
        for probe in &report.probes {
            let _ = writeln!(
                out,
                "probe: {}: {:?}: {:?} in {} ms",
                probe.chunk.as_deref().unwrap_or("program"),
                probe.annotation,
                probe.outcome,
                probe.millis
            );
        }
        let total: u128 = report.probes.iter().map(|probe| probe.millis).sum();
        let _ = writeln!(
            out,
            "{} probe(s) in {total} ms; each withholds one annotation, so two inferable \
             findings are not proven jointly removable",
            report.probes.len()
        );
    }
    let _ = writeln!(out, "{} finding(s)", report.findings.len());
    Ok(out)
}
