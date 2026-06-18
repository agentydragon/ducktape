//! P2 (matching over the facts): a structural homomorphism from a selector
//! needle's `chunk_facts` projection onto a candidate's, anchored at the
//! top-level statement. This is the Datalog-side matcher the resolver will use;
//! it operates over the AST-facts EDB, never by re-walking ASTs.
//!
//! Faithful subset (rung 1): exact-identifier structure with **expression-
//! position single-node holes** (`ANYTHING` / `EXPR` / `STMT` matching any one
//! subtree). Fail-closed elsewhere — variable-length run holes (`STMT_LIST`,
//! `ARGS`, …), the `STR_LITERAL_MATCHING_RE` predicate, and alpha-equivalence
//! (which needs scope facts the EDB does not yet carry) return [`Unsupported`]
//! rather than a weaker (under-constraining) match. Parity with the production
//! matcher is **proven** by `selector_match_differential_test` against
//! `source_match::needle_matches`, not asserted.

use std::collections::HashMap;

use chunk_facts::{ChunkFacts, NodeId};
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE, hole_name_for,
};

/// A needle construct whose faithful encoding rung 1 has not implemented. Loud
/// by design: the matcher returns this rather than a weaker (under-constrained)
/// match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub reason: &'static str,
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

/// Bijective needle↔subject identifier binding accumulated during alpha
/// matching. `bind` succeeds iff consistent in both directions, so distinct
/// needle identifiers map to distinct subject ones (alpha-equivalence is a
/// bijection). NOTE (rung 2): this is whole-pattern consistency without
/// within-statement scoping/shadowing — faithful for the non-shadowing
/// selectors the corpus uses; the corpus differential is the gate that proves
/// it (and would flag any shadowing case, which then motivates scope facts).
#[derive(Default)]
struct Bindings {
    needle_to_subject: HashMap<String, String>,
    subject_to_needle: HashMap<String, String>,
}

impl Bindings {
    fn bind(&mut self, needle: &str, subject: &str) -> bool {
        if let Some(existing) = self.needle_to_subject.get(needle) {
            return existing == subject;
        }
        if self.subject_to_needle.contains_key(subject) {
            return false;
        }
        self.needle_to_subject
            .insert(needle.to_string(), subject.to_string());
        self.subject_to_needle
            .insert(subject.to_string(), needle.to_string());
        true
    }
}

/// Per-node lookups over one `ChunkFacts`, built once.
struct Index<'a> {
    kind: HashMap<NodeId, &'a str>,
    children: HashMap<NodeId, Vec<NodeId>>,
    ident: HashMap<NodeId, &'a str>,
    str_lit: HashMap<NodeId, &'a str>,
    num_lit: HashMap<NodeId, &'a str>,
    bool_lit: HashMap<NodeId, bool>,
    prop_name: HashMap<NodeId, &'a str>,
    operator: HashMap<NodeId, &'a str>,
    regex: HashMap<NodeId, (&'a str, &'a str)>,
    roots: Vec<NodeId>,
}

impl<'a> Index<'a> {
    fn build(facts: &'a ChunkFacts) -> Self {
        let mut by_parent: HashMap<NodeId, Vec<(u32, NodeId)>> = HashMap::new();
        for (parent, ordinal, child) in &facts.child {
            by_parent
                .entry(*parent)
                .or_default()
                .push((*ordinal, *child));
        }
        let children = by_parent
            .into_iter()
            .map(|(parent, mut kids)| {
                kids.sort_by_key(|(ordinal, _)| *ordinal);
                (parent, kids.into_iter().map(|(_, child)| child).collect())
            })
            .collect();
        Index {
            kind: facts.node_kind.iter().map(|(id, k)| (*id, *k)).collect(),
            children,
            ident: facts
                .ident_name
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            str_lit: facts
                .str_lit
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            num_lit: facts
                .num_lit
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            bool_lit: facts.bool_lit.iter().copied().collect(),
            prop_name: facts
                .prop_name
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            operator: facts
                .operator
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            regex: facts
                .regex
                .iter()
                .map(|(id, exp, flags)| (*id, (exp.as_str(), flags.as_str())))
                .collect(),
            roots: facts.top_level.iter().map(|(id, _)| *id).collect(),
        }
    }

    fn children_of(&self, id: NodeId) -> &[NodeId] {
        self.children.get(&id).map_or(&[], Vec::as_slice)
    }
}

enum HoleClass {
    /// `ANYTHING` / `EXPR` / `STMT`: matches any one subtree in this position.
    SingleNode,
    /// Run/list holes and the regex predicate: not faithful in rung 1.
    Unsupported,
}

fn hole_class(name: &str) -> Option<HoleClass> {
    if hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        || hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
        || hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
    {
        return Some(HoleClass::SingleNode);
    }
    let run_holes = [
        STMT_LIST_HOLE_KEYWORD,
        ARGS_HOLE_KEYWORD,
        OBJECT_PROPS_HOLE_KEYWORD,
        CLASS_REST_HOLE_KEYWORD,
        CASE_REST_HOLE_KEYWORD,
        DECLARATORS_HOLE_KEYWORD,
    ];
    if run_holes.iter().any(|kw| hole_name_for(name, kw).is_some())
        || name == STRING_LITERAL_REGEX_PREDICATE
    {
        return Some(HoleClass::Unsupported);
    }
    None
}

fn homo(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let nkind = needle.kind.get(&nid).copied().unwrap_or_default();

    // Expression-position single-node hole (`ANYTHING` / `EXPR` / `STMT`):
    // matches any one subtree. Run/list holes are rejected up front in
    // [`matches`], so they never reach here (a mid-traversal arity check could
    // otherwise short-circuit to a wrong `false` before the hole is seen).
    if nkind == "Ident"
        && let Some(name) = needle.ident.get(&nid)
        && matches!(hole_class(name), Some(HoleClass::SingleNode))
    {
        return Ok(true);
    }

    // Structural equality: kind, then non-identifier labels (always exact),
    // then the identifier label (exact or alpha-bound), then children.
    if nkind != subject.kind.get(&sid).copied().unwrap_or_default() {
        return Ok(false);
    }
    if needle.str_lit.get(&nid) != subject.str_lit.get(&sid)
        || needle.num_lit.get(&nid) != subject.num_lit.get(&sid)
        || needle.bool_lit.get(&nid) != subject.bool_lit.get(&sid)
        || needle.prop_name.get(&nid) != subject.prop_name.get(&sid)
        || needle.operator.get(&nid) != subject.operator.get(&sid)
        || needle.regex.get(&nid) != subject.regex.get(&sid)
    {
        return Ok(false);
    }
    match (
        needle.ident.get(&nid).copied(),
        subject.ident.get(&sid).copied(),
    ) {
        (Some(n), Some(s)) => {
            let consistent = match mode {
                Mode::Exact => n == s,
                Mode::AlphaAll => bindings.bind(n, s),
            };
            if !consistent {
                return Ok(false);
            }
        }
        (None, None) => {}
        _ => return Ok(false),
    }

    let nchildren = needle.children_of(nid);
    let schildren = subject.children_of(sid);
    if nchildren.len() != schildren.len() {
        return Ok(false);
    }
    for (nc, sc) in nchildren.iter().zip(schildren) {
        if !homo(needle, *nc, subject, *sc, mode, bindings)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// True iff the needle (one top-level statement) structurally matches the
/// subject statement under `mode`, with expression-position single-node holes.
/// Errors (fail-closed) on un-lowered constructs rather than returning a weaker
/// match. Both inputs are single-statement `ChunkFacts`.
pub fn matches(needle: &ChunkFacts, subject: &ChunkFacts, mode: Mode) -> Result<bool, Unsupported> {
    let needle = Index::build(needle);
    let subject = Index::build(subject);
    // Fail-closed up front: a needle containing any not-yet-faithful hole
    // (variable-length run/list hole or the regex predicate) cannot be matched
    // soundly yet, and an arity check during traversal could otherwise mask it
    // as a wrong `false`. Reject the whole needle loudly instead.
    if needle
        .ident
        .values()
        .any(|name| matches!(hole_class(name), Some(HoleClass::Unsupported)))
    {
        return Err(Unsupported {
            reason: "needle uses a run/list hole or the regex predicate",
        });
    }
    let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
        return Ok(false);
    };
    let mut bindings = Bindings::default();
    homo(&needle, n_root, &subject, s_root, mode, &mut bindings)
}
