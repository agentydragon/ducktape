//! The node-indexed `Index` view of one statement's facts.

use std::collections::HashMap;

use chunk_facts::{ChunkFacts, NodeId, NodeKind};
use regex::Regex;

use crate::predicates::regex_predicate_pattern;

/// A node-indexed view of one statement's `ChunkFacts`, owning its string labels
/// (`Box<str>`) so it can be **built once and cached** — e.g. one per chunk body
/// item in `ChunkResolver` — and reused across many needle matches, instead of
/// rebuilt per `(needle, subject)` pair. `kind` holds each node's [`NodeKind`]
/// (copied from the facts, themselves `Copy`); the value labels are copied out so
/// the index outlives the borrowed facts.
pub struct Index {
    // Node ids are dense (`0..node_count`), so every relation is a `Vec` indexed
    // by node id — array access instead of hashing on the per-node-match hot path.
    pub(crate) kind: Vec<NodeKind>,
    pub(crate) children: Vec<Vec<NodeId>>,
    pub(crate) ident: Vec<Option<Box<str>>>,
    pub(crate) str_lit: Vec<Option<Box<str>>>,
    pub(crate) num_lit: Vec<Option<Box<str>>>,
    pub(crate) bool_lit: Vec<Option<bool>>,
    pub(crate) prop_name: Vec<Option<Box<str>>>,
    pub(crate) operator: Vec<Option<Box<str>>>,
    pub(crate) regex: Vec<Option<(Box<str>, Box<str>)>>,
    /// A `Class` node's superclass expression node (the `extends` clause), kept
    /// as a separate relation because it is not a child member. `homo` compares
    /// it for `Class` nodes (both present and matched, or both absent).
    pub(crate) super_class: Vec<Option<NodeId>>,
    /// `STR_LITERAL_MATCHING_RE(...)` predicate node -> its compiled pattern,
    /// built once when the index is built (only needle indices carry predicates;
    /// the marker keyword never appears in real subject source). `homo` looks the
    /// regex up instead of recompiling it per candidate — the regex compile, not
    /// the match, dominated the CSS-module class-name member scans.
    pub(crate) predicate_regex: HashMap<NodeId, Regex>,
    pub(crate) roots: Vec<NodeId>,
}

impl Index {
    pub fn build(facts: &ChunkFacts) -> Self {
        let n = facts.node_kind.len();
        // node_kind is pushed in node-id order, so it already indexes by id.
        let kind: Vec<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        let mut children: Vec<Vec<NodeId>> = vec![Vec::new(); n];
        let mut child_ordinals: Vec<Vec<(u32, NodeId)>> = vec![Vec::new(); n];
        for (parent, ordinal, child) in &facts.child {
            child_ordinals[*parent as usize].push((*ordinal, *child));
        }
        for (parent, mut kids) in child_ordinals.into_iter().enumerate() {
            kids.sort_by_key(|(ordinal, _)| *ordinal);
            children[parent] = kids.into_iter().map(|(_, child)| child).collect();
        }
        let label = |source: &[(NodeId, String)]| {
            let mut out: Vec<Option<Box<str>>> = vec![None; n];
            for (id, value) in source {
                out[*id as usize] = Some(value.as_str().into());
            }
            out
        };
        let mut bool_lit = vec![None; n];
        for (id, value) in &facts.bool_lit {
            bool_lit[*id as usize] = Some(*value);
        }
        let mut regex: Vec<Option<(Box<str>, Box<str>)>> = vec![None; n];
        for (id, exp, flags) in &facts.regex {
            regex[*id as usize] = Some((exp.as_str().into(), flags.as_str().into()));
        }
        let mut super_class = vec![None; n];
        for (class, super_node) in &facts.super_class {
            super_class[*class as usize] = Some(*super_node);
        }
        let mut index = Index {
            kind,
            children,
            ident: label(&facts.ident_name),
            str_lit: label(&facts.str_lit),
            num_lit: label(&facts.num_lit),
            bool_lit,
            prop_name: label(&facts.prop_name),
            operator: label(&facts.operator),
            regex,
            super_class,
            predicate_regex: HashMap::new(),
            roots: facts.top_level.iter().map(|(id, _)| *id).collect(),
        };
        // Compile each `STR_LITERAL_MATCHING_RE(...)` predicate once now (the
        // structure needed to detect one is fully built above). A pattern that
        // fails to compile is absent; `unsupported_needle_construct` rejects it
        // before any candidate traversal (including an empty candidate set).
        for node in 0..n as NodeId {
            if let Some(pattern) = regex_predicate_pattern(&index, node)
                && let Ok(compiled) = Regex::new(pattern)
            {
                index.predicate_regex.insert(node, compiled);
            }
        }
        index
    }

    pub(crate) fn children_of(&self, id: NodeId) -> &[NodeId] {
        self.children.get(id as usize).map_or(&[], Vec::as_slice)
    }

    pub(crate) fn kind_of(&self, id: NodeId) -> NodeKind {
        self.kind[id as usize]
    }

    pub(crate) fn ident_of(&self, id: NodeId) -> Option<&str> {
        self.ident.get(id as usize).and_then(|slot| slot.as_deref())
    }

    pub(crate) fn str_lit_of(&self, id: NodeId) -> Option<&str> {
        self.str_lit
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    pub(crate) fn num_lit_of(&self, id: NodeId) -> Option<&str> {
        self.num_lit
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    pub(crate) fn bool_lit_of(&self, id: NodeId) -> Option<bool> {
        self.bool_lit.get(id as usize).copied().flatten()
    }

    pub(crate) fn prop_name_of(&self, id: NodeId) -> Option<&str> {
        self.prop_name
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    pub(crate) fn operator_of(&self, id: NodeId) -> Option<&str> {
        self.operator
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    pub(crate) fn regex_of(&self, id: NodeId) -> Option<(&str, &str)> {
        self.regex
            .get(id as usize)
            .and_then(|slot| slot.as_ref())
            .map(|(exp, flags)| (&**exp, &**flags))
    }

    pub(crate) fn super_class_of(&self, id: NodeId) -> Option<NodeId> {
        self.super_class.get(id as usize).copied().flatten()
    }
}

/// Read surface over a built [`Index`] for the fact-based near-miss diagnostics
/// (`source_match_diagnostics::fact_near_miss`), which instruments this matcher's own
/// first-divergence descent. These expose the same EDB relations `homo` consults
/// — node kind, ordered children, identifier/property/operator labels, the
/// superclass edge, and the top-level root — so the diagnostic walk reads exactly
/// what the match walk reads, never re-walking ASTs.
impl Index {
    /// The single top-level (root) node of a one-statement index, or `None` for
    /// an empty (non-extractable) statement.
    pub fn root(&self) -> Option<NodeId> {
        self.roots.first().copied()
    }

    pub fn kind(&self, id: NodeId) -> &'static str {
        self.kind_of(id).as_tag()
    }

    pub fn node_kind(&self, id: NodeId) -> NodeKind {
        self.kind_of(id)
    }

    /// Node ids are dense: every node is one of `0..node_count()`.
    pub fn node_count(&self) -> usize {
        self.kind.len()
    }

    pub fn children(&self, id: NodeId) -> &[NodeId] {
        self.children_of(id)
    }

    pub fn ident(&self, id: NodeId) -> Option<&str> {
        self.ident_of(id)
    }

    pub fn prop_name(&self, id: NodeId) -> Option<&str> {
        self.prop_name_of(id)
    }

    pub fn operator(&self, id: NodeId) -> Option<&str> {
        self.operator_of(id)
    }
}
