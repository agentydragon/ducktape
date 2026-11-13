//! Human diagnostics at command boundaries. Structured reports are independent.

use std::io::{self, Write};
use std::sync::Mutex;

use analysis::diagnostics::{ChunkDiagnostic, Diagnostics, InvocationDiagnostic, progress};
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

/// One decoder owns the flag for every phase and executable. Unknown nonempty
/// values enable progress, matching the historical staged-analysis convention.
fn progress_enabled(raw: Option<&std::ffi::OsStr>) -> bool {
    raw.is_some_and(|raw| {
        raw.to_str().is_none_or(|raw| {
            !matches!(
                raw.to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off" | "no"
            )
        })
    })
}

struct ProgressOutput<'a> {
    stderr: &'a mut (dyn Write + Send),
    error: Option<DiagnosticOutputError>,
}

fn emit_progress(output: &Mutex<ProgressOutput<'_>>, event: progress::Event<'_>) {
    let mut output = output.lock().expect("progress output lock poisoned");
    if output.error.is_some() {
        return;
    }
    output.error = write_diagnostic(
        output.stderr,
        format_args!(
            "[debundle progress] chunk={} phase={} state={}",
            event.chunk, event.phase, event.state
        ),
    )
    .err();
}

/// Give one command operation its diagnostic context and render it on either result.
/// Phase observation writes and flushes synchronously, retaining the first output
/// failure while analysis completes. The final result preserves both native causes.
pub fn run<T>(
    stderr: &mut (dyn Write + Send),
    operation: impl FnOnce(&Diagnostics) -> Result<T>,
) -> Result<T> {
    run_with_progress(
        stderr,
        progress_enabled(std::env::var_os("DUCKTAPE_DEBUNDLE_PROGRESS").as_deref()),
        operation,
    )
}

fn run_with_progress<T>(
    stderr: &mut (dyn Write + Send),
    progress: bool,
    operation: impl FnOnce(&Diagnostics) -> Result<T>,
) -> Result<T> {
    let output = Mutex::new(ProgressOutput {
        stderr,
        error: None,
    });
    let observer = |event| emit_progress(&output, event);
    let diagnostics = if progress {
        Diagnostics::with_progress(&observer)
    } else {
        Diagnostics::default()
    };
    let result = operation(&diagnostics);
    let mut output = output.lock().expect("progress output lock poisoned");
    let emission = match output.error.take() {
        Some(error) => Err(error),
        None => report(output.stderr, &diagnostics),
    };
    finish(result, emission)
}

#[cfg(test)]
mod tests {
    use super::*;
    use progress::{Phase, State};

    #[test]
    fn one_decoder_disables_all_historical_false_values() {
        assert!(!progress_enabled(None));
        for raw in ["", "0", "false", "FALSE", "off", "Off", "no", "NO"] {
            assert!(!progress_enabled(Some(raw.as_ref())), "{raw:?}");
        }
        for raw in ["1", "true", "yes", "on", "custom"] {
            assert!(progress_enabled(Some(raw.as_ref())), "{raw:?}");
        }
    }

    #[test]
    fn disabled_progress_is_silent_and_parallel_progress_preserves_lines_and_order() {
        for enabled in [false, true] {
            let mut bytes = Vec::new();
            run_with_progress(&mut bytes, enabled, |diagnostics| {
                std::thread::scope(|scope| {
                    for chunk in ["a", "b", "c"] {
                        scope.spawn(move || {
                            diagnostics.progress(chunk, Phase::ComputeChunkAnalysis, State::Start);
                            diagnostics.progress(chunk, Phase::ComputeChunkAnalysis, State::End);
                        });
                    }
                });
                Ok(())
            })
            .unwrap();
            let text = String::from_utf8(bytes).unwrap();
            if !enabled {
                assert!(text.is_empty());
                continue;
            }
            assert_eq!(text.lines().count(), 6);
            for chunk in ["a", "b", "c"] {
                let lines: Vec<_> = text
                    .lines()
                    .filter(|line| line.contains(&format!("chunk={chunk} ")))
                    .collect();
                assert_eq!(
                    lines,
                    [
                        format!(
                            "[debundle progress] chunk={chunk} phase=compute_chunk_analysis state=start"
                        ),
                        format!(
                            "[debundle progress] chunk={chunk} phase=compute_chunk_analysis state=end"
                        ),
                    ]
                );
            }
        }
    }

    struct FailingOutput {
        flush: bool,
        writes: usize,
        flushes: usize,
    }

    impl Write for FailingOutput {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.writes += 1;
            if self.flush {
                Ok(bytes.len())
            } else {
                // A forbidden later attempt would return a different native cause.
                Err(io::Error::from_raw_os_error(if self.writes == 1 {
                    32
                } else {
                    5
                }))
            }
        }

        fn flush(&mut self) -> io::Result<()> {
            self.flushes += 1;
            Err(io::Error::from_raw_os_error(if self.flushes == 1 {
                28
            } else {
                5
            }))
        }
    }

    #[derive(Debug, thiserror::Error)]
    #[error("operation failed")]
    struct OperationFailure;

    #[test]
    fn first_live_write_or_flush_failure_survives_success_or_operation_failure() {
        for flush in [false, true] {
            for failed_operation in [false, true] {
                let mut output = FailingOutput {
                    flush,
                    writes: 0,
                    flushes: 0,
                };
                let error = run_with_progress(&mut output, true, |diagnostics| {
                    diagnostics.progress("a", Phase::ComputeChunkAnalysis, State::Start);
                    diagnostics.progress("a", Phase::ComputeChunkAnalysis, State::End);
                    diagnostics.record_invocation(InvocationDiagnostic::OwnershipSubset);
                    if failed_operation {
                        Err(OperationFailure.into())
                    } else {
                        Ok(())
                    }
                })
                .unwrap_err();
                assert_eq!(
                    error.downcast_ref::<OperationFailure>().is_some(),
                    failed_operation
                );
                let native = match error.downcast_ref::<DiagnosticOutputError>().unwrap() {
                    DiagnosticOutputError::Write(error) => {
                        assert!(!flush);
                        error
                    }
                    DiagnosticOutputError::Flush(error) => {
                        assert!(flush);
                        error
                    }
                };
                assert_eq!(native.raw_os_error(), Some(if flush { 28 } else { 32 }));
                assert_eq!(output.flushes, usize::from(flush));
                if !flush {
                    assert_eq!(output.writes, 1);
                }
            }
        }
    }
}
