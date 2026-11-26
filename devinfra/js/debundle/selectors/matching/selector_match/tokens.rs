//! Invariant and identifier tokens for candidate pruning.

use std::collections::HashSet;

use chunk_facts::{NodeId, NodeKind};
use source_match_holes::is_hole_keyword;

use crate::Mode;
use crate::index::Index;
use crate::predicates::regex_predicate_pattern;
use crate::validate::consumed_nodes;

/// An invariant token — a label `homo` always compares **exactly**, even in
/// alpha mode (string/number literals, member/property names, regex literals).
/// Any structural match must carry every invariant token the needle pins, so a
/// token → containing-statements index lets a selector skip straight to the
/// candidates that share its rarest token instead of scanning the whole chunk.
/// The `var`/`let`/`const` keyword is invariant but too common to discriminate.
///
/// [`Ident`](Token::Ident) is the **exact-mode-only** discriminator: an identifier
/// spelling is alpha-renamable (so *not* invariant) under [`Mode::AlphaAll`], but
/// under [`Mode::Exact`] `homo` compares it byte-for-byte, so a needle identifier
/// pins it — but **only** when the match runs without a `target_binding` prebind
/// (a prebind alpha-couples one needle name to a candidate's, so that name's
/// spelling is no longer required). Subjects are indexed with their identifiers so
/// an exact-mode needle can require them; an alpha-mode (or prebind) query simply
/// pins no `Ident`, leaving the existing behavior unchanged.
#[derive(Clone, PartialEq, Eq, Hash)]
pub enum Token {
    Str(Box<str>),
    Num(Box<str>),
    Prop(Box<str>),
    Regex(Box<str>, Box<str>),
    Ident(Box<str>),
}

/// The deduplicated invariant tokens (see [`Token`]) a statement's index carries.
/// For a body item these are indexed for candidate lookup; for a needle they are
/// the tokens any match must also carry. A `STR_LITERAL_MATCHING_RE(...)`
/// predicate's pattern argument is excluded — it matches varying string values
/// by regex, not by literal equality, so it pins no specific token.
pub fn invariant_tokens(index: &Index) -> Vec<Token> {
    let node_count = index.kind.len() as NodeId;
    let predicate_args: HashSet<NodeId> = (0..node_count)
        .filter(|&node| regex_predicate_pattern(index, node).is_some())
        .filter_map(|node| index.children_of(node).get(1).copied())
        .collect();
    let mut tokens: HashSet<Token> = HashSet::new();
    for node in 0..node_count {
        if !predicate_args.contains(&node)
            && let Some(value) = index.str_lit_of(node)
        {
            tokens.insert(Token::Str(value.into()));
        }
        if let Some(value) = index.num_lit_of(node) {
            tokens.insert(Token::Num(value.into()));
        }
        if let Some(name) = index.prop_name_of(node)
            && !is_hole_keyword(name)
        {
            tokens.insert(Token::Prop(name.into()));
        }
        // A shorthand same-name property (`{ k }` / `const { k } = …`) carries its
        // key only as `ident_name`, but the key is a stable property name — the
        // same `Prop(k)` token its explicit form (`{ k: k }`) pins. Index it so the
        // shorthand⟷explicit equivalence in [`homo`] is not pre-filtered away (a
        // needle pinning `Prop(k)` must still reach a shorthand candidate, and
        // vice versa). The value identifier stays renamable (not a token).
        if matches!(
            index.kind_of(node),
            NodeKind::Shorthand | NodeKind::PatAssign
        ) && index.children_of(node).is_empty()
            && let Some(name) = index.ident_of(node)
            && !is_hole_keyword(name)
        {
            tokens.insert(Token::Prop(name.into()));
        }
        if let Some((pattern, flags)) = index.regex_of(node) {
            tokens.insert(Token::Regex(pattern.into(), flags.into()));
        }
    }
    tokens.into_iter().collect()
}

/// The tokens to index a **subject** (chunk body item / declarator) under: every
/// [`invariant_tokens`] token plus an [`Token::Ident`] for each identifier spelling
/// the subject carries. The identifiers make the postings index answer
/// "which subjects contain identifier `X`" so an exact-mode needle that pins `X`
/// can prune to them; an alpha-mode needle simply never queries an `Ident` token,
/// so its candidate set is unchanged. Real subject source carries no hole keywords,
/// but they are excluded defensively to keep the index identical in spirit to the
/// needle side.
pub fn subject_tokens(index: &Index) -> Vec<Token> {
    let mut tokens = invariant_tokens(index);
    let mut seen: HashSet<Box<str>> = HashSet::new();
    for node in 0..index.kind.len() as NodeId {
        if let Some(name) = index.ident_of(node)
            && !is_hole_keyword(name)
            && seen.insert(name.into())
        {
            tokens.push(Token::Ident(name.into()));
        }
    }
    tokens
}

/// The tokens a needle requires of any matching subject, for candidate pruning.
///
/// Always includes [`invariant_tokens`] (literals / property names / regex — these
/// `homo` compares exactly in every mode). Under [`Mode::Exact`] **and** when the
/// match runs without a `target_binding` prebind (`allow_exact_ident`), it also
/// adds an [`Token::Ident`] for every identifier the matcher provably compares
/// byte-for-byte: every `ident_name` node that is neither a hole keyword nor
/// absorbed by a run-hole carrier / predicate (the [`consumed_nodes`] set, the same
/// nodes [`homo`] never visits as a node-for-node identifier comparison).
///
/// **Soundness** (the candidate set must never miss a real match): in exact mode
/// without a prebind, a non-consumed needle identifier `X` is matched via
/// [`Bindings::resolve_unbound`], which under `Exact` requires `needle == subject`
/// — so any matching subject carries identifier `X`, hence appears in the `Ident(X)`
/// postings. Excluding the consumed nodes is essential: a `DECLARATORS`/`EXPR`/
/// `ANYTHING` hole or a `STR_LITERAL_MATCHING_RE` callee imposes no spelling on the
/// subject. A `target_binding` prebind alpha-couples one needle name to a
/// candidate's, so its spelling is not required either — callers on a prebind path
/// pass `allow_exact_ident = false` and fall back to invariant-only tokens.
pub fn needle_required_tokens(index: &Index, mode: Mode, allow_exact_ident: bool) -> Vec<Token> {
    let mut tokens = invariant_tokens(index);
    if mode == Mode::Exact && allow_exact_ident {
        let consumed = consumed_nodes(index);
        let mut seen: HashSet<Box<str>> = HashSet::new();
        for node in 0..index.kind.len() as NodeId {
            if !consumed.contains(&node)
                && let Some(name) = index.ident_of(node)
                && !is_hole_keyword(name)
                && seen.insert(name.into())
            {
                tokens.push(Token::Ident(name.into()));
            }
        }
    }
    tokens
}
