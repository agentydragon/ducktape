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
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintRule, LintedAnnotation,
    PurityHintOwner,
};
use anyhow::{Context, Result, ensure};
use clap::Args as ClapArgs;
use output_layout::SPEC_LINT_REPORT;
use peel::{OutputFormat, print_report};
use pipeline::{AnnotationProbe, AnnotationProbeRun, load_transform_spec_source};
use serde::Serialize;
use serde_json::Value;
use spec::{MemberPurity, TransformSpec};
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

    /// Rerun the program once per `purity: pure` hint with that hint withheld
    /// and report the hint `inferable` when the run is accepted with identical
    /// reports. Costs one full pipeline run per probed hint.
    #[arg(long)]
    pub probe_analysis: bool,

    /// Probe only hints of this chunk (repeatable). Requires `--probe-analysis`.
    #[arg(long = "probe-chunk", requires = "probe_analysis")]
    pub probe_chunks: Vec<String>,

    /// Probe at most this many hints; the rest are reported `inconclusive`
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
    chunk: String,
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

/// One `purity: pure` hint the probe can withhold.
#[derive(Debug, Clone)]
struct PurityTarget {
    chunk: String,
    owner: PurityHintOwner,
}

pub fn run(args: SpecLintArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    if let Some(max_probes) = args.max_probes {
        ensure!(max_probes > 0, "--max-probes must be positive");
    }
    let pass = run_captured_pass(args.transform.clone())?;
    let reports: Vec<(String, Vec<LintFinding>)> = pass.chunk_reports(SPEC_LINT_REPORT)?;
    if reports.is_empty()
        && let Some(error) = pass.error
    {
        return Err(error).context("running the spec lint pass");
    }
    let unlinted_chunks = pass
        .expected_chunks
        .iter()
        .filter(|chunk| !reports.iter().any(|(linted, _)| linted == *chunk))
        .cloned()
        .collect();
    let mut report = SpecLintReport {
        findings: reports
            .into_iter()
            .flat_map(|(_, findings)| findings)
            .collect(),
        unlinted_chunks,
        probes: Vec::new(),
    };
    if args.probe_analysis {
        probe_purity_hints(&args, &mut report)?;
    }
    print_report(&report, format, render_text).context("writing spec lint output")
}

/// Withhold each selected `purity: pure` hint in turn and compare the run with
/// the baseline. A probed hint's finding replaces its classifier lead.
fn probe_purity_hints(args: &SpecLintArgs, report: &mut SpecLintReport) -> Result<()> {
    let cli = args.transform.clone().resolve()?;
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let chunks: BTreeSet<_> = args.probe_chunks.iter().cloned().collect();
    let targets: Vec<_> = purity_targets(&spec)
        .into_iter()
        .filter(|target| chunks.is_empty() || chunks.contains(&target.chunk))
        .collect();
    if targets.is_empty() {
        return Ok(());
    }
    let probe = AnnotationProbe::prepare(&cli, &spec)?;
    let scratch = tempfile::tempdir().context("creating probe scratch dir")?;
    let baseline_root = scratch.path().join("baseline");
    let baseline = match probe.run(probe.spec(), &baseline_root)? {
        AnnotationProbeRun::Accepted => Some(report_tree(&baseline_root)?),
        AnnotationProbeRun::Rejected(error) | AnnotationProbeRun::Inconclusive(error) => {
            let error = format!("{error:#}");
            for target in &targets {
                report.findings.push(purity_finding(
                    target,
                    InferenceVerdict::Inconclusive {
                        reason: InconclusiveReason::BaselineNotAccepted {
                            error: error.clone(),
                        },
                    },
                ));
            }
            None
        }
    };
    if let Some(baseline) = baseline {
        let budget = args.max_probes.unwrap_or(usize::MAX);
        for (index, target) in targets.iter().enumerate() {
            if index >= budget {
                report.findings.push(purity_finding(
                    target,
                    InferenceVerdict::Inconclusive {
                        reason: InconclusiveReason::NotProbed,
                    },
                ));
                continue;
            }
            let root = scratch.path().join(format!("probe-{index}"));
            let started = Instant::now();
            let verdict = match probe.run(&withhold_purity(probe.spec(), target)?, &root)? {
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
                chunk: target.chunk.clone(),
                annotation: LintedAnnotation::PurityHint {
                    owner: target.owner.clone(),
                },
                outcome: match &verdict {
                    Some(InferenceVerdict::Inferable { .. }) => ProbeOutcome::Unchanged,
                    Some(InferenceVerdict::Inconclusive { .. }) => ProbeOutcome::Inconclusive,
                    None => ProbeOutcome::Changed,
                },
                millis: started.elapsed().as_millis(),
            });
            if let Some(verdict) = verdict {
                report.findings.push(purity_finding(target, verdict));
            }
            std::fs::remove_dir_all(&root)
                .with_context(|| format!("removing probe dir {}", root.display()))?;
        }
    }
    // The probe supersedes the classifier lead for every hint it targeted.
    let probed: Vec<_> = targets
        .iter()
        .map(|target| (target.chunk.clone(), target.owner.clone()))
        .collect();
    report.findings.retain(|finding| {
        !matches!(
            (&finding.annotation, &finding.verdict),
            (
                LintedAnnotation::PurityHint { owner },
                InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::PurityClassification { .. }
                },
            ) if probed.contains(&(finding.chunk.clone(), owner.clone()))
        )
    });
    Ok(())
}

fn purity_finding(target: &PurityTarget, verdict: InferenceVerdict) -> LintFinding {
    LintFinding {
        rule: LintRule::InferableAnnotation,
        chunk: target.chunk.clone(),
        annotation: LintedAnnotation::PurityHint {
            owner: target.owner.clone(),
        },
        verdict,
    }
}

/// Every authored `purity: pure` annotation, in spec order.
fn purity_targets(spec: &TransformSpec) -> Vec<PurityTarget> {
    let modules = spec.logical_modules.iter().flat_map(|(chunk, modules)| {
        modules.iter().flat_map(move |(module, logical)| {
            logical
                .annotations
                .iter()
                .filter(|(_, annotation)| annotation.purity == MemberPurity::Pure)
                .map(move |(name, _)| PurityTarget {
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
            .map(move |(name, _)| PurityTarget {
                chunk: chunk.clone(),
                owner: PurityHintOwner::ChunkRename { name: name.clone() },
            })
    });
    modules.chain(renames).collect()
}

/// A copy of `spec` with `target`'s `purity: pure` reset to the default.
fn withhold_purity(spec: &TransformSpec, target: &PurityTarget) -> Result<TransformSpec> {
    let mut spec = spec.clone();
    let annotation = match &target.owner {
        PurityHintOwner::LogicalMember(member) => spec
            .logical_modules
            .get_mut(&target.chunk)
            .and_then(|modules| modules.get_mut(&member.module))
            .and_then(|module| module.annotations.get_mut(&member.name)),
        PurityHintOwner::ChunkRename { name } => spec
            .chunk_renames
            .get_mut(&target.chunk)
            .and_then(|renames| renames.annotations.get_mut(name)),
        PurityHintOwner::InputBinding { .. } => None,
    }
    .with_context(|| format!("purity hint {target:?} vanished from the spec"))?;
    annotation.purity = MemberPurity::default();
    Ok(spec)
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
            LintedAnnotation::CommutingStatements {
                spec_location,
                a,
                b,
            } => {
                format!("{spec_location}: endpoints {a} and {b}")
            }
        };
        let _ = writeln!(
            out,
            "{severity}[inferable_annotation]: chunk {}: {subject}: {verdict}",
            finding.chunk
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
                "probe: chunk {}: {:?}: {:?} in {} ms",
                probe.chunk, probe.annotation, probe.outcome, probe.millis
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
