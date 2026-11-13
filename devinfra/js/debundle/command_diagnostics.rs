//! Human diagnostics at command boundaries. Structured reports are independent.

use std::io::{self, Write};

use analysis::diagnostics::{ChunkDiagnostic, Diagnostics, InvocationDiagnostic};
use anyhow::Result;

pub fn report(
    stderr: &mut dyn Write,
    diagnostics: &Diagnostics,
) -> std::result::Result<(), DiagnosticOutputError> {
    let collected = diagnostics.drain();
    for diagnostic in collected.invocation {
        match diagnostic {
            InvocationDiagnostic::OwnershipSubset => write_diagnostic(
                stderr,
                format_args!(
                    "Ownership subset: validating the selected assignment with full spec context; the canonical assignment is not being validated."
                ),
            )?,
        }
    }
    for (chunk, diagnostics) in collected.chunks {
        for diagnostic in diagnostics {
            match diagnostic {
                ChunkDiagnostic::DanglingExportContract { contract, export } => write_diagnostic(
                    stderr,
                    format_args!(
                        "cross_module_purity: dangling {contract} assertion {chunk}:{export} — no such analyzed module export; ignoring"
                    ),
                )?,
                ChunkDiagnostic::OpaqueEntry { error } => write_diagnostic(
                    stderr,
                    format_args!(
                        "cross_module_purity: chunk {chunk} entry failed to parse; leaving it opaque to the purity oracle: {error:#}"
                    ),
                )?,
                ChunkDiagnostic::Selector(outcome) => {
                    write_diagnostic(stderr, format_args!("{}", outcome.render_line()))?
                }
                ChunkDiagnostic::UnusedCommutingStatements(unused) => {
                    write_diagnostic(stderr, format_args!("warning: chunk {chunk}: {unused}"))?
                }
                ChunkDiagnostic::RedundantPurityHint(hint) => write_diagnostic(
                    stderr,
                    format_args!(
                        "warning: chunk {chunk}: `purity: pure` hint on binding `{binding}` is redundant — \
                     the analyzer infers {reason} for this binding without this hint. {basis}",
                        binding = hint.binding_name,
                        reason = hint.reason.explanation(),
                        basis = hint.proof_basis.explanation(),
                    ),
                )?,
                ChunkDiagnostic::RedundantPureMemberHint(hint) => write_diagnostic(
                    stderr,
                    format_args!(
                        "warning: chunk {chunk}: `pure_members: [{property}]` on binding `{binding}` \
                     is redundant — the analyzer infers {reason} without the hint. \
                     Remove the entry from the spec.",
                        binding = hint.binding_name,
                        property = hint.property,
                        reason = hint.reason.explanation(),
                    ),
                )?,
                ChunkDiagnostic::PurityCompletionDisagreement {
                    ordinal,
                    source_location,
                    evidence,
                } => write_diagnostic(
                    stderr,
                    format_args!(
                        "warning: chunk {chunk}: synchronous purity completion disagreed at \
                         statement #{ordinal} ({location}): prepared {prepared}, completed {completed}",
                        location = source_location.as_ref().map_or_else(
                            || "source location unavailable".to_owned(),
                            render_source_location,
                        ),
                        prepared = render_purity(&evidence.prepared),
                        completed = render_purity(&evidence.completed),
                    ),
                )?,
                ChunkDiagnostic::AdmissionOverride { shape, statements } => {
                    let check = shape.check();
                    let description = shape.explanation();
                    let noun = if statements.len() == 1 {
                        "statement"
                    } else {
                        "statements"
                    };
                    let statements = statements
                        .iter()
                        .map(|ordinal| format!("#{ordinal}"))
                        .collect::<Vec<_>>()
                        .join(", ");
                    write_diagnostic(
                        stderr,
                        format_args!(
                            "notice: chunk {chunk}: admission check {check} overridden by spec \
                        (chunk_analysis_options.{chunk}.admission_overrides): {noun} \
                        {statements}: {description}"
                        ),
                    )?;
                }
                ChunkDiagnostic::RedundantAdmissionOverride(check) => write_diagnostic(
                    stderr,
                    format_args!(
                        "warning: chunk {chunk}: admission override `{check}` is redundant — \
                     the chunk has no {check} violation and the override is a no-op. \
                     Remove it from chunk_analysis_options.{chunk}.admission_overrides."
                    ),
                )?,
            }
        }
    }
    Ok(())
}

fn render_source_location(location: &analysis::SourceLocation) -> String {
    match location.start_column {
        Some(column) => format!("{}:{}:{column}", location.source_path, location.start_line),
        None => format!("{}:{}", location.source_path, location.start_line),
    }
}

fn render_purity(purity: &analysis::Purity) -> String {
    match purity {
        analysis::Purity::Pure { .. } => "pure".to_owned(),
        analysis::Purity::NotPure { reasons } if reasons.is_empty() => "not pure".to_owned(),
        analysis::Purity::NotPure { reasons } => format!(
            "not pure ({})",
            reasons
                .iter()
                .map(|reason| {
                    let location = reason
                        .source_location
                        .as_ref()
                        .map(|location| format!(" at {}", render_source_location(location)))
                        .unwrap_or_default();
                    let detail = reason
                        .detail
                        .as_ref()
                        .map(|detail| format!(": {detail}"))
                        .unwrap_or_default();
                    format!("{}{location}{detail}", reason.rule)
                })
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// A stderr failure retains its native I/O cause and is never a successful closed stdout.
#[derive(Debug, thiserror::Error)]
pub enum DiagnosticOutputError {
    #[error("writing command diagnostics to stderr: {0}")]
    Write(#[source] io::Error),
    #[error("flushing command diagnostics to stderr: {0}")]
    Flush(#[source] io::Error),
}

pub fn write_diagnostic(
    stderr: &mut dyn Write,
    message: std::fmt::Arguments<'_>,
) -> std::result::Result<(), DiagnosticOutputError> {
    writeln!(stderr, "{message}").map_err(DiagnosticOutputError::Write)?;
    stderr.flush().map_err(DiagnosticOutputError::Flush)?;
    Ok(())
}

/// Preserve the operation's cause when its diagnostic also fails to reach the reader.
pub fn finish<T>(
    operation: Result<T>,
    emission: std::result::Result<(), DiagnosticOutputError>,
) -> Result<T> {
    match (operation, emission) {
        (result, Ok(())) => result,
        (Ok(_), Err(error)) => Err(error.into()),
        (Err(operation), Err(emission)) => {
            // Context keeps the original operation available to anyhow downcasts.
            Err(operation.context(emission))
        }
    }
}

/// Give one command operation its diagnostic context and render it on either result.
pub fn run<T>(
    stderr: &mut dyn Write,
    operation: impl FnOnce(&Diagnostics) -> Result<T>,
) -> Result<T> {
    let diagnostics = Diagnostics::default();
    let result = operation(&diagnostics);
    finish(result, report(stderr, &diagnostics))
}
