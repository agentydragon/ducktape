//! Human diagnostics at command boundaries. Structured reports are independent.

use analysis::diagnostics::{Diagnostic, Diagnostics};

pub fn report(diagnostics: &Diagnostics) {
    for (chunk, diagnostics) in diagnostics.drain() {
        for diagnostic in diagnostics {
            match diagnostic {
                Diagnostic::DanglingExportContract { contract, export } => eprintln!(
                    "cross_module_purity: dangling {contract} assertion {chunk}:{export} — no such analyzed module export; ignoring"
                ),
                Diagnostic::OpaqueEntry { error } => eprintln!(
                    "cross_module_purity: chunk {chunk} entry failed to parse; leaving it opaque to the purity oracle: {error:#}"
                ),
                Diagnostic::Selector(outcome) => eprintln!("{}", outcome.render_line()),
                Diagnostic::UnusedCommutingStatements(unused) => {
                    eprintln!("warning: chunk {chunk}: {unused}")
                }
                Diagnostic::OwnershipSubset => eprintln!(
                    "Ownership subset: validating the selected assignment with full spec context; the canonical assignment is not being validated."
                ),
                Diagnostic::RedundantPurityHint(hint) => eprintln!(
                    "warning: chunk {chunk}: `purity: pure` hint on binding `{binding}` is redundant — \
                     the analyzer infers {reason} for this binding without this hint. {basis}",
                    binding = hint.binding_name,
                    reason = hint.reason.explanation(),
                    basis = hint.proof_basis.explanation(),
                ),
                Diagnostic::RedundantPureMemberHint(hint) => eprintln!(
                    "warning: chunk {chunk}: `pure_members: [{property}]` on binding `{binding}` \
                     is redundant — the analyzer infers {reason} without the hint. \
                     Remove the entry from the spec.",
                    binding = hint.binding_name,
                    property = hint.property,
                    reason = hint.reason.explanation(),
                ),
                Diagnostic::AdmissionOverride { shape, statements } => {
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
                    eprintln!(
                        "notice: chunk {chunk}: admission check {check} overridden by spec \
                        (chunk_analysis_options.{chunk}.admission_overrides): {noun} \
                        {statements}: {description}"
                    );
                }
                Diagnostic::RedundantAdmissionOverride(check) => eprintln!(
                    "warning: chunk {chunk}: admission override `{check}` is redundant — \
                     the chunk has no {check} violation and the override is a no-op. \
                     Remove it from chunk_analysis_options.{chunk}.admission_overrides."
                ),
            }
        }
    }
}

/// Give one command operation its diagnostic context and render it on either result.
pub fn run<T>(operation: impl FnOnce(&Diagnostics) -> anyhow::Result<T>) -> anyhow::Result<T> {
    let diagnostics = Diagnostics::default();
    let result = operation(&diagnostics);
    report(&diagnostics);
    result
}
