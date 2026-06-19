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
//! greedy-leftmost with `Bindings` snapshot/restore. The
//! `STR_LITERAL_MATCHING_RE("re")` predicate matches a string-literal subject
//! whose value matches `re`. Fail-closed only on what is genuinely unhandled: a
//! **misplaced** run-hole keyword (one reaching the node matcher rather than
//! being consumed as a list carrier) or a malformed predicate returns
//! [`Unsupported`] rather than a weaker (under-constraining) match. Parity with
//! the production matcher is **proven** by `selector_match_differential_test`
//! against `source_match::needle_matches`.
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
use regex::Regex;
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

/// One lexical frame's bijective needle↔subject identifier map.
#[derive(Default, Clone)]
struct AlphaScope {
    forward: HashMap<String, String>,
    backward: HashMap<String, String>,
}

/// Scope-aware bijective needle↔subject identifier binding accumulated during
/// alpha matching — a stack of lexical frames, mirroring the production matcher's
/// `alpha_scopes`. References (`match_ref`) resolve against the visible stack
/// (innermost-out); bindings (`match_binding`) consult only the current frame so a
/// binding may shadow an outer same-spelled one. A function/arrow/constructor/
/// setter/catch node pushes a frame around its params + body, so same-spelled
/// locals in sibling scopes (e.g. a param reused across two functions) stay
/// independent — without this the flat bijection conflated them and under-matched.
/// Cloneable so run-hole placement can snapshot/restore across backtracking.
#[derive(Clone)]
struct Bindings {
    scopes: Vec<AlphaScope>,
}

impl Default for Bindings {
    fn default() -> Self {
        Self {
            scopes: vec![AlphaScope::default()],
        }
    }
}

impl Bindings {
    fn push_scope(&mut self) {
        self.scopes.push(AlphaScope::default());
    }

    fn pop_scope(&mut self) {
        self.scopes.pop();
    }

    /// Match an identifier **reference**: consult the visible scope stack
    /// (innermost-out); bind in the current frame if neither side is known.
    fn match_ref(&mut self, needle: &str, subject: &str) -> bool {
        for scope in self.scopes.iter().rev() {
            if let Some(mapped) = scope.forward.get(needle) {
                return mapped == subject;
            }
            if scope.backward.contains_key(subject) {
                return false;
            }
        }
        self.bind_current(needle, subject)
    }

    /// Match an identifier **binding** (declaration): consult only the current
    /// frame, so it may shadow an outer binding of the same spelling.
    fn match_binding(&mut self, needle: &str, subject: &str) -> bool {
        self.bind_current(needle, subject)
    }

    fn bind_current(&mut self, needle: &str, subject: &str) -> bool {
        let scope = self.scopes.last_mut().expect("always a root scope");
        match (scope.forward.get(needle), scope.backward.get(subject)) {
            (Some(mapped), _) => mapped == subject,
            (None, Some(_)) => false,
            (None, None) => {
                scope
                    .forward
                    .insert(needle.to_string(), subject.to_string());
                scope
                    .backward
                    .insert(subject.to_string(), needle.to_string());
                true
            }
        }
    }
}

/// Node kinds that introduce a lexical scope for their params + body (mirrors the
/// production matcher's `with_alpha_scope` sites: function/arrow bodies, catch
/// clauses, setter and constructor params).
fn introduces_alpha_scope(kind: &str) -> bool {
    matches!(
        kind,
        "Function"
            | "AsyncFunction"
            | "GeneratorFunction"
            | "AsyncGeneratorFunction"
            | "Arrow"
            | "AsyncArrow"
            | "Constructor"
            | "Setter"
            | "Catch"
    )
}

/// A node-indexed view of one statement's `ChunkFacts`, owning its string labels
/// (`Box<str>`) so it can be **built once and cached** — e.g. one per chunk body
/// item in `ChunkResolver` — and reused across many needle matches, instead of
/// rebuilt per `(needle, subject)` pair. `kind` keeps the `&'static` node-kind
/// tags (already static in the facts); the value labels are copied out so the
/// index outlives the borrowed facts.
pub struct Index {
    // Node ids are dense (`0..node_count`), so every relation is a `Vec` indexed
    // by node id — array access instead of hashing on the per-node-match hot path.
    kind: Vec<&'static str>,
    children: Vec<Vec<NodeId>>,
    ident: Vec<Option<Box<str>>>,
    str_lit: Vec<Option<Box<str>>>,
    num_lit: Vec<Option<Box<str>>>,
    bool_lit: Vec<Option<bool>>,
    prop_name: Vec<Option<Box<str>>>,
    operator: Vec<Option<Box<str>>>,
    regex: Vec<Option<(Box<str>, Box<str>)>>,
    roots: Vec<NodeId>,
}

impl Index {
    pub fn build(facts: &ChunkFacts) -> Self {
        let n = facts.node_kind.len();
        // node_kind is pushed in node-id order, so it already indexes by id.
        let kind = facts.node_kind.iter().map(|(_, k)| *k).collect();
        let mut children: Vec<Vec<NodeId>> = vec![Vec::new(); n];
        let mut child_ordinals: Vec<Vec<(u32, NodeId)>> = vec![Vec::new(); n];
        for (parent, ordinal, child) in &facts.child {
            child_ordinals[*parent as usize].push((*ordinal, *child));
        }
        for (parent, mut kids) in child_ordinals.into_iter().enumerate() {
            kids.sort_by_key(|(ordinal, _)| *ordinal);
            children[parent] = kids.into_iter().map(|(_, child)| child).collect();
        }
        let mut label = |source: &[(NodeId, String)]| {
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
        Index {
            kind,
            children,
            ident: label(&facts.ident_name),
            str_lit: label(&facts.str_lit),
            num_lit: label(&facts.num_lit),
            bool_lit,
            prop_name: label(&facts.prop_name),
            operator: label(&facts.operator),
            regex,
            roots: facts.top_level.iter().map(|(id, _)| *id).collect(),
        }
    }

    fn children_of(&self, id: NodeId) -> &[NodeId] {
        self.children.get(id as usize).map_or(&[], Vec::as_slice)
    }

    fn kind_of(&self, id: NodeId) -> &'static str {
        self.kind.get(id as usize).copied().unwrap_or_default()
    }

    fn ident_of(&self, id: NodeId) -> Option<&str> {
        self.ident.get(id as usize).and_then(|slot| slot.as_deref())
    }

    fn str_lit_of(&self, id: NodeId) -> Option<&str> {
        self.str_lit
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    fn num_lit_of(&self, id: NodeId) -> Option<&str> {
        self.num_lit
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    fn bool_lit_of(&self, id: NodeId) -> Option<bool> {
        self.bool_lit.get(id as usize).copied().flatten()
    }

    fn prop_name_of(&self, id: NodeId) -> Option<&str> {
        self.prop_name
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    fn operator_of(&self, id: NodeId) -> Option<&str> {
        self.operator
            .get(id as usize)
            .and_then(|slot| slot.as_deref())
    }

    fn regex_of(&self, id: NodeId) -> Option<(&str, &str)> {
        self.regex
            .get(id as usize)
            .and_then(|slot| slot.as_ref())
            .map(|(exp, flags)| (&**exp, &**flags))
    }
}

/// `EXPR` (bare or named) or bare `ANYTHING`: a single-node hole in **expression**
/// position (an `Ident`). Mirrors `holes.rs::expression_hole_name`.
fn is_expr_single_hole(name: &str) -> bool {
    hole_name_for(name, EXPR_HOLE_KEYWORD).is_some() || name == ANYTHING_HOLE_KEYWORD
}

/// `STMT` (bare or named, but not the `STMT_LIST` run hole) or bare `ANYTHING`: a
/// single-node hole in **statement** position. Mirrors `holes.rs::statement_hole_name`
/// with the `STMT_LIST`-wins-first precedence.
fn is_stmt_single_hole(name: &str) -> bool {
    name == ANYTHING_HOLE_KEYWORD
        || (hole_name_for(name, STMT_LIST_HOLE_KEYWORD).is_none()
            && hole_name_for(name, STMT_HOLE_KEYWORD).is_some())
}

/// A single-node hole borne by `node`, parse-position polymorphic: an expression
/// `Ident` (`EXPR`/`ANYTHING`), a binding `BindingIdent` pattern (`ANYTHING` only,
/// mirroring `is_anything_pat_hole`), or an expression-statement `ExprStmt`
/// (`STMT`/`ANYTHING`, matching any statement kind). Anonymous match-any
/// semantics; cross-occurrence equality of *named* single-node holes (`EXPR_x`)
/// is deferred (the equality-hole rung) and differential-gated.
fn is_single_node_hole(index: &Index, node: NodeId) -> bool {
    match index.kind_of(node) {
        "Ident" => index.ident_of(node).is_some_and(is_expr_single_hole),
        "BindingIdent" => index
            .ident_of(node)
            .is_some_and(|n| n == ANYTHING_HOLE_KEYWORD),
        "ExprStmt" => {
            let kids = index.children_of(node);
            kids.len() == 1
                && index.kind_of(kids[0]) == "Ident"
                && index.ident_of(kids[0]).is_some_and(is_stmt_single_hole)
        }
        _ => false,
    }
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
        .ident_of(node)
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
        // A `SwitchCase`'s body is a statement list too; its leading `case` test
        // (when present) is a non-carrier, so it falls out as an anchored-left
        // fixed segment under the same placement.
        "Block" | "SwitchCase" => {
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
                    && index.prop_name_of(kids[0]).is_some_and(|name| {
                        name == CLASS_REST_HOLE_KEYWORD || name == ANYTHING_HOLE_KEYWORD
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
                        .ident_of(kids[0])
                        .is_some_and(|name| name == CASE_REST_HOLE_KEYWORD)
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

/// The regex pattern of a well-formed `STR_LITERAL_MATCHING_RE("re")` predicate
/// at `node`: a `Call` of exactly the predicate callee with one string-literal
/// argument. Mirrors `holes.rs::string_literal_regex_pattern`. The predicate
/// matches a string-literal subject whose value matches `re`, not by structure.
fn regex_predicate_pattern(index: &Index, node: NodeId) -> Option<&str> {
    if index.kind_of(node) != "Call" {
        return None;
    }
    let kids = index.children_of(node);
    let [callee, arg] = kids else {
        return None;
    };
    let is_predicate = index
        .ident_of(*callee)
        .is_some_and(|name| name == STRING_LITERAL_REGEX_PREDICATE);
    (is_predicate && index.kind_of(*arg) == "StrLit")
        .then(|| index.str_lit_of(*arg))
        .flatten()
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

    // Single-node hole (expression / pattern / statement position): matches any
    // one subtree, before the kind check so a `STMT;`/`ANYTHING` needle can match
    // a different-kind subject. (Unhandled constructs — the regex predicate,
    // misplaced run-hole keywords — are rejected up front in [`matches`], before
    // structural matching can mask them, so they never reach here.)
    if is_single_node_hole(needle, nid) {
        return Ok(true);
    }

    // `STR_LITERAL_MATCHING_RE("re")` predicate: matches a string-literal subject
    // whose value matches `re` (an invalid pattern matches nothing), not by
    // structure. Checked before the kind comparison (the needle is a `Call`, the
    // subject a `StrLit`). Mirrors `holes.rs::string_literal_matches_regex`.
    if let Some(pattern) = regex_predicate_pattern(needle, nid) {
        return Ok(subject.kind_of(sid) == "StrLit"
            && subject
                .str_lit_of(sid)
                .is_some_and(|value| Regex::new(pattern).is_ok_and(|re| re.is_match(value))));
    }

    // Structural equality: kind, then non-identifier labels (always exact),
    // then the identifier label (exact or alpha-bound), then children.
    if nkind != subject.kind_of(sid) {
        return Ok(false);
    }
    if needle.str_lit_of(nid) != subject.str_lit_of(sid)
        || needle.num_lit_of(nid) != subject.num_lit_of(sid)
        || needle.bool_lit_of(nid) != subject.bool_lit_of(sid)
        || needle.prop_name_of(nid) != subject.prop_name_of(sid)
        || needle.operator_of(nid) != subject.operator_of(sid)
        || needle.regex_of(nid) != subject.regex_of(sid)
    {
        return Ok(false);
    }
    match (needle.ident_of(nid), subject.ident_of(sid)) {
        (Some(n), Some(s)) => {
            let consistent = match mode {
                Mode::Exact => n == s,
                // A binding identifier (declaration) shadows within its frame; a
                // reference resolves against the visible scope stack.
                Mode::AlphaAll if nkind == "BindingIdent" => bindings.match_binding(n, s),
                Mode::AlphaAll => bindings.match_ref(n, s),
            };
            if !consistent {
                return Ok(false);
            }
        }
        (None, None) => {}
        _ => return Ok(false),
    }

    // A function/arrow/constructor/setter/catch node scopes its params + body, so
    // same-spelled locals in sibling scopes stay independent (alpha shadowing).
    if mode == Mode::AlphaAll && introduces_alpha_scope(nkind) {
        bindings.push_scope();
        let result = match_children(needle, nid, nkind, subject, sid, mode, bindings);
        bindings.pop_scope();
        return result;
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

/// The var-decl node of a single var-decl-statement's facts, paired with the
/// root wrapper kind so the caller can enforce wrapper symmetry (a plain `const`
/// statement must not match an `export const`). `None` if the statement is not a
/// (possibly exported) variable declaration.
fn var_decl_node(index: &Index) -> Option<(&'static str, NodeId)> {
    let &root = index.roots.first()?;
    match index.kind_of(root) {
        "VarDecl" => Some(("VarDecl", root)),
        "ExportDecl" => {
            let &kid = index.children_of(root).first()?;
            (index.kind_of(kid) == "VarDecl").then_some(("ExportDecl", kid))
        }
        _ => None,
    }
}

/// Greedy-leftmost alignment of a needle var-decl's declarators (possibly
/// carrying `DECLARATORS` run holes) onto a subject var-decl's declarators,
/// recording for each needle declarator the subject declarator it placed onto.
/// Mirrors `matcher::match_var_declarator_slice_with_alignment`: a hole-free
/// needle aligns 1:1 (lengths must match); otherwise the non-hole declarators
/// partition into maximal fixed segments placed as an ordered subsequence with
/// gaps. `None` when no placement matches.
fn align_var_declarators(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<Option<Vec<Option<usize>>>, Unsupported> {
    let mut alignment = vec![None; ndecls.len()];
    let is_hole = |d: NodeId| is_run_hole_carrier(needle, "VarDecl", d);
    if !ndecls.iter().any(|&d| is_hole(d)) {
        if ndecls.len() != sdecls.len() {
            return Ok(None);
        }
        for (idx, (&nd, &sd)) in ndecls.iter().zip(sdecls).enumerate() {
            if !homo(needle, nd, subject, sd, mode, bindings)? {
                return Ok(None);
            }
            alignment[idx] = Some(idx);
        }
        return Ok(Some(alignment));
    }
    let mut segments: Vec<(usize, usize)> = Vec::new();
    let mut idx = 0;
    while idx < ndecls.len() {
        if is_hole(ndecls[idx]) {
            idx += 1;
            continue;
        }
        let start = idx;
        while idx < ndecls.len() && !is_hole(ndecls[idx]) {
            idx += 1;
        }
        segments.push((start, idx - start));
    }
    // An all-holes declarator list pins nothing (no positions to align).
    if segments.is_empty() {
        return Ok(Some(alignment));
    }
    let anchored_left = !is_hole(ndecls[0]);
    let anchored_right = !is_hole(ndecls[ndecls.len() - 1]);
    let placed = place_declarator_segments(
        needle,
        ndecls,
        subject,
        sdecls,
        &segments,
        anchored_left,
        anchored_right,
        0,
        0,
        mode,
        bindings,
        &mut alignment,
    )?;
    Ok(placed.then_some(alignment))
}

/// `place_segments` for declarator lists, additionally recording the chosen
/// alignment (and rolling it back alongside `Bindings` on a failed placement).
/// Mirrors `matcher::place_var_declarator_segments`.
#[allow(clippy::too_many_arguments)]
fn place_declarator_segments(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    segments: &[(usize, usize)],
    anchored_left: bool,
    anchored_right: bool,
    seg_idx: usize,
    cand_min: usize,
    mode: Mode,
    bindings: &mut Bindings,
    alignment: &mut [Option<usize>],
) -> Result<bool, Unsupported> {
    let Some(&(needle_start, seg_len)) = segments.get(seg_idx) else {
        return Ok(true);
    };
    let remaining: usize = segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = sdecls.len().checked_sub(remaining) else {
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
        let bindings_snapshot = bindings.clone();
        let alignment_snapshot = alignment.to_vec();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            if !homo(
                needle,
                ndecls[needle_start + offset],
                subject,
                sdecls[start + offset],
                mode,
                bindings,
            )? {
                segment_ok = false;
                break;
            }
            alignment[needle_start + offset] = Some(start + offset);
        }
        if segment_ok
            && place_declarator_segments(
                needle,
                ndecls,
                subject,
                sdecls,
                segments,
                anchored_left,
                anchored_right,
                seg_idx + 1,
                start + seg_len,
                mode,
                bindings,
                alignment,
            )?
        {
            return Ok(true);
        }
        *bindings = bindings_snapshot;
        alignment.copy_from_slice(&alignment_snapshot);
    }
    Ok(false)
}

/// Match two var-decl statements and, on success, return the greedy-leftmost
/// declarator alignment (needle declarator index → subject declarator index,
/// `None` for a `DECLARATORS`-hole-absorbed needle position); `None` if they do
/// not match. `prebind` pins one needle identifier to one subject identifier
/// before matching (alpha-mode coupling, mirroring `prebind_alpha_sym`), so the
/// caller can force the target binding's identity — a no-op under `Exact`. This
/// composes the whole-statement var-decl match (wrapper symmetry, `var`/`let`/
/// `const` kind, declarator alignment) with production's
/// `var_declarator_alignment`: a `Some` result *is* a faithful match. Fail-
/// closed on an unsupported needle construct.
pub fn var_declarator_alignment(
    needle: &ChunkFacts,
    subject: &ChunkFacts,
    mode: Mode,
    prebind: Option<(&str, &str)>,
) -> Result<Option<Vec<Option<usize>>>, Unsupported> {
    var_declarator_alignment_indexed(&Index::build(needle), &Index::build(subject), mode, prebind)
}

/// Like [`var_declarator_alignment`], but over **prebuilt** indices, so the
/// declarator-hole resolver can reuse cached body indices across the
/// owner × candidate-binding inner loop instead of rebuilding both indices on
/// every alignment attempt.
pub fn var_declarator_alignment_indexed(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    prebind: Option<(&str, &str)>,
) -> Result<Option<Vec<Option<usize>>>, Unsupported> {
    if let Some(reason) = unsupported_needle_construct(needle) {
        return Err(Unsupported { reason });
    }
    let (Some((nwrap, nvd)), Some((swrap, svd))) = (var_decl_node(needle), var_decl_node(subject))
    else {
        return Ok(None);
    };
    // Wrapper symmetry (plain vs exported) and `var`/`let`/`const` kind: the
    // node-level structure the declarator alignment does not itself compare.
    if nwrap != swrap || needle.operator_of(nvd) != subject.operator_of(svd) {
        return Ok(None);
    }
    let mut bindings = Bindings::default();
    if let Some((n, s)) = prebind
        && !bindings.match_binding(n, s)
    {
        return Ok(None);
    }
    align_var_declarators(
        needle,
        needle.children_of(nvd),
        subject,
        subject.children_of(svd),
        mode,
        &mut bindings,
    )
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
/// construct this matcher does not faithfully handle: a run-hole keyword not
/// consumed as a list carrier (a misplaced hole), or a `STR_LITERAL_MATCHING_RE`
/// occurrence that is not a well-formed predicate callee. A token is consumed
/// iff it lies inside a run-hole carrier subtree or is a predicate's callee/arg,
/// which mirrors exactly what the matcher's structural rules absorb.
fn unsupported_needle_construct(index: &Index) -> Option<&'static str> {
    let mut consumed: HashSet<NodeId> = HashSet::new();
    for (parent, kids) in index.children.iter().enumerate() {
        let parent_kind = index.kind_of(parent as NodeId);
        for &child in kids {
            if is_run_hole_carrier(index, parent_kind, child) {
                collect_subtree(index, child, &mut consumed);
            }
        }
    }
    // A well-formed predicate consumes its own callee + string-literal argument
    // (the matcher handles the whole `Call`, never the bare callee identifier).
    for node in 0..index.kind.len() as NodeId {
        if regex_predicate_pattern(index, node).is_some() {
            consumed.extend(index.children_of(node));
        }
    }
    for node in 0..index.kind.len() as NodeId {
        let Some(name) = index.ident_of(node).or_else(|| index.prop_name_of(node)) else {
            continue;
        };
        if name == STRING_LITERAL_REGEX_PREDICATE && !consumed.contains(&node) {
            return Some("malformed STR_LITERAL_MATCHING_RE predicate");
        }
        if is_run_hole_keyword(name) && !consumed.contains(&node) {
            return Some("run-hole keyword outside a list position");
        }
    }
    None
}

/// True iff the needle (one top-level statement) structurally matches the
/// subject statement under `mode`, with single-node and run holes as described
/// in the module docs. Errors (fail-closed) on un-lowered constructs (the regex
/// predicate, a misplaced run hole) rather than returning a weaker match. Both
/// inputs are single-statement `ChunkFacts`.
pub fn matches(needle: &ChunkFacts, subject: &ChunkFacts, mode: Mode) -> Result<bool, Unsupported> {
    matches_indexed(&Index::build(needle), &Index::build(subject), mode)
}

/// Like [`matches`], but over **prebuilt** indices, so a caller resolving many
/// needles against one chunk body builds each subject `Index` once (cached in
/// `ChunkResolver`) rather than per `(needle, subject)` pair.
pub fn matches_indexed(needle: &Index, subject: &Index, mode: Mode) -> Result<bool, Unsupported> {
    if let Some(reason) = unsupported_needle_construct(needle) {
        return Err(Unsupported { reason });
    }
    let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
        return Ok(false);
    };
    let mut bindings = Bindings::default();
    homo(needle, n_root, subject, s_root, mode, &mut bindings)
}

/// A **sound** per-candidate prefilter for the single-statement `matches` scan:
/// the root node kind a subject must share for `matches` to possibly return true.
/// `Some(kind)` ⟹ every subject whose root kind differs is a guaranteed
/// `Ok(false)` (it is exactly the `nkind != subject kind` gate in [`homo`], which
/// runs after the hole/predicate special-cases), so the caller may skip it
/// without changing any verdict. `None` ⟹ no prefilter applies — the needle root
/// is a single-node hole or the regex predicate, which match subjects of *other*
/// kinds — so the caller must run the full match. Mirrors production's
/// `no_wildcard_shape_prefilter` role for the fact matcher.
pub fn needle_root_kind_prefilter(needle: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(needle);
    let &root = index.roots.first()?;
    if is_single_node_hole(&index, root) || regex_predicate_pattern(&index, root).is_some() {
        return None;
    }
    subject_root_kind(needle)
}

/// The root (top-level) node kind of a single-statement `ChunkFacts`, for caching
/// subject root kinds against [`needle_root_kind_prefilter`]. `node_kind` is dense
/// in node id by construction, so this is an O(1) lookup of the `top_level` node.
pub fn subject_root_kind(facts: &ChunkFacts) -> Option<&'static str> {
    let (root_id, _) = facts.top_level.first()?;
    facts
        .node_kind
        .get(*root_id as usize)
        .map(|(_, kind)| *kind)
}

/// The init-expression node kind of a single-declarator var-decl statement's
/// first declarator (the `VarDeclarator` child at ordinal 1), or `None` when
/// there is no init. A **sound secondary prefilter** for the var-decl member
/// scan: a subject declarator can only match the needle declarator if their init
/// kinds agree (the root-kind prefilter does not discriminate — every var-decl
/// shares kind `VarDecl`). `None` returns fall through to the full match.
pub fn var_declarator_init_kind(facts: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(facts);
    init_node_of(&index).map(|init| index.kind_of(init))
}

/// Like [`var_declarator_init_kind`], but `None` when the needle's init is a
/// single-node hole (`EXPR`/`ANYTHING`) — which matches any subject init, so no
/// prefilter applies and the caller must run the full match.
pub fn needle_var_declarator_init_kind_prefilter(needle: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(needle);
    let init = init_node_of(&index)?;
    (!is_single_node_hole(&index, init)).then(|| index.kind_of(init))
}

/// The init node of a single-declarator var-decl statement: `var_decl_node` →
/// first `VarDeclarator` → its `init` child (declarator child ordinal 1).
fn init_node_of(index: &Index) -> Option<NodeId> {
    let (_, var_decl) = var_decl_node(index)?;
    let &declarator = index.children_of(var_decl).first()?;
    index.children_of(declarator).get(1).copied()
}

/// True iff a needle root (one statement's facts) is a module-level `STMT_LIST`
/// hole — an expression statement whose sole child is the keyword. These
/// separate the needle's fixed segments at the top level (mirrors
/// `holes.rs::module_item_list_hole_name`).
fn is_module_stmt_list_hole(index: &Index) -> bool {
    let Some(&root) = index.roots.first() else {
        return false;
    };
    index.kind_of(root) == "ExprStmt" && {
        let kids = index.children_of(root);
        kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
    }
}

/// Match a multi-statement needle (each element one statement's facts, in source
/// order) against a chunk body (each element one top-level statement's facts, in
/// body order) as an ordered subsequence with module-level `STMT_LIST` holes,
/// **enumerating every alignment** (needle-index → body-index, `None` for a hole
/// position). Mirrors `body_search::find_matching_body_group_alignments` /
/// `place_module_item_segments` over facts: never anchored at module level, and
/// an all-holes needle pins nothing (no alignment). Fail-closed on an unsupported
/// non-hole needle statement.
pub fn match_top_level_sequence(
    needles: &[ChunkFacts],
    subject_items: &[ChunkFacts],
    mode: Mode,
) -> Result<Vec<Vec<Option<usize>>>, Unsupported> {
    let needle_idx: Vec<Index> = needles.iter().map(Index::build).collect();
    let subject_idx: Vec<Index> = subject_items.iter().map(Index::build).collect();
    match_top_level_sequence_indexed(&needle_idx, &subject_idx, mode)
}

/// Like [`match_top_level_sequence`], but over **prebuilt** subject indices so a
/// caller resolving many multi-statement needles against one chunk body reuses
/// the cached body `Index`es instead of rebuilding them all per call.
pub fn match_top_level_sequence_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    mode: Mode,
) -> Result<Vec<Vec<Option<usize>>>, Unsupported> {
    let mut segments: Vec<(usize, usize)> = Vec::new();
    let mut i = 0;
    while i < needle_idx.len() {
        if is_module_stmt_list_hole(&needle_idx[i]) {
            i += 1;
            continue;
        }
        // A fixed (non-hole) needle statement must be faithfully supported.
        if let Some(reason) = unsupported_needle_construct(&needle_idx[i]) {
            return Err(Unsupported { reason });
        }
        let start = i;
        while i < needle_idx.len() && !is_module_stmt_list_hole(&needle_idx[i]) {
            if let Some(reason) = unsupported_needle_construct(&needle_idx[i]) {
                return Err(Unsupported { reason });
            }
            i += 1;
        }
        segments.push((start, i - start));
    }
    // An all-holes needle pins nothing — no alignment (matches the matcher).
    if segments.is_empty() {
        return Ok(Vec::new());
    }
    let mut alignment = vec![None; needle_idx.len()];
    let mut matches = Vec::new();
    let mut bindings = Bindings::default();
    place_top_level(
        needle_idx,
        subject_idx,
        &segments,
        0,
        0,
        mode,
        &mut bindings,
        &mut alignment,
        &mut matches,
    )?;
    Ok(matches)
}

/// Resolve a **contiguous** multi-statement needle whose item at `target_idx` is
/// a single-declarator var-decl, mirroring production's
/// `find_matching_target_binding_ranges_with_single_declarator` /
/// `SingleDeclaratorTargetWindow`. For each body window of length
/// `needles.len()`, thread one `Bindings` through the items in source order:
/// every non-target item matches as a whole statement, and at `target_idx` the
/// match branches over the window item's declarators (each matched via the
/// single needle declarator, so a target inside a multi-declarator owner is
/// found). Returns `(target_body_idx, subject_declarator_idx)` for every
/// full-window match — the caller reads the binding from that declarator and
/// applies owner-level categoricity. This path is fixed-window (no module-level
/// `STMT_LIST` holes); a hole-bearing needle item fails closed via the per-item
/// unsupported scan, matching production (which rejects it).
pub fn match_single_declarator_target_windows(
    needles: &[ChunkFacts],
    subject_items: &[ChunkFacts],
    target_idx: usize,
    mode: Mode,
) -> Result<Vec<(usize, usize)>, Unsupported> {
    let needle_idx: Vec<Index> = needles.iter().map(Index::build).collect();
    let subject_idx: Vec<Index> = subject_items.iter().map(Index::build).collect();
    match_single_declarator_target_windows_indexed(&needle_idx, &subject_idx, target_idx, mode)
}

/// Like [`match_single_declarator_target_windows`], but over **prebuilt** subject
/// indices so the caller reuses the cached chunk body indices instead of
/// rebuilding them all per selector.
pub fn match_single_declarator_target_windows_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    target_idx: usize,
    mode: Mode,
) -> Result<Vec<(usize, usize)>, Unsupported> {
    for needle in needle_idx {
        if let Some(reason) = unsupported_needle_construct(needle) {
            return Err(Unsupported { reason });
        }
    }
    let n = needle_idx.len();
    let mut matches = Vec::new();
    if n == 0 || n > subject_idx.len() {
        return Ok(matches);
    }
    for window_start in 0..=(subject_idx.len() - n) {
        let mut bindings = Bindings::default();
        let mut found: Vec<usize> = Vec::new();
        match_declarator_target_window(
            needle_idx,
            subject_idx,
            window_start,
            target_idx,
            0,
            mode,
            &mut bindings,
            None,
            &mut found,
        )?;
        matches.extend(
            found
                .into_iter()
                .map(|decl| (window_start + target_idx, decl)),
        );
    }
    Ok(matches)
}

/// One window's recursive item-by-item match for
/// [`match_single_declarator_target_windows`], threading `Bindings` in source
/// order and branching at `target_idx` over the window item's declarators. The
/// chosen declarator index is carried to the window end (when it is recorded),
/// so a binding is reported only for a full-window match. Mirrors
/// `SingleDeclaratorTargetWindow::match_items`.
#[allow(clippy::too_many_arguments)]
fn match_declarator_target_window(
    needles: &[Index],
    subjects: &[Index],
    window_start: usize,
    target_idx: usize,
    item_idx: usize,
    mode: Mode,
    bindings: &mut Bindings,
    chosen_decl: Option<usize>,
    found: &mut Vec<usize>,
) -> Result<(), Unsupported> {
    if item_idx == needles.len() {
        if let Some(decl) = chosen_decl {
            found.push(decl);
        }
        return Ok(());
    }
    let needle = &needles[item_idx];
    let subject = &subjects[window_start + item_idx];
    let snapshot = bindings.clone();
    if item_idx == target_idx {
        let (Some((nwrap, nvd)), Some((swrap, svd))) =
            (var_decl_node(needle), var_decl_node(subject))
        else {
            return Ok(());
        };
        if nwrap != swrap || needle.operator_of(nvd) != subject.operator_of(svd) {
            return Ok(());
        }
        let [needle_decl] = needle.children_of(nvd) else {
            return Ok(());
        };
        for (decl_idx, &subject_decl) in subject.children_of(svd).iter().enumerate() {
            *bindings = snapshot.clone();
            if homo(needle, *needle_decl, subject, subject_decl, mode, bindings)? {
                match_declarator_target_window(
                    needles,
                    subjects,
                    window_start,
                    target_idx,
                    item_idx + 1,
                    mode,
                    bindings,
                    Some(decl_idx),
                    found,
                )?;
            }
        }
        *bindings = snapshot;
    } else {
        let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
            return Ok(());
        };
        if homo(needle, n_root, subject, s_root, mode, bindings)? {
            match_declarator_target_window(
                needles,
                subjects,
                window_start,
                target_idx,
                item_idx + 1,
                mode,
                bindings,
                chosen_decl,
                found,
            )?;
        }
        *bindings = snapshot;
    }
    Ok(())
}

/// Recursive ordered-subsequence placement of the needle's fixed segments into
/// the chunk body, enumerating every alignment with `Bindings` + alignment
/// snapshot/restore. Mirrors `place_module_item_segments`.
#[allow(clippy::too_many_arguments)]
fn place_top_level(
    needle_idx: &[Index],
    subject_idx: &[Index],
    segments: &[(usize, usize)],
    seg_idx: usize,
    cand_min: usize,
    mode: Mode,
    bindings: &mut Bindings,
    alignment: &mut [Option<usize>],
    matches: &mut Vec<Vec<Option<usize>>>,
) -> Result<(), Unsupported> {
    let Some(&(needle_start, seg_len)) = segments.get(seg_idx) else {
        matches.push(alignment.to_vec());
        return Ok(());
    };
    let remaining: usize = segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = subject_idx.len().checked_sub(remaining) else {
        return Ok(());
    };
    for start in cand_min..=latest_start {
        let bindings_snapshot = bindings.clone();
        let alignment_snapshot = alignment.to_vec();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            let needle = &needle_idx[needle_start + offset];
            let subject = &subject_idx[start + offset];
            let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first())
            else {
                segment_ok = false;
                break;
            };
            if !homo(needle, n_root, subject, s_root, mode, bindings)? {
                segment_ok = false;
                break;
            }
            alignment[needle_start + offset] = Some(start + offset);
        }
        if segment_ok {
            place_top_level(
                needle_idx,
                subject_idx,
                segments,
                seg_idx + 1,
                start + seg_len,
                mode,
                bindings,
                alignment,
                matches,
            )?;
        }
        *bindings = bindings_snapshot;
        alignment.copy_from_slice(&alignment_snapshot);
    }
    Ok(())
}
