//! Shared outcome schema for the five spec-mutating CLI verbs
//! (`bindings {assign,unassign,rename}`, `modules {merge,delete}`).
//!
//! Every mutating verb prints one [`MutationOutcome`]-carrying object
//! on stdout when a JSON format is selected (explicit `--format
//! json|ndjson`, or stdout is a pipe): the shared core is
//! `verb` / `action` / `gate` / `files_written` / `files_deleted`;
//! verb-specific fields (`moves_applied`, `binding`, …) flatten in
//! alongside it. A refused verified write carries the canonical candidate
//! assessment that caused the refusal. Pipeline graph diagnostics retain
//! their report files and CLI queries. See docs/cli.md § "Rejection diagnostics".

use std::collections::BTreeMap;

use anyhow::Result;
use peel::OutputFormat;
use serde::Serialize;
use spec::LogicalModule;

use crate::candidate_assessment::{CandidateAssessmentRejection, candidate_write_is_accepted};

/// How a mutating verb's edit was validated, reported in the outcome
/// so machine readers can tell a gate-checked apply from a
/// `--no-verify` one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateOutcome {
    /// The canonical candidate assessment accepted the edit.
    Passed,
    /// Graph claims and requested selectors resolved; a full assessment is
    /// needed before a verified write.
    ClaimsChecked,
    /// Name-collision validation only (no graph-backed gate for this
    /// verb or edit).
    NamesOnly,
    /// Validation skipped via `--no-verify`.
    Skipped,
    /// No gate needed: the edit cannot change the assignment (e.g.
    /// deleting structurally empty modules, an empty batch).
    NotRequired,
}

impl GateOutcome {
    pub fn with_candidate_assessment(
        self,
        assessment: Option<&gate::ApplicationRelocationAssessmentReport>,
        candidate_modules: &BTreeMap<String, LogicalModule>,
    ) -> Self {
        if self == Self::ClaimsChecked && candidate_write_is_accepted(assessment, candidate_modules)
        {
            Self::Passed
        } else {
            self
        }
    }
}

/// The shared outcome core. Verb-specific outcome structs embed this
/// via `#[serde(flatten)]` so all five mutating verbs share one JSON
/// schema family.
#[derive(Debug, Clone, Serialize)]
pub struct MutationOutcome {
    /// Which mutating verb produced the outcome:
    /// `assign` | `unassign` | `rename` | `merge` | `delete` | `apply`.
    pub verb: &'static str,
    /// `applied` | `dry-run` | `noop` | `unchanged`.
    pub action: &'static str,
    pub gate: GateOutcome,
    /// A graph-only preview has checked claims but still needs the canonical
    /// full-context assessment before it can become a verified write.
    #[serde(skip_serializing_if = "is_false")]
    pub needs_full_assessment: bool,
    /// Files written (or, under `--dry-run`, that would be written).
    pub files_written: Vec<String>,
    /// Files deleted (or, under `--dry-run`, that would be deleted).
    pub files_deleted: Vec<String>,
}

fn is_false(value: &bool) -> bool {
    !*value
}

/// Shared stdout envelope for typed candidate and graph diagnostics.
#[derive(Debug, Serialize)]
pub struct GateRejectionOutcome<'a, T> {
    pub verb: &'static str,
    pub action: &'static str,
    pub rejection: &'a T,
}

/// Print `value` as JSON on stdout: pretty for `json`, one line for
/// `ndjson`. Callers handle `text` themselves.
pub fn print_outcome_json<T: Serialize>(value: &T, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Json => println!("{}", serde_json::to_string_pretty(value)?),
        OutputFormat::Ndjson => println!("{}", serde_json::to_string(value)?),
        OutputFormat::Text => unreachable!("text outcomes are rendered per verb"),
    }
    Ok(())
}

/// If `err` is a verified candidate-assessment rejection and JSON output is
/// selected, include the same canonical report that caused the refusal.
pub fn emit_gate_rejection_json(
    verb: &'static str,
    format: Option<OutputFormat>,
    err: &anyhow::Error,
) {
    let Some(rejection) = err.downcast_ref::<CandidateAssessmentRejection>() else {
        return;
    };
    let format = OutputFormat::resolve(format);
    if format != OutputFormat::Text {
        print_outcome_json(
            &GateRejectionOutcome {
                verb,
                action: "rejected",
                rejection: &rejection.report,
            },
            format,
        )
        .expect("candidate rejection serializes");
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use crate::candidate_assessment::ensure_candidate_write;

    #[test]
    fn public_report_status_cannot_authorize_or_label_a_verified_write() {
        // This is an API spoofing control: no source/output assessment ran,
        // even when a caller copies an accepted public diagnostic status.
        let mut report = gate::ApplicationRelocationAssessmentReport::from_chunks(
            &BTreeSet::new(),
            Vec::new(),
            None,
        )
        .unwrap();
        report.status = gate::CandidateRelocationStatus::AcceptedWithinCheckedScope;
        let copied_report = report.clone();
        let candidate = BTreeMap::new();
        assert_eq!(
            GateOutcome::ClaimsChecked.with_candidate_assessment(Some(&copied_report), &candidate),
            GateOutcome::ClaimsChecked,
        );
        assert!(ensure_candidate_write(Some(&copied_report), &candidate, false, false).is_err());
        assert!(ensure_candidate_write(Some(&copied_report), &candidate, true, false).is_ok());
        assert_eq!(
            GateOutcome::Skipped.with_candidate_assessment(Some(&copied_report), &candidate),
            GateOutcome::Skipped,
        );
    }
}
