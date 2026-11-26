//! The shape matcher's kernel: a structural homomorphism from a selector
//! needle's `chunk_facts` projection onto a candidate's, anchored at the
//! top-level statement. It operates over the facts, never by re-walking ASTs;
//! `source_match::chunk_resolver` runs it per candidate.
//!
//! Faithful subset: exact- and alpha-identifier structure with
//! **expression-position single-node holes** (`ANYTHING` / `EXPR` / `STMT`
//! matching any one subtree) and **variable-length run holes** (`STMT_LIST` /
//! `ARGS` / `ARRAY_ELEMENTS` / `CASE_REST` / `DECLARATORS` / `SEQ_EXPRS`) matched
//! as an ordered subsequence with gaps. Run-hole placement partitions the needle
//! list into maximal fixed segments at the carriers, placed greedy-leftmost with
//! `Bindings` snapshot/restore. The
//! `STR_LITERAL_MATCHING_RE("re")` predicate matches a string-literal subject
//! whose value matches `re`. `AST_CONTAINS_STRING("value")` matches an expression
//! subtree with an exact string-literal descendant. Fail-closed only on what is
//! genuinely unhandled: a
//! **misplaced** run-hole keyword (one reaching the node matcher rather than
//! being consumed as a list carrier) or a malformed predicate returns
//! [`Unsupported`] rather than a weaker (under-constraining) match. Match
//! semantics across identifier modes, holes, and declarator alignment are pinned
//! by `selector_match_test`.
//!
//! Run-hole placement is a direct greedy-plus-backtracking search. Why matching
//! is not encoded as solver constraints: <docs/selector_resolution.md>
//! § Rejected: tree matching as solver constraints.

use std::collections::BTreeMap;

use chunk_facts::NodeId;

mod bindings;
mod holes;
mod homo;
mod index;
mod list_align;
mod predicates;
mod sequence;
mod single;
mod tokens;
mod validate;

pub use holes::{is_class_rest_member, is_declarator_run_hole};
pub use homo::nodes_match;
pub use index::Index;
pub use list_align::{
    pinned_declarator_matches_in_order, var_declarator_alignment_indexed,
    var_declarator_alignment_prepared,
};
pub use sequence::{
    explain_exact_range_indexed, match_fixed_window_sequence_indexed,
    match_single_declarator_target_windows_indexed, match_top_level_sequence_indexed,
};
pub use single::{
    matches, matches_indexed, matches_prepared, needle_root_kind_prefilter,
    needle_var_declarator_init_kind_prefilter, subject_root_kind, var_declarator_init_kind,
};
pub use tokens::{Token, invariant_tokens, needle_required_tokens, subject_tokens};
pub use validate::{consumed_nodes, diagnostic_shape_bounded};

/// A needle construct whose faithful encoding this matcher has not implemented.
/// Loud by design: the matcher returns this rather than a weaker (under-
/// constrained) match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub reason: &'static str,
}

impl std::fmt::Display for Unsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.reason)
    }
}

impl std::error::Error for Unsupported {}

/// One successful match: where it landed (`site`: an alignment, a window start,
/// a declarator) and what each of the needle's free identifiers bound to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Matched<T> {
    pub site: T,
    pub free_bindings: BTreeMap<String, String>,
}

/// Identifier-matching mode (mirrors `SourceMatchIdentifierMode`). `Exact`
/// requires identical spellings; `AlphaAll` treats value/binding identifiers as
/// alpha-renamable — a needle identifier binds to one subject identifier
/// consistently, which is the consistency join.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Exact,
    AlphaAll,
}

/// A failure observed by the actual structural matcher on the best explored
/// branch. Node IDs are relative to the two statement fact projections.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TraceCategory {
    Shape,
    Literal,
    Binding,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceFailure {
    pub needle_statement_index: usize,
    pub subject_statement_index: usize,
    pub needle_node: NodeId,
    pub subject_node: NodeId,
    pub category: TraceCategory,
    pub reason: String,
    /// Comparisons reached along this branch; used to prefer a deeper failure.
    pub progress: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExactRangeMatch {
    pub matched: bool,
    /// One subject position per needle statement; holes have no position.
    pub alignment: Vec<Option<usize>>,
    pub free_bindings: BTreeMap<String, String>,
    pub failure: Option<TraceFailure>,
    pub diagnostic_limited: bool,
}
