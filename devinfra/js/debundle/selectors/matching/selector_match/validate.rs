//! Fail-closed needle validation and the diagnostic shape gate.

use std::collections::HashSet;

use chunk_facts::NodeId;
use source_match_holes::{
    AST_CONTAINS_STRING_PREDICATE, STRING_LITERAL_REGEX_PREDICATE, is_run_hole_keyword,
};

use crate::holes::{is_floating_declarator, is_run_hole_carrier};
use crate::index::Index;
use crate::predicates::{descendant_string_pattern, regex_predicate_pattern};

/// Conservative size gate for the diagnostic API before it invokes the normal
/// member candidate resolver, whose matching search is intentionally unbounded.
/// This gate is never used by ordinary selector resolution.
pub fn diagnostic_shape_bounded(index: &Index) -> bool {
    const MAX_DEPTH: usize = 64;
    const MAX_HOLES: usize = 6;
    const MAX_HOLE_LIST: usize = 16;
    let mut holes = 0;
    for (parent, children) in index.children.iter().enumerate() {
        let kind = index.kind_of(parent as NodeId);
        let count = children
            .iter()
            .filter(|&&child| {
                is_run_hole_carrier(index, kind, child)
                    || is_floating_declarator(index, kind, child)
            })
            .count();
        holes += count;
        if holes > MAX_HOLES || (count > 0 && children.len() > MAX_HOLE_LIST) {
            return false;
        }
    }
    let mut stack: Vec<_> = index.roots.iter().map(|&root| (root, 0)).collect();
    while let Some((node, depth)) = stack.pop() {
        if depth > MAX_DEPTH {
            return false;
        }
        stack.extend(
            index
                .children_of(node)
                .iter()
                .map(|&child| (child, depth + 1)),
        );
    }
    true
}

pub(crate) fn collect_subtree(index: &Index, node: NodeId, out: &mut HashSet<NodeId>) {
    if out.insert(node) {
        for &child in index.children_of(node) {
            collect_subtree(index, child, out);
        }
    }
}

/// The needle nodes the matcher's structural rules **absorb** rather than compare
/// node-for-node: every node inside a run-hole carrier subtree (consumed by list
/// placement, never visited by [`homo`]) and every well-formed
/// `STR_LITERAL_MATCHING_RE(...)` predicate's callee + argument (the matcher
/// handles the whole `Call`, never the bare callee identifier). Shared by the
/// faithful-subset guard ([`unsupported_needle_construct`]) and the exact-mode
/// identifier discriminator ([`needle_required_tokens`]), and the free-identifier
/// classifier (`source_match::free_identifiers`), so they agree exactly
/// on which identifiers are real comparisons versus absorbed placeholders.
pub fn consumed_nodes(index: &Index) -> HashSet<NodeId> {
    let mut consumed: HashSet<NodeId> = HashSet::new();
    for (parent, kids) in index.children.iter().enumerate() {
        let parent_kind = index.kind_of(parent as NodeId);
        for &child in kids {
            if is_run_hole_carrier(index, parent_kind, child) {
                collect_subtree(index, child, &mut consumed);
            }
        }
    }
    for node in 0..index.kind.len() as NodeId {
        if regex_predicate_pattern(index, node).is_some()
            || descendant_string_pattern(index, node).is_some()
        {
            consumed.extend(index.children_of(node));
        }
    }
    consumed
}

/// Reject — **before** structural matching, so it can never be masked by an
/// earlier kind/arity mismatch short-circuiting to `false` — any needle
/// construct this matcher does not faithfully handle: a run-hole keyword not
/// consumed as a list carrier (a misplaced hole), or a `STR_LITERAL_MATCHING_RE`
/// occurrence that is not a well-formed predicate callee. A token is consumed
/// iff it lies inside a run-hole carrier subtree or is a predicate's callee/arg,
/// which mirrors exactly what the matcher's structural rules absorb.
pub(crate) fn unsupported_needle_construct(index: &Index) -> Option<&'static str> {
    let consumed = consumed_nodes(index);
    for node in 0..index.kind.len() as NodeId {
        if regex_predicate_pattern(index, node).is_some()
            && !index.predicate_regex.contains_key(&node)
        {
            return Some("invalid STR_LITERAL_MATCHING_RE regex");
        }
        let Some(name) = index.ident_of(node).or_else(|| index.prop_name_of(node)) else {
            continue;
        };
        if name == STRING_LITERAL_REGEX_PREDICATE && !consumed.contains(&node) {
            return Some("malformed STR_LITERAL_MATCHING_RE predicate");
        }
        if name == AST_CONTAINS_STRING_PREDICATE && !consumed.contains(&node) {
            return Some("malformed AST_CONTAINS_STRING predicate");
        }
        // A run hole its parent did not consume as a list carrier sits in an
        // unsupported position: fail closed instead of matching an identifier.
        if is_run_hole_keyword(name) && !consumed.contains(&node) {
            return Some("run-hole keyword outside a list position");
        }
    }
    None
}
