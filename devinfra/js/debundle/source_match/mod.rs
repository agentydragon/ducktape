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
//! - `timing` — selector identity keys, preview, and timing diagnostics.
//! - `parse_validate` — selector/module parsing and capability validation.
//! - `types` — shared result/selector types and the body-index filter.
//! - `binding_resolution` — member-binding candidate and group resolution.
//! - `target_matching` — locating matching target bindings/declarators.
//! - `string_literal_predicate` — the `STR_LITERAL_MATCHING_RE` predicate.
//! - `declared_bindings` — declared-binding extraction from AST items.
//! - `body_search` — running the matcher across a module body.
//! - `hints` — no-match diagnostics and mismatch-reason rendering.
//! - `anonymous_statement` — anonymous target-statement index resolution.
//! - `prepared_needle` — `PreparedNeedle` and its prefilters.
//! - `matcher`/`matcher_nodes` — the `AstWildcardMatcher` engine.
//! - `alpha_canonicalize` — alpha-equivalent identifier canonicalization.
//! - `wildcard_idents` — collecting wildcard identifier names.
//! - `holes` — local hole-keyword dispatch over AST nodes.

pub(crate) use std::collections::{BTreeMap, BTreeSet};
pub(crate) use std::sync::OnceLock;
pub(crate) use std::time::{Duration, Instant};

pub(crate) use anyhow::{Context, Result, bail};
pub(crate) use regex::Regex;
pub(crate) use spec::{
    AnonymousStatementSelector, BindingGroup, BindingGroupAdoptNames, BindingSourceKind,
    SourceMatch, SourceMatchIdentifierMode, TargetStatements, TargetStatementsAll,
};
pub(crate) use swc_atoms::{Atom, Wtf8Atom};
pub(crate) use swc_common::{EqIgnoreSpan, SyntaxContext};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

// Syntactic-hole keyword vocabulary lives in `source_match_holes` so the
// matcher, the `selector_codemod` minimizer, and `selector_candidate_index`
// share one spelling. See that module's docs for the hole language.
pub(crate) use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE, hole_name_for,
};

mod alpha_canonicalize;
mod anonymous_statement;
mod binding_resolution;
mod body_search;
mod datalog_resolver;
mod declared_bindings;
mod hints;
mod holes;
mod matcher;
mod parse_validate;
mod prepared_needle;
mod resolver;
mod string_literal_predicate;
mod target_matching;
mod timing;
mod types;
mod wildcard_idents;

// Crate-internal re-exports: each submodule reaches its siblings' crate-internal
// items through `use super::*;`, which sees these globs.
pub(crate) use alpha_canonicalize::*;
pub(crate) use anonymous_statement::*;
pub(crate) use body_search::*;
pub(crate) use declared_bindings::*;
pub(crate) use hints::*;
pub(crate) use holes::*;
pub(crate) use matcher::*;
pub(crate) use parse_validate::*;
pub(crate) use prepared_needle::*;
pub(crate) use string_literal_predicate::*;
pub(crate) use target_matching::*;
pub(crate) use timing::*;
pub(crate) use wildcard_idents::*;

// Public API: importable at `source_match::<item>` exactly as before the split.
pub use binding_resolution::{
    binding_group_anonymous_statement_selector, binding_group_member_selectors,
    find_anonymous_statement_body_index_groups, find_anonymous_statement_body_indices,
    member_binding_candidate_matches, member_binding_candidate_matches_within,
    member_binding_candidates, resolve_anonymous_statement_body_index,
    resolve_anonymous_statement_body_indices, resolve_member_binding, resolve_member_binding_group,
    resolve_member_binding_group_match, source_match_body_debt,
    source_match_declared_binding_names,
};
pub use datalog_resolver::ChunkResolver;
pub use resolver::{
    AstWildcardResolver, DifferentialResolver, DisagreementSink, ResolverDisagreement,
    ResolverOutcome, ResolverSite, SelectorResolver, needle_matches,
};
pub use timing::{selector_body_key, selector_key, source_match_preview};
pub use types::{
    BindingGroupMemberSelector, BodyIndexFilter, MemberBindingMatch, ResolvedMemberBinding,
    ResolvedMemberBindingGroup, SourceMatchBodyDebt, SourceMatchNearMiss,
};

#[cfg(test)]
mod tests;
