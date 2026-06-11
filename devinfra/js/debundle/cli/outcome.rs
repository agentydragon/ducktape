//! Shared outcome schema for the five spec-mutating CLI verbs
//! (`bindings {assign,unassign,rename}`, `modules {merge,delete}`).
//!
//! Every mutating verb prints one [`MutationOutcome`]-carrying object
//! on stdout when a JSON format is selected (explicit `--format
//! json|ndjson`, or stdout is a pipe): the shared core is
//! `verb` / `action` / `gate` / `files_written` / `files_deleted`;
//! verb-specific fields (`moves_applied`, `binding`, …) flatten in
//! alongside it. When the realizability gate refuses the edit, the
//! verbs instead print a [`GateRejectionOutcome`] (`action:
//! "rejected"`) carrying the canonical rejection projections — the
//! same `BlockingSccEntry` / `AtomicUnitConflictReport` wire shapes
//! `cycles.json` / `atomic_unit_conflicts.json` use — and exit
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
    /// The realizability + atom-split gate ran against the post-edit
    /// spec and passed.
    Passed,
    /// Name-collision validation only (no graph-backed gate for this
    /// verb or edit).
    NamesOnly,
    /// Validation skipped via `--no-verify`.
    Skipped,
    /// No gate needed: the edit cannot change the partition (e.g.
    /// deleting structurally empty modules, an empty batch).
    NotRequired,
}

/// The shared outcome core. Verb-specific outcome structs embed this
/// via `#[serde(flatten)]` so all five mutating verbs share one JSON
/// schema family.
#[derive(Debug, Clone, Serialize)]
pub struct MutationOutcome {
    /// Which mutating verb produced the outcome:
    /// `assign` | `unassign` | `rename` | `merge` | `delete`.
    pub verb: &'static str,
    /// `applied` | `dry-run` | `noop` | `unchanged`.
    pub action: &'static str,
    pub gate: GateOutcome,
    /// Files written (or, under `--dry-run`, that would be written).
    pub files_written: Vec<String>,
    /// Files deleted (or, under `--dry-run`, that would be deleted).
    pub files_deleted: Vec<String>,
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
