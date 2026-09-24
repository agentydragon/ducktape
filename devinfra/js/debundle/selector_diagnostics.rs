//! Machine-readable keep-going selector diagnostics.
//!
//! The materialize keep-going pass classifies every selector problem it
//! finds while building a chunk plan and emits a per-chunk
//! [`SelectorDiagnosticsReport`] instead of stopping at the first failing
//! selector. These types are the stable, debundler-owned JSON contract for
//! that report.
//!
//! Two consumers share these types so the on-disk shape can never drift
//! between writer and reader:
//!
//! - the producer ([`crate::lowering::materialize::plan_builder`]) builds a
//!   report from its internal diagnostic state and serializes it to
//!   `selector_diagnostics.json` per chunk;
//! - the `debundle spec validate` CLI verb
//!   ([`crate::cli::validate`]) runs the keep-going dry-run pass, reads those
//!   per-chunk reports back, and re-emits a combined report on stdout in the
//!   shared `--format text|json|ndjson` convention.
//!
//! Failure taxonomy ([`SelectorDiagnosticEntry::category`]):
//!
//! - `unresolved_selector` — a source-backed binding selector or anonymous
//!   statement selector matched zero top-level candidates (no-match);
//! - `ambiguous_selector` — the active resolver reported more than one valid
//!   selector assignment;
//! - `selector_resolution_error` — the selector failed to resolve for a
//!   reason other than a classified no-match / ambiguity (including native
//!   solver no-assignment diagnostics, parse / schema / unsupported hole);
//! - `conflicting_selector` — the selector is in an unsatisfiable core of the
//!   joint solve: it cannot resolve together with the selectors its message
//!   names (the named set need not be minimal). Selectors outside every core
//!   still resolve;
//! - `too_broad_selector` — the shape matcher placed the selector at more than
//!   `MAX_CANDIDATES_PER_SELECTOR` places; it is rejected without a solve;
//! - `duplicate_claim` — two selectors resolved to the same declaration
//!   identity in the same chunk;
//! - `resolved_by_elimination` ([`Severity::Warning`]) — the selector resolved,
//!   but only because other selectors' solved claims took its alternatives.
//!   The message names those claimers. It does not fail the run.
//!
//! Every other category is [`Severity::Error`]. All but `duplicate_claim`
//! cover both source-backed binding selectors and `anonymous_statements[].match`
//! selectors;
//! [`SelectorDiagnosticEntry::selector_kind`] distinguishes them
//! (`source_matches` / `members.source_match` /
//! `anonymous_statements.source_match`). `members.source_match` appears only for
//! internal lowered selector forms; public YAML binding claims use
//! `source_matches[]`.
//!
//! Not yet classified here: name-pin debt annotated with `note:` (surfacing it
//! as structured entries needs `note:` plumbed through `MemberRequest`), and the
//! free-readable-identifier class (`TODO.md` P1.5): `alpha_all` readable names
//! that are free references rather than local binders.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Per-chunk keep-going selector diagnostics. The producer writes `None`
/// when a chunk has no selector problems, so a present report always
/// carries at least one diagnostic.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelectorDiagnosticsReport {
    pub chunk_id: String,
    /// Failure-class histogram keyed by [`SelectorDiagnosticEntry::category`].
    pub counts: BTreeMap<String, usize>,
    pub diagnostics: Vec<SelectorDiagnosticEntry>,
    /// Known gaps in the taxonomy (classes not yet emitted as structured
    /// entries), carried so a coordinator sees what the report does *not*
    /// cover.
    pub coverage_notes: Vec<String>,
}

/// One classified selector failure with enough source identity to feed a
/// later repair flow.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelectorDiagnosticEntry {
    pub category: String,
    pub severity: Severity,
    pub module_id: String,
    pub module_path: Option<String>,
    pub export_name: Option<String>,
    pub selector_kind: String,
    pub target_binding: Option<String>,
    pub claim_origin: Option<String>,
    pub body_indices: Vec<usize>,
    pub first_mismatch: Option<String>,
    pub source_match_preview: Option<String>,
    pub source_match_hash: Option<String>,
    pub source_match_body_hash: Option<String>,
    pub duplicate_claim: Option<DuplicateClaimReport>,
    pub message: String,
    pub recommended_next_action: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    /// The selector did not resolve; the run fails.
    Error,
    /// The selector resolved; the entry flags fragility and the run continues.
    Warning,
}

/// Two selectors resolving to the same declaration identity in one chunk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DuplicateClaimReport {
    pub chunk_id: String,
    pub binding: String,
    pub existing: DuplicateClaimSiteReport,
    pub duplicate: DuplicateClaimSiteReport,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DuplicateClaimSiteReport {
    pub module_id: String,
    pub export_name: Option<String>,
    pub claim_origin: Option<String>,
}
