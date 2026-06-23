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
//! - `source_match_native_diff_mismatch` — native AST lowering for a
//!   `source_match` selector resolved differently than the legacy
//!   `SourceMatchCandidate` oracle.
//!
//! The first three categories cover both member / binding-group `source_match`
//! selectors and `anonymous_statements[].match` selectors;
//! [`SelectorDiagnosticEntry::selector_kind`] distinguishes them
//! (`members.source_match` / `binding_groups.source_match` /
//! `anonymous_statements.source_match`).
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_match_native_diff: Option<SourceMatchNativeDiffReport>,
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceMatchNativeDiffReport {
    pub mismatch_kind: String,
    pub oracle: SourceMatchNativeDiffOracle,
    pub native: SourceMatchNativeDiffOutcome,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceMatchNativeDiffOracle {
    pub body_index: usize,
    pub statement_ordinal: usize,
    pub owner_id: Option<usize>,
    pub binding: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceMatchNativeDiffOutcome {
    pub kind: String,
    pub statement_ordinal: Option<usize>,
    pub owner_id: Option<usize>,
    pub binding: Option<String>,
    pub candidate_count: Option<usize>,
    pub message: Option<String>,
}
