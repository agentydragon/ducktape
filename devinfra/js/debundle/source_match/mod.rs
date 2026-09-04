//! Resolve readable source-pattern selectors against parsed JavaScript ASTs.
//!
//! `spec` owns the YAML-facing selector data. `js_ast` owns parsing and other
//! low-level AST helpers. This module is the bridge that interprets selector
//! semantics such as alpha-equivalent identifier matching.
//!
//! The crate is split by responsibility; submodules share the imports and
//! crate-internal items re-exported below via `pub(crate) use`, so each
//! submodule only needs `use super::*;`:
//!
//! - `identity` — selector identity keys and log-safe previews.
//! - `parse_validate` — selector/module parsing and capability validation.
//! - `types` — shared result/selector types.
//! - `binding_resolution` — canonical source-match claim expansion and
//!   declared-binding extraction.
//! - `declared_bindings` — declared-binding extraction from AST items.
//! - `datalog_resolver` — the legacy fact-based resolver retained as an oracle.
//! - `fact_near_miss` — fact-based `source_match` debt / near-miss diagnostics.
//! - `resolver` — the legacy resolver seam trait.
//! - `anonymous_statement` — anonymous source-match statement validation.
//! - `holes` — local hole-keyword dispatch over AST nodes.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use anyhow::{Context, Result, bail};
pub(crate) use serde::Serialize;
pub(crate) use spec::{
    AnonymousStatementSelector, BindingSourceKind, SourceMatch, SourceMatchClaim,
    SourceMatchIdentifierMode,
};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

// Syntactic-hole keyword vocabulary lives in `source_match_holes` so the
// fact matcher, the `selector_codemod` minimizer, and `selector_candidate_index`
// share one spelling. See that module's docs for the hole language.
pub(crate) use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
    STRING_LITERAL_REGEX_PREDICATE, hole_name_for, labeled_hole_name_for,
};

/// The fact matcher's identifier mode for a selector — the single
/// `SourceMatchIdentifierMode` → `selector_match::Mode` mapping shared by the
/// resolver (`datalog_resolver`) and the near-miss diagnostics (`fact_near_miss`).
/// The two enums mirror each other 1:1 but stay distinct so `selector_match`
/// (the matcher) needs no dependency on the `spec` authoring schema.
pub(crate) fn selector_mode(selector: &AnonymousStatementSelector) -> selector_match::Mode {
    match selector.identifiers {
        SourceMatchIdentifierMode::Exact => selector_match::Mode::Exact,
        SourceMatchIdentifierMode::AlphaAll => selector_match::Mode::AlphaAll,
    }
}

mod anonymous_statement;
mod binding_resolution;
mod datalog_resolver;
mod declared_bindings;
mod fact_near_miss;
mod holes;
mod identity;
mod parse_validate;
mod resolver;
mod types;

// Crate-internal re-exports: each submodule reaches its siblings' crate-internal
// items through `use super::*;`, which sees these globs.
pub(crate) use anonymous_statement::*;
pub(crate) use declared_bindings::*;
pub(crate) use holes::*;
pub(crate) use resolver::SelectorResolver;

/// Legacy procedural resolver API retained for parity tests and migration
/// oracles while production resolution moves through `selector_runtime`.
#[doc(hidden)]
pub mod legacy_resolver {
    pub use crate::datalog_resolver::ChunkResolver;
    pub use crate::resolver::SelectorResolver;
}

// Public API for selector parsing, normalization, and diagnostics.
pub use binding_resolution::{
    source_match_claim_member_selectors, source_match_declared_binding_names,
};
pub use fact_near_miss::fact_source_match_body_debt;
pub use identity::{selector_body_key, selector_key, source_match_preview};
pub use parse_validate::parse_selector_module_with_capability_check;
pub use types::{
    BindingGroupMemberSelector, MemberBindingGroupMatch, MemberBindingMatch,
    ParsedSourceMatchSelector, ResolvedMemberBinding, ResolvedMemberBindingGroup,
    SourceMatchBodyDebt, SourceMatchNearMiss,
};
