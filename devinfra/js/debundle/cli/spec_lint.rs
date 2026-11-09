//! `debundle spec lint` — every author annotation the engine may already
//! infer, for one exact source/spec pair (SPEC.md § Spec lint).
//!
//! It runs the `spec validate` pass (the ordinary dry-run pipeline, with the
//! shared resolver and gate) and reads back each chunk's `spec_lint.json`.
//! Findings are warnings and suggestions only: the command never edits the
//! spec and exits 0 whenever the pass produced reports.

use std::fmt::Write as _;

use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintedAnnotation,
    PurityHintOwner,
};
use anyhow::{Context, Result};
use clap::Args as ClapArgs;
use output_layout::SPEC_LINT_REPORT;
use peel::{OutputFormat, print_report};
use serde::Serialize;
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
}

#[derive(Debug, Serialize)]
struct SpecLintReport {
    findings: Vec<LintFinding>,
    /// Chunks the pass stopped before linting: their annotations are unchecked,
    /// not clean.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    unlinted_chunks: Vec<String>,
}

pub fn run(args: SpecLintArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    let pass = run_captured_pass(args.transform)?;
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
    let report = SpecLintReport {
        findings: reports
            .into_iter()
            .flat_map(|(_, findings)| findings)
            .collect(),
        unlinted_chunks,
    };
    print_report(&report, format, render_text).context("writing spec lint output")
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
                    }
                ),
            ),
            InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::PurityClassification { .. },
            } => (
                "note",
                "inconclusive (the binding classifies without the hint; other consumers of \
                 the hint were not probed)"
                    .to_owned(),
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
    let _ = writeln!(out, "{} finding(s)", report.findings.len());
}
