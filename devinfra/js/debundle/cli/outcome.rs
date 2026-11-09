//! Shared outcome schema for the five spec-mutating CLI verbs
//! (`bindings {assign,unassign,rename}`, `modules {merge,delete}`).
//!
//! Every mutating verb prints one [`MutationOutcome`]-carrying object
//! on stdout when a JSON format is selected (explicit `--format
//! json|ndjson`, or stdout is a pipe): the shared core is
//! `verb` / `action` / `gate` / `needs_full_assessment` (ownership edits
//! only) / `files_written` / `files_deleted`;
//! verb-specific fields (`moves_applied`, `binding`, …) flatten in
//! alongside it. When the realizability gate refuses the edit, the
//! verbs instead print a [`GateRejectionOutcome`] (`action:
//! "rejected"`) carrying the canonical rejection projections — the
//! same `BlockingSccEntry` / `AtomicGroupConflictReport` wire shapes
//! `cycles.json` / `atomic_group_conflicts.json` use — and exit
//! non-zero. See docs/cli.md § "Rejection diagnostics".

use anyhow::Result;
use peel::OutputFormat;
use serde::Serialize;

use crate::edit_gate::{GateRejection, GateRejectionReport};

/// How a mutating verb's edit was validated, reported in the outcome
/// so machine readers can tell a gate-checked apply from a
/// `--no-verify` one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateOutcome {
    /// The graph-backed claim checks and legacy realizability + atom-split
    /// gate passed, and the full candidate assessment (`--analysis`) found
    /// the candidate placement mappable for canonical emission.
    Passed,
    /// The graph-backed claim checks and legacy realizability + atom-split
    /// gate passed; the full candidate was not assessed (or its assessment
    /// did not accept the placement).
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
    /// Label for an ownership edit whose graph checks produced `self`:
    /// `ClaimsChecked` becomes `Passed` only when the full candidate
    /// assessment ran and its placement is mappable for canonical emission.
    pub fn with_candidate_assessment(
        self,
        assessment: Option<&gate::CandidateRelocationAssessmentReport>,
    ) -> Self {
        match assessment {
            Some(assessment)
                if self == Self::ClaimsChecked && assessment.placement.emission_mappable =>
            {
                Self::Passed
            }
            _ => self,
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
    /// The edit passed only graph-backed claim checks; no full candidate
    /// assessment (`--analysis`) ran. Such writes are still permitted while
    /// the legacy gate is authoritative.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub needs_full_assessment: bool,
    /// Files written (or, under `--dry-run`, that would be written).
    pub files_written: Vec<String>,
    /// Files deleted (or, under `--dry-run`, that would be deleted).
    pub files_deleted: Vec<String>,
}

impl MutationOutcome {
    /// The shared core for an ownership edit (`assign`, `unassign`, `apply`,
    /// `refine`, `merge`, `delete`). `checks` is the label the graph-backed
    /// checks produced; `assessment` is the full candidate assessment, when
    /// `--analysis` supplied one.
    pub fn ownership_edit(
        verb: &'static str,
        action: &'static str,
        checks: GateOutcome,
        assessment: Option<&gate::CandidateRelocationAssessmentReport>,
        files_written: Vec<String>,
        files_deleted: Vec<String>,
    ) -> Self {
        Self {
            verb,
            action,
            gate: checks.with_candidate_assessment(assessment),
            needs_full_assessment: checks == GateOutcome::ClaimsChecked && assessment.is_none(),
            files_written,
            files_deleted,
        }
    }

    /// Warn on stderr when this outcome wrote ownership changes that only
    /// graph-backed claim checks covered.
    pub fn warn_if_unassessed_write(&self) {
        if self.needs_full_assessment && self.action == "applied" {
            eprintln!(
                "warning: {}: write passed graph claim checks but was not assessed against the full candidate (needs_full_assessment; preview with --dry-run --analysis)",
                self.verb
            );
        }
    }
}

/// Stdout envelope for a realizability-gate rejection. `action:
/// "rejected"` keeps it in the same schema family as the success
/// outcomes; `rejection` carries the canonical per-SCC / per-conflict
/// projections.
#[derive(Debug, Serialize)]
pub struct GateRejectionOutcome<'a> {
    pub verb: &'static str,
    pub action: &'static str,
    pub rejection: &'a GateRejectionReport,
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

/// If `err` is a realizability-gate rejection and a JSON format is
/// selected (explicitly or because stdout is a pipe), mirror the
/// stderr blame report as a structured [`GateRejectionOutcome`] on
/// stdout so machine readers don't have to scrape prose. The error
/// still propagates — the command exits non-zero either way.
pub fn emit_gate_rejection_json(
    verb: &'static str,
    format: Option<OutputFormat>,
    err: &anyhow::Error,
) {
    let Some(rejection) = err.downcast_ref::<GateRejection>() else {
        return;
    };
    let format = OutputFormat::resolve(format);
    if format == OutputFormat::Text {
        return;
    }
    let outcome = GateRejectionOutcome {
        verb,
        action: "rejected",
        rejection: &rejection.report,
    };
    print_outcome_json(&outcome, format).expect("rejection outcome serializes");
}
