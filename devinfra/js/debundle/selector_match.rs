//! P2 (matching over the facts): a structural homomorphism from a selector
//! needle's `chunk_facts` projection onto a candidate's, anchored at the
//! top-level statement. This is the Datalog-side matcher the resolver will use;
//! it operates over the AST-facts EDB, never by re-walking ASTs.
//!
//! Faithful subset: exact- and alpha-identifier structure with
//! **expression-position single-node holes** (`ANYTHING` / `EXPR` / `STMT`
//! matching any one subtree) and **variable-length run holes** (`STMT_LIST` /
//! `ARGS` / `OBJECT_PROPS` / `CLASS_REST` / `CASE_REST` / `DECLARATORS`) matched
//! as an ordered subsequence with gaps. The run-hole placement mirrors the
//! production matcher's `match_list_with_holes` / `place_segments` over facts:
//! the carriers partition the needle list into maximal fixed segments, placed
//! greedy-leftmost with `Bindings` snapshot/restore. Fail-closed elsewhere: the
//! `STR_LITERAL_MATCHING_RE` predicate and any **misplaced** run-hole keyword
//! (one reaching the node matcher rather than being consumed as a list carrier)
//! return [`Unsupported`] rather than a weaker (under-constraining) match.
//! Parity with the production matcher is **proven** by
//! `selector_match_differential_test` against `source_match::needle_matches`.
//!
//! This per-`(needle, subject)` homomorphism is the **kernel match relation**,
//! not a rival "N separate solves" design: the one global evaluation (the plan's
//! P4) composes it, and for today's cross-ref-free corpus the global solve
//! decomposes by connected components into exactly these independent matches
//! (see `plans/selector_constraint_model.md`). The run-hole placement is
//! realized here as a direct (greedy + backtracking) search; the equivalent
//! relational chain-join — the form that folds into the global fixpoint, with
//! cross-gap alpha-binding coupling fail-closed — is the P3/P4 native-lowering
//! shape recorded in the plan.

use std::collections::{HashMap, HashSet};

use chunk_facts::{ChunkFacts, NodeId};
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE, hole_name_for,
};

/// A needle construct whose faithful encoding this matcher has not implemented.
/// Loud by design: the matcher returns this rather than a weaker (under-
/// constrained) match.
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
/// bijection). Cloneable so run-hole placement can snapshot/restore it across
/// backtracked segment placements. NOTE: this is whole-pattern consistency
/// without within-statement scoping/shadowing — faithful for the non-shadowing
/// selectors the corpus uses; the corpus differential is the gate.
#[derive(Default, Clone)]
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

    fn kind_of(&self, id: NodeId) -> &'a str {
        self.kind.get(&id).copied().unwrap_or_default()
    }
}

/// `ANYTHING` / `EXPR` / `STMT` (bare or named): a single-node hole matching any
/// one subtree in expression position.
fn is_single_node_hole(name: &str) -> bool {
    hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        || hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
        || hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
}

const RUN_HOLE_KEYWORDS: [&str; 6] = [
    STMT_LIST_HOLE_KEYWORD,
    ARGS_HOLE_KEYWORD,
    OBJECT_PROPS_HOLE_KEYWORD,
    CLASS_REST_HOLE_KEYWORD,
    CASE_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD,
];

/// A run-hole keyword in any (bare/named) form. Used only as a fail-closed net:
/// a run hole that reaches the node matcher was *not* consumed as a list carrier
/// by its parent, i.e. it sits in an unsupported position, so the match errors
/// rather than treating the keyword as an ordinary identifier.
fn is_run_hole_keyword(name: &str) -> bool {
    RUN_HOLE_KEYWORDS
        .iter()
        .any(|kw| hole_name_for(name, kw).is_some())
}

fn node_ident_hole(index: &Index, node: NodeId, keyword: &str) -> bool {
    index
        .ident
        .get(&node)
        .is_some_and(|name| hole_name_for(name, keyword).is_some())
}

/// True iff `child` is a variable-length run-hole carrier in a list under a
/// parent of `parent_kind`, mirroring the production matcher's per-list-type
/// `is_hole` predicates (`source_match/holes.rs`) projected onto facts. A
/// carrier is consumed by list placement and never visited by [`homo`].
fn is_run_hole_carrier(index: &Index, parent_kind: &str, child: NodeId) -> bool {
    let ck = index.kind_of(child);
    match parent_kind {
        // `STMT_LIST;` — an expression statement whose sole child is the keyword.
        "Block" => {
            ck == "ExprStmt" && {
                let kids = index.children_of(child);
                kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
            }
        }
        // `ARGS` — a bare identifier argument (the callee is split off before
        // this list, so any `ARGS` here is in argument position).
        "Call" | "New" | "OptCall" => {
            ck == "Ident" && node_ident_hole(index, child, ARGS_HOLE_KEYWORD)
        }
        // `OBJECT_PROPS` / `ANYTHING` — a shorthand object-literal property.
        "Object" => {
            ck == "Shorthand"
                && (node_ident_hole(index, child, OBJECT_PROPS_HOLE_KEYWORD)
                    || node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD))
        }
        // `OBJECT_PROPS` / `ANYTHING` in a destructuring pattern — a shorthand
        // (no default) destructure property.
        "ObjectPat" => {
            ck == "PatAssign"
                && index.children_of(child).is_empty()
                && (node_ident_hole(index, child, OBJECT_PROPS_HOLE_KEYWORD)
                    || node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD))
        }
        // `CLASS_REST;` / `ANYTHING;` — a class field, no initializer, whose key
        // is the (exact) keyword.
        "Class" => {
            ck == "ClassProp" && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index.prop_name.get(&kids[0]).is_some_and(|name| {
                        *name == CLASS_REST_HOLE_KEYWORD || *name == ANYTHING_HOLE_KEYWORD
                    })
            }
        }
        // `case CASE_REST:` — a switch clause, no body, whose sole child is the
        // (exact) keyword test.
        "Switch" => {
            ck == "SwitchCase" && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index
                        .ident
                        .get(&kids[0])
                        .is_some_and(|name| *name == CASE_REST_HOLE_KEYWORD)
            }
        }
        // `const DECLARATORS` / `ANYTHING` — a declarator whose name binding is
        // the keyword.
        "VarDecl" => {
            ck == "VarDeclarator" && {
                let kids = index.children_of(child);
                !kids.is_empty()
                    && index.kind_of(kids[0]) == "BindingIdent"
                    && (node_ident_hole(index, kids[0], DECLARATORS_HOLE_KEYWORD)
                        || node_ident_hole(index, kids[0], ANYTHING_HOLE_KEYWORD))
            }
        }
        _ => false,
    }
}

/// Number of leading children that are *not* part of a run-hole-bearing list and
/// so are matched positionally: the callee of a call/new, the discriminant of a
/// switch. Every other node's children form one matchable list.
fn list_prefix_len(parent_kind: &str) -> usize {
    matches!(parent_kind, "Call" | "New" | "OptCall" | "Switch") as usize
}

fn homo(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let nkind = needle.kind_of(nid);

    // Expression-position single-node hole: matches any one subtree. (Unhandled
    // constructs — the regex predicate, misplaced run-hole keywords — are
    // rejected up front in [`matches`], before structural matching can mask
    // them, so they never reach here.)
    if nkind == "Ident"
        && let Some(&name) = needle.ident.get(&nid)
        && is_single_node_hole(name)
    {
        return Ok(true);
    }

    // Structural equality: kind, then non-identifier labels (always exact),
    // then the identifier label (exact or alpha-bound), then children.
    if nkind != subject.kind_of(sid) {
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

    match_children(needle, nid, nkind, subject, sid, mode, bindings)
}

/// Match the children of two same-kind nodes: a positional prefix (callee /
/// discriminant), then the tail list — positionally when the needle carries no
/// run hole, or as an ordered subsequence with gaps when it does.
fn match_children(
    needle: &Index,
    nid: NodeId,
    nkind: &str,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let nchildren = needle.children_of(nid);
    let schildren = subject.children_of(sid);
    let prefix = list_prefix_len(nkind);
    let nprefix = prefix.min(nchildren.len());
    let sprefix = prefix.min(schildren.len());
    if nprefix != sprefix {
        return Ok(false);
    }
    for i in 0..nprefix {
        if !homo(needle, nchildren[i], subject, schildren[i], mode, bindings)? {
            return Ok(false);
        }
    }
    let nlist = &nchildren[nprefix..];
    let slist = &schildren[sprefix..];
    if nlist.iter().any(|&c| is_run_hole_carrier(needle, nkind, c)) {
        return match_list_with_holes(needle, nlist, subject, slist, nkind, mode, bindings);
    }
    if nlist.len() != slist.len() {
        return Ok(false);
    }
    for (nc, sc) in nlist.iter().zip(slist) {
        if !homo(needle, *nc, subject, *sc, mode, bindings)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Ordered-subsequence-with-gaps match of a run-hole-bearing needle list against
/// a candidate list. The carriers partition the needle into maximal fixed
/// segments; an all-holes list pins nothing (matches any candidate run). Mirrors
/// the production matcher's `match_list_with_holes`.
fn match_list_with_holes(
    needle: &Index,
    nlist: &[NodeId],
    subject: &Index,
    slist: &[NodeId],
    parent_kind: &str,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let mut segments: Vec<(usize, usize)> = Vec::new();
    let mut idx = 0;
    while idx < nlist.len() {
        if is_run_hole_carrier(needle, parent_kind, nlist[idx]) {
            idx += 1;
            continue;
        }
        let start = idx;
        while idx < nlist.len() && !is_run_hole_carrier(needle, parent_kind, nlist[idx]) {
            idx += 1;
        }
        segments.push((start, idx - start));
    }
    if segments.is_empty() {
        return Ok(true);
    }
    let anchored_left = !is_run_hole_carrier(needle, parent_kind, nlist[0]);
    let anchored_right = !is_run_hole_carrier(needle, parent_kind, nlist[nlist.len() - 1]);
    place_segments(
        needle,
        nlist,
        subject,
        slist,
        &segments,
        anchored_left,
        anchored_right,
        0,
        0,
        mode,
        bindings,
    )
}

/// Place `segments[seg_idx..]` into `slist[cand_min..]`, leftmost-first, rolling
/// the `Bindings` back after each failed attempt; returns true (leaving the
/// committed bindings) once every remaining segment is placed. Mirrors the
/// production matcher's `place_segments`.
#[allow(clippy::too_many_arguments)]
fn place_segments(
    needle: &Index,
    nlist: &[NodeId],
    subject: &Index,
    slist: &[NodeId],
    segments: &[(usize, usize)],
    anchored_left: bool,
    anchored_right: bool,
    seg_idx: usize,
    cand_min: usize,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let Some(&(needle_start, seg_len)) = segments.get(seg_idx) else {
        return Ok(true);
    };
    let remaining: usize = segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = slist.len().checked_sub(remaining) else {
        return Ok(false);
    };
    let mut lo = cand_min;
    let mut hi = latest_start;
    if seg_idx == 0 && anchored_left {
        hi = hi.min(0);
    }
    if seg_idx == segments.len() - 1 && anchored_right {
        lo = lo.max(latest_start);
    }
    for start in lo..=hi {
        let snapshot = bindings.clone();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            if !homo(
                needle,
                nlist[needle_start + offset],
                subject,
                slist[start + offset],
                mode,
                bindings,
            )? {
                segment_ok = false;
                break;
            }
        }
        if segment_ok
            && place_segments(
                needle,
                nlist,
                subject,
                slist,
                segments,
                anchored_left,
                anchored_right,
                seg_idx + 1,
                start + seg_len,
                mode,
                bindings,
            )?
        {
            return Ok(true);
        }
        *bindings = snapshot;
    }
    Ok(false)
}

fn collect_subtree(index: &Index, node: NodeId, out: &mut HashSet<NodeId>) {
    if out.insert(node) {
        for &child in index.children_of(node) {
            collect_subtree(index, child, out);
        }
    }
}

/// Reject — **before** structural matching, so it can never be masked by an
/// earlier kind/arity mismatch short-circuiting to `false` — any needle
/// construct this matcher does not faithfully handle: the unlowered
/// `STR_LITERAL_MATCHING_RE` predicate, and any run-hole keyword that is not
/// consumed as a list carrier (a misplaced hole). A keyword is consumed iff it
/// lies inside a carrier subtree, so this mirrors exactly what list placement
/// will and will not absorb.
fn unsupported_needle_construct(index: &Index) -> Option<&'static str> {
    if index
        .ident
        .values()
        .any(|name| *name == STRING_LITERAL_REGEX_PREDICATE)
    {
        return Some("STR_LITERAL_MATCHING_RE predicate not lowered");
    }
    let mut consumed: HashSet<NodeId> = HashSet::new();
    for (&parent, kids) in &index.children {
        let parent_kind = index.kind_of(parent);
        for &child in kids {
            if is_run_hole_carrier(index, parent_kind, child) {
                collect_subtree(index, child, &mut consumed);
            }
        }
    }
    let misplaced = index
        .ident
        .iter()
        .chain(index.prop_name.iter())
        .any(|(node, name)| is_run_hole_keyword(name) && !consumed.contains(node));
    misplaced.then_some("run-hole keyword outside a list position")
}

/// True iff the needle (one top-level statement) structurally matches the
/// subject statement under `mode`, with single-node and run holes as described
/// in the module docs. Errors (fail-closed) on un-lowered constructs (the regex
/// predicate, a misplaced run hole) rather than returning a weaker match. Both
/// inputs are single-statement `ChunkFacts`.
pub fn matches(needle: &ChunkFacts, subject: &ChunkFacts, mode: Mode) -> Result<bool, Unsupported> {
    let needle = Index::build(needle);
    if let Some(reason) = unsupported_needle_construct(&needle) {
        return Err(Unsupported { reason });
    }
    let subject = Index::build(subject);
    let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
        return Ok(false);
    };
    let mut bindings = Bindings::default();
    homo(&needle, n_root, &subject, s_root, mode, &mut bindings)
}
