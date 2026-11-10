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

use analysis::reports::spec_lint::{
    ChunkSpecLint, InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintRule,
    LintedAnnotation, PurityHintOwner,
};
use anyhow::{Context, Result, ensure};
use clap::Args as ClapArgs;
use output_layout::SPEC_LINT_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{AnnotationProbe, AnnotationProbeRun, load_transform_spec_source};
use serde::Serialize;
use serde_json::Value;
use spec::{MemberPurity, ReentryFreeOperation, TransformSpec};
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
}

impl ProbeTarget {
    fn chunk(&self) -> Option<&str> {
        match self {
            Self::PurityHint { chunk, .. } => Some(chunk),
            Self::ReentryFreeOperation(_) => None,
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
        }
        Ok(spec)
    }
}

pub fn run(args: SpecLintArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    if let Some(max_probes) = args.max_probes {
        ensure!(max_probes > 0, "--max-probes must be positive");
    }
    let cli = args.transform.clone().resolve()?;
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let pass = run_captured_pass(args.transform.clone())?;
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
    let mut findings: Vec<LintFinding> = reports
        .into_iter()
        .flat_map(|(_, chunk)| chunk.findings)
        .collect();
    for operation in &spec.host_behavior.reentry_free_operations {
        let verdict = if !unlinted_chunks.is_empty() {
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::UnlintedChunks,
            }
        } else if consumed.contains(operation) {
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::Consumed,
            }
        } else {
            InferenceVerdict::Inferable {
                reason: InferableReason::NeverConsumed,
            }
        };
        findings.push(LintFinding::reentry_free_operation(*operation, verdict));
    }
    let mut report = SpecLintReport {
        findings,
        unlinted_chunks,
        probes: Vec::new(),
    };
    if args.probe_analysis {
        probe_annotations(&args, &cli, &spec, &mut report)?;
    }
    print_report(&report, format, render_text).context("writing spec lint output")
}

/// Withhold each selected annotation in turn and compare the run with the
/// baseline: every `purity: pure` hint, and every `reentry_free_operations`
/// entry some chunk consumed (an unconsumed one is already exact). A probed
/// annotation's finding replaces its cheap lead.
fn probe_annotations(
    args: &SpecLintArgs,
    cli: &pipeline::TransformRequest,
    spec: &TransformSpec,
    report: &mut SpecLintReport,
) -> Result<()> {
    let chunks: BTreeSet<_> = args.probe_chunks.iter().cloned().collect();
    let consumed_operations = report.findings.iter().filter_map(|finding| match finding {
        LintFinding {
            annotation: LintedAnnotation::ReentryFreeOperation { operation },
            verdict:
                InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::Consumed,
                },
            ..
        } => Some(ProbeTarget::ReentryFreeOperation(*operation)),
        _ => None,
    });
    let targets: Vec<_> = purity_targets(spec)
        .into_iter()
        .chain(consumed_operations.collect::<Vec<_>>())
        .filter(|target| {
            chunks.is_empty() || target.chunk().is_some_and(|chunk| chunks.contains(chunk))
        })
        .collect();
    if targets.is_empty() {
        return Ok(());
    }
    let probe = AnnotationProbe::prepare(cli, spec)?;
    let scratch = tempfile::tempdir().context("creating probe scratch dir")?;
    let baseline_root = scratch.path().join("baseline");
    let mut probed = Vec::new();
    let baseline = match probe.run(probe.spec(), &baseline_root)? {
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
            let verdict = match probe.run(&target.withhold(probe.spec())?, &root)? {
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

fn render_text(report: &SpecLintReport, out: &mut String) {
    for finding in &report.findings {
        let (severity, verdict) = match &finding.verdict {
            InferenceVerdict::Inferable { reason } => (
                "warning",
                format!(
                    "inferable ({}); remove it in a reviewed spec edit and revalidate",
                    match reason {
                        InferableReason::NoSequencedEdge => "no sequenced edge joins the endpoints",
                        InferableReason::SummarizedEndpoints =>
                            "the endpoints' summarized effects touch disjoint cells",
                        InferableReason::NoAdmissionViolation =>
                            "the chunk has no violation to override",
                        InferableReason::WithheldRunUnchanged =>
                            "withholding it alone leaves every report unchanged",
                        InferableReason::NeverConsumed => "no analysis consumed it",
                    }
                ),
            ),
            InferenceVerdict::Inconclusive { reason } => (
                "note",
                format!(
                    "inconclusive ({})",
                    match reason {
                        InconclusiveReason::PurityClassification { .. } =>
                            "the binding classifies without the hint; other consumers of the \
                             hint were not probed"
                                .to_owned(),
                        InconclusiveReason::BaselineNotAccepted { error } =>
                            format!("the probe baseline was not accepted: {error}"),
                        InconclusiveReason::ProbeFailed { error } =>
                            format!("the withheld run failed to run: {error}"),
                        InconclusiveReason::NotProbed =>
                            "not probed: --max-probes reached".to_owned(),
                        InconclusiveReason::Consumed =>
                            "consumed by the analysis; --probe-analysis decides whether it is needed"
                                .to_owned(),
                        InconclusiveReason::UnlintedChunks =>
                            "some chunk was not linted, so its uses are unknown".to_owned(),
                    }
                ),
            ),
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
            LintedAnnotation::ReentryFreeOperation { operation } => format!(
                "`host_behavior.reentry_free_operations` entry `{}`",
                serde_json::to_value(operation)
                    .ok()
                    .and_then(|value| value.as_str().map(str::to_owned))
                    .unwrap_or_default()
            ),
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
}
