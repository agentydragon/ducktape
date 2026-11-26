//! `STR_LITERAL_MATCHING_RE` and `AST_CONTAINS_STRING` predicate recognition.

use chunk_facts::{NodeId, NodeKind};
use source_match_holes::{AST_CONTAINS_STRING_PREDICATE, STRING_LITERAL_REGEX_PREDICATE};

use crate::index::Index;

/// The regex pattern of a well-formed `STR_LITERAL_MATCHING_RE("re")` predicate
/// at `node`: a `Call` of exactly the predicate callee with one string-literal
/// argument. Mirrors `holes.rs::string_literal_regex_pattern`. The predicate
/// matches a string-literal subject whose value matches `re`, not by structure.
pub(crate) fn regex_predicate_pattern(index: &Index, node: NodeId) -> Option<&str> {
    if index.kind_of(node) != NodeKind::Call {
        return None;
    }
    let kids = index.children_of(node);
    let [callee, arg] = kids else {
        return None;
    };
    let is_predicate = index
        .ident_of(*callee)
        .is_some_and(|name| name == STRING_LITERAL_REGEX_PREDICATE);
    (is_predicate && index.kind_of(*arg) == NodeKind::StrLit)
        .then(|| index.str_lit_of(*arg))
        .flatten()
}

/// Exact string literal required anywhere below the candidate expression.
/// Unlike regex matching, this has an invariant token, so the existing token
/// posting index filters candidates before the subtree walk.
pub(crate) fn descendant_string_pattern(index: &Index, node: NodeId) -> Option<&str> {
    if index.kind_of(node) != NodeKind::Call {
        return None;
    }
    let [callee, arg] = index.children_of(node) else {
        return None;
    };
    (index.ident_of(*callee) == Some(AST_CONTAINS_STRING_PREDICATE)
        && index.kind_of(*arg) == NodeKind::StrLit)
        .then(|| index.str_lit_of(*arg))
        .flatten()
}

pub(crate) fn subtree_contains_string(index: &Index, root: NodeId, expected: &str) -> bool {
    let mut pending = vec![root];
    while let Some(node) = pending.pop() {
        if index.str_lit_of(node) == Some(expected) {
            return true;
        }
        pending.extend_from_slice(index.children_of(node));
    }
    false
}
