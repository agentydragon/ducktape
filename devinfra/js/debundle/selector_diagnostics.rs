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
//! - the `debundle spec validate --keep-going` CLI verb
//!   ([`crate::cli::validate`]) runs the keep-going dry-run pass, reads those
//!   per-chunk reports back, and re-emits a combined report on stdout in the
//!   shared `--format text|json|ndjson` convention.
//!
//! Failure taxonomy ([`SelectorDiagnosticEntry::category`]):
//!
//! - `unresolved_selector` — a `source_match` selector matched zero
//!   top-level candidates (no-match);
//! - `ambiguous_selector` — a `source_match` selector matched more than one
//!   candidate without a differentiating `target_binding`;
//! - `selector_resolution_error` — the selector failed to resolve for a
//!   reason other than no-match / ambiguity (parse / schema / unsupported
//!   hole);
//! - `duplicate_claim` — two selectors resolved to the same declaration
//!   identity in the same chunk.
//!
//! Not yet classified here (deferred to the free-readable-identifier work in
//! `TODO.md` P1.5): `alpha_all` readable names that are free references
//! rather than local binders.

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
    pub module_id: String,
    pub module_path: Option<String>,
    pub export_name: Option<String>,
    pub selector_kind: String,
    pub target_binding: Option<String>,
    pub claim_origin: Option<String>,
    pub body_indices: Vec<usize>,
    pub first_mismatch: Option<String>,
    pub nearest_candidates: Vec<SelectorNearestCandidate>,
    pub source_match_preview: Option<String>,
    pub source_match_hash: Option<String>,
    pub source_match_body_hash: Option<String>,
    pub duplicate_claim: Option<DuplicateClaimReport>,
    pub message: String,
    pub recommended_next_action: String,
}

/// A near-miss top-level statement scored against an unresolved selector,
/// cheapest-distance first.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelectorNearestCandidate {
    pub body_index: usize,
    pub declared_bindings: Vec<String>,
    pub score: usize,
    pub first_mismatch: String,
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
