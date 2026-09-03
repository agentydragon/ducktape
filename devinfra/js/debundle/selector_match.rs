//! P2 (matching over the facts): a structural homomorphism from a selector
//! needle's `chunk_facts` projection onto a candidate's, anchored at the
//! top-level statement. This is the Datalog-side matcher the resolver will use;
//! it operates over the AST-facts EDB, never by re-walking ASTs.
//!
//! Faithful subset: exact- and alpha-identifier structure with
//! **expression-position single-node holes** (`ANYTHING` / `EXPR` / `STMT`
//! matching any one subtree) and **variable-length run holes** (`STMT_LIST` /
//! `ARGS` / `ARRAY_ELEMENTS` / `CASE_REST` / `DECLARATORS`) matched
//! as an ordered subsequence with gaps. Run-hole placement partitions the needle
//! list into maximal fixed segments at the carriers, placed greedy-leftmost with
//! `Bindings` snapshot/restore. The
//! `STR_LITERAL_MATCHING_RE("re")` predicate matches a string-literal subject
//! whose value matches `re`. Fail-closed only on what is genuinely unhandled: a
//! **misplaced** run-hole keyword (one reaching the node matcher rather than
//! being consumed as a list carrier) or a malformed predicate returns
//! [`Unsupported`] rather than a weaker (under-constraining) match. Match
//! semantics across identifier modes, holes, and declarator alignment are pinned
//! by `selector_match_differential_test`.
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

use chunk_facts::{ChunkFacts, NodeId, NodeKind};
use regex::Regex;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, ARRAY_ELEMENTS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, STMT_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
    STRING_LITERAL_REGEX_PREDICATE, hole_name_for, labeled_hole_name_for,
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

/// One lexical frame's bijective needle↔subject identifier map.
#[derive(Default, Clone)]
struct AlphaScope {
    forward: HashMap<String, String>,
    backward: HashMap<String, String>,
}

/// Scope-aware bijective needle↔subject identifier binding accumulated during
/// alpha matching — a stack of lexical frames. References (`match_ref`) resolve
/// against the visible stack
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
    /// (innermost-out), then resolve the unbound case per `mode`. A prebound
    /// mapping is honored in **either** mode — that is how a `target_binding`
    /// prebind forces one needle name onto one subject name even under `Exact`
    /// (where the unbound fallback is exact spelling, not a fresh alpha pair).
    fn match_ref(&mut self, needle: &str, subject: &str, mode: Mode) -> bool {
        for scope in self.scopes.iter().rev() {
            if let Some(mapped) = scope.forward.get(needle) {
                return mapped == subject;
            }
            if scope.backward.contains_key(subject) {
                return false;
            }
        }
        self.resolve_unbound(needle, subject, mode)
    }

    /// Match an identifier **binding** (declaration): consult only the current
    /// frame, so it may shadow an outer binding of the same spelling.
    fn match_binding(&mut self, needle: &str, subject: &str, mode: Mode) -> bool {
        let scope = self.scopes.last().expect("always a root scope");
        if let Some(mapped) = scope.forward.get(needle) {
            return mapped == subject;
        }
        if scope.backward.contains_key(subject) {
            return false;
        }
        self.resolve_unbound(needle, subject, mode)
    }

    /// Resolve a needle↔subject pair that neither side has mapped yet: under
    /// `AlphaAll` bind them as a fresh alpha pair; under `Exact` require identical
    /// spellings (no new binding). The pre-binding gate (`match_ref`/`match_binding`)
    /// already short-circuited a known mapping, so this only sees genuinely-free
    /// names.
    fn resolve_unbound(&mut self, needle: &str, subject: &str, mode: Mode) -> bool {
        match mode {
            Mode::Exact => needle == subject,
            Mode::AlphaAll => {
                let scope = self.scopes.last_mut().expect("always a root scope");
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

    /// Force a needle↔subject mapping in the current frame before matching (the
    /// `target_binding` alpha coupling). Honors an existing mapping; fails if
    /// either side is already mapped incompatibly. Used in both identifier modes.
    fn prebind(&mut self, needle: &str, subject: &str) -> bool {
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

/// Node kinds that introduce a lexical scope for their params + body:
/// function/arrow bodies, catch clauses, setter and constructor params.
fn introduces_alpha_scope(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::Function
            | NodeKind::AsyncFunction
            | NodeKind::GeneratorFunction
            | NodeKind::AsyncGeneratorFunction
            | NodeKind::Arrow
            | NodeKind::AsyncArrow
            | NodeKind::Constructor
            | NodeKind::Setter
            | NodeKind::Catch
    )
}

/// A node-indexed view of one statement's `ChunkFacts`, owning its string labels
/// (`Box<str>`) so it can be **built once and cached** — e.g. one per chunk body
/// item in `ChunkResolver` — and reused across many needle matches, instead of
/// rebuilt per `(needle, subject)` pair. `kind` holds each node's [`NodeKind`]
/// (copied from the facts, themselves `Copy`); the value labels are copied out so
/// the index outlives the borrowed facts.
pub struct Index {
    // Node ids are dense (`0..node_count`), so every relation is a `Vec` indexed
    // by node id — array access instead of hashing on the per-node-match hot path.
    kind: Vec<NodeKind>,
    children: Vec<Vec<NodeId>>,
    ident: Vec<Option<Box<str>>>,
    str_lit: Vec<Option<Box<str>>>,
    num_lit: Vec<Option<Box<str>>>,
    bool_lit: Vec<Option<bool>>,
    prop_name: Vec<Option<Box<str>>>,
    operator: Vec<Option<Box<str>>>,
    regex: Vec<Option<(Box<str>, Box<str>)>>,
    /// A `Class` node's superclass expression node (the `extends` clause), kept
    /// as a separate relation because it is not a child member. `homo` compares
    /// it for `Class` nodes (both present and matched, or both absent).
    super_class: Vec<Option<NodeId>>,
    /// `STR_LITERAL_MATCHING_RE(...)` predicate node -> its compiled pattern,
    /// built once when the index is built (only needle indices carry predicates;
    /// the marker keyword never appears in real subject source). `homo` looks the
    /// regex up instead of recompiling it per candidate — the regex compile, not
    /// the match, dominated the CSS-module class-name member scans.
    predicate_regex: HashMap<NodeId, Regex>,
    roots: Vec<NodeId>,
}

/// A string-literal regex predicate the needle requires a matching subject to
/// satisfy. Private fields keep `regex::Regex` out of downstream crates' direct
/// API surface; callers can ask only the two facts useful for safe pruning.
pub struct StringLiteralPredicate<'a> {
    regex: Option<&'a Regex>,
    literal_prefix: Option<Box<str>>,
}

impl StringLiteralPredicate<'_> {
    /// A leading literal prefix every matching string value must start with, when
    /// the pattern has a conservatively recognized anchored prefix. `None` means
    /// no prefix-based pruning is safe for this predicate.
    pub fn literal_prefix(&self) -> Option<&str> {
        self.literal_prefix.as_deref()
    }

    /// True iff `value` satisfies the compiled predicate. An invalid regex pattern
    /// has no compiled regex and therefore matches nothing, exactly as `homo` does.
    pub fn is_match(&self, value: &str) -> bool {
        self.regex.is_some_and(|regex| regex.is_match(value))
    }

    /// Whether the predicate pattern compiled successfully. Invalid patterns
    /// match no subject, so a prefilter may soundly return no candidates.
    pub fn is_valid(&self) -> bool {
        self.regex.is_some()
    }
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
        // fails to compile is simply absent — `homo` then matches nothing, as the
        // per-candidate `Regex::new(...).is_ok_and(...)` did before.
        for node in 0..n as NodeId {
            if let Some(pattern) = regex_predicate_pattern(&index, node)
                && let Ok(compiled) = Regex::new(pattern)
            {
                index.predicate_regex.insert(node, compiled);
            }
        }
        index
    }

    fn children_of(&self, id: NodeId) -> &[NodeId] {
        self.children.get(id as usize).map_or(&[], Vec::as_slice)
    }

    fn kind_of(&self, id: NodeId) -> NodeKind {
        self.kind[id as usize]
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

    fn super_class_of(&self, id: NodeId) -> Option<NodeId> {
        self.super_class.get(id as usize).copied().flatten()
    }
}

/// Read surface over a built [`Index`] for the fact-based near-miss diagnostics
/// (`source_match::fact_near_miss`), which instruments this matcher's own
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

/// Match two arbitrary sub-nodes (`nid` in `needle`, `sid` in `subject`) with a
/// fresh [`Bindings`], the per-node match oracle the near-miss descent uses to
/// decide whether one aligned pair (a class member, a declarator) matches. This
/// is the *same* [`homo`] relation the whole-statement match runs, just rooted at
/// a sub-node instead of the top-level statement. Sound to call on a sub-node of a
/// needle that already passed [`matches_indexed`]/[`var_declarator_alignment_indexed`]:
/// the only [`Unsupported`] sources (a misplaced run-hole keyword, a malformed
/// predicate) are whole-needle properties already rejected up front, so a
/// sub-node descent of a probed needle never newly errors.
pub fn nodes_match(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
) -> Result<bool, Unsupported> {
    let mut bindings = Bindings::default();
    homo(needle, nid, subject, sid, mode, &mut bindings)
}

/// True iff `node` is an `ANYTHING;` class-rest member hole under a `Class`
/// parent (a class field whose key is the exact keyword and which has no
/// initializer). The class-member near-miss scan skips these.
pub fn is_class_rest_member(index: &Index, node: NodeId) -> bool {
    is_run_hole_carrier(index, NodeKind::Class, node)
}

/// Greedy-leftmost in-order match of a needle var-decl's **pinned** (non-hole)
/// declarators onto a subject var-decl's declarators, returning the matched
/// `(needle_decl_idx, subject_decl_idx)` pairs in order — the diagnostic greedy
/// scan used by `fact_near_miss`'s declarator-alignment reasons (*not* the segment
/// placement of [`align_var_declarators`]). One [`Bindings`] is threaded across the
/// whole scan with snapshot/restore, so an alpha binding committed by an earlier
/// pin constrains a later one. `ndecls`/`sdecls` are the two `VarDecl` nodes'
/// declarator children (`index.children(var_decl_node)`).
pub fn pinned_declarator_matches_in_order(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    mode: Mode,
) -> Result<Vec<(usize, usize)>, Unsupported> {
    let mut bindings = Bindings::default();
    let mut subject_start = 0;
    let mut matches = Vec::new();
    for (needle_idx, &ndecl) in ndecls.iter().enumerate() {
        if is_run_hole_carrier(needle, NodeKind::VarDecl, ndecl) {
            continue;
        }
        let mut found = None;
        for (subject_idx, &sdecl) in sdecls.iter().enumerate().skip(subject_start) {
            let snapshot = bindings.clone();
            if homo(needle, ndecl, subject, sdecl, mode, &mut bindings)? {
                found = Some(subject_idx);
                break;
            }
            bindings = snapshot;
        }
        let Some(subject_idx) = found else {
            break;
        };
        matches.push((needle_idx, subject_idx));
        subject_start = subject_idx + 1;
    }
    Ok(matches)
}

/// True iff `node` is a `DECLARATORS` / `ANYTHING` declarator run-hole carrier
/// under a `VarDecl` parent (a declarator whose name binding is the keyword). The
/// pinned-declarator near-miss alignment skips these.
pub fn is_declarator_run_hole(index: &Index, node: NodeId) -> bool {
    is_run_hole_carrier(index, NodeKind::VarDecl, node)
}

/// `EXPR[_label]` or `ANYTHING[_label]`: a single-node hole in **expression**
/// position (an `Ident`). Labels are cosmetic and never bind.
fn is_expr_single_hole(name: &str) -> bool {
    hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
        || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
}

/// `STMT[_label]` or `ANYTHING[_label]`: a single-node hole in **statement**
/// position. Labels are cosmetic and never bind.
fn is_stmt_single_hole(name: &str) -> bool {
    hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
        || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
}

/// A single-node hole borne by `node`, parse-position polymorphic: an expression
/// `Ident` (`EXPR`/`ANYTHING`), a binding `BindingIdent` pattern (`ANYTHING` only,
/// mirroring `is_anything_pat_hole`), or an expression-statement `ExprStmt`
/// (`STMT`/`ANYTHING`, matching any statement kind). Suffixed labels are
/// readability-only and do not bind.
fn is_single_node_hole(index: &Index, node: NodeId) -> bool {
    match index.kind_of(node) {
        NodeKind::Ident => index.ident_of(node).is_some_and(is_expr_single_hole),
        NodeKind::BindingIdent => index
            .ident_of(node)
            .is_some_and(|n| hole_name_for(n, ANYTHING_HOLE_KEYWORD).is_some()),
        NodeKind::ExprStmt => {
            let kids = index.children_of(node);
            kids.len() == 1
                && index.kind_of(kids[0]) == NodeKind::Ident
                && index.ident_of(kids[0]).is_some_and(is_stmt_single_hole)
        }
        _ => false,
    }
}

const RUN_HOLE_KEYWORDS: [&str; 5] = [
    STMT_LIST_HOLE_KEYWORD,
    ARGS_HOLE_KEYWORD,
    ARRAY_ELEMENTS_HOLE_KEYWORD,
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
        .any(|kw| labeled_hole_name_for(name, kw).is_some())
}

/// Whether `name` is any hole/placeholder keyword (single-node or run). Used to
/// keep hole markers out of the invariant-token index: an `ANYTHING;` class-field
/// hole, for instance, projects to a `prop_name` fact, but it matches *absence*
/// of members, not a real `ANYTHING`-named property — indexing it would require
/// a token no real subject carries.
fn is_hole_keyword(name: &str) -> bool {
    [
        ANYTHING_HOLE_KEYWORD,
        EXPR_HOLE_KEYWORD,
        STMT_HOLE_KEYWORD,
        STMT_LIST_HOLE_KEYWORD,
        ARGS_HOLE_KEYWORD,
        ARRAY_ELEMENTS_HOLE_KEYWORD,
        DECLARATORS_HOLE_KEYWORD,
        CASE_REST_HOLE_KEYWORD,
    ]
    .iter()
    .any(|kw| labeled_hole_name_for(name, kw).is_some())
}

fn node_ident_hole(index: &Index, node: NodeId, keyword: &str) -> bool {
    index
        .ident_of(node)
        .is_some_and(|name| labeled_hole_name_for(name, keyword).is_some())
}

/// True iff `child` is a variable-length run-hole carrier in a list under a
/// parent of `parent_kind` — the per-list-type hole-carrier rules over facts. A
/// carrier is consumed by list placement and never visited by [`homo`].
fn is_run_hole_carrier(index: &Index, parent_kind: NodeKind, child: NodeId) -> bool {
    let ck = index.kind_of(child);
    match parent_kind {
        // `STMT_LIST;` — an expression statement whose sole child is the keyword.
        // A `SwitchCase`'s body is a statement list too; its leading `case` test
        // (when present) is a non-carrier, so it falls out as an anchored-left
        // fixed segment under the same placement.
        NodeKind::Block | NodeKind::SwitchCase => {
            ck == NodeKind::ExprStmt && {
                let kids = index.children_of(child);
                kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
            }
        }
        // `ARGS` — a bare identifier argument (the callee is split off before
        // this list, so any `ARGS` here is in argument position).
        NodeKind::Call | NodeKind::New | NodeKind::OptCall => {
            ck == NodeKind::Ident && node_ident_hole(index, child, ARGS_HOLE_KEYWORD)
        }
        // `ANYTHING` — a shorthand object-literal property.
        NodeKind::Object => {
            ck == NodeKind::Shorthand && node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD)
        }
        // `ANYTHING` in a destructuring pattern — a shorthand (no default)
        // destructure property.
        NodeKind::ObjectPat => {
            ck == NodeKind::PatAssign
                && index.children_of(child).is_empty()
                && node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD)
        }
        // `ARRAY_ELEMENTS` — a bare identifier array element (the keyword in
        // element position). Spread / elision elements project to other node kinds
        // (`Spread` / `Elision`), so a carrier is unambiguously the keyword
        // identifier. No `ANYTHING` form: `ANYTHING` in element position is a
        // single-element `EXPR`, not a run.
        NodeKind::Array => {
            ck == NodeKind::Ident && node_ident_hole(index, child, ARRAY_ELEMENTS_HOLE_KEYWORD)
        }
        // `ANYTHING;` — a class field, no initializer, whose key is the keyword.
        NodeKind::Class => {
            ck == NodeKind::ClassProp && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index
                        .prop_name_of(kids[0])
                        .is_some_and(|name| hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
            }
        }
        // `case CASE_REST[_label]:` — a switch clause, no body, whose sole child
        // is the keyword test. The optional label is cosmetic.
        NodeKind::Switch => {
            ck == NodeKind::SwitchCase && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index.ident_of(kids[0]).is_some_and(|name| {
                        labeled_hole_name_for(name, CASE_REST_HOLE_KEYWORD).is_some()
                    })
            }
        }
        // `const DECLARATORS` / `ANYTHING` — a declarator whose name binding is
        // the keyword.
        NodeKind::VarDecl => {
            ck == NodeKind::VarDeclarator && {
                let kids = index.children_of(child);
                !kids.is_empty()
                    && index.kind_of(kids[0]) == NodeKind::BindingIdent
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
fn list_prefix_len(parent_kind: NodeKind) -> usize {
    matches!(
        parent_kind,
        NodeKind::Call | NodeKind::New | NodeKind::OptCall | NodeKind::Switch
    ) as usize
}

/// The regex pattern of a well-formed `STR_LITERAL_MATCHING_RE("re")` predicate
/// at `node`: a `Call` of exactly the predicate callee with one string-literal
/// argument. Mirrors `holes.rs::string_literal_regex_pattern`. The predicate
/// matches a string-literal subject whose value matches `re`, not by structure.
fn regex_predicate_pattern(index: &Index, node: NodeId) -> Option<&str> {
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

/// A conservative required-prefix extractor for regex predicates. It recognizes
/// only `^literal...` forms where every emitted prefix byte is guaranteed to be
/// the start of every matched value. Unsupported escapes/metacharacters stop the
/// prefix rather than guessing; no prefix means "do not prune by this predicate".
fn regex_required_literal_prefix(pattern: &str) -> Option<Box<str>> {
    let rest = pattern.strip_prefix('^')?;
    if regex_contains_unescaped_alternation(rest) {
        return None;
    }
    let mut prefix = String::new();
    let mut chars = rest.chars();
    while let Some(ch) = chars.next() {
        match ch {
            '\\' => {
                let Some(escaped) = chars.next() else {
                    break;
                };
                if regex_escape_is_literal_char(escaped) {
                    prefix.push(escaped);
                } else {
                    break;
                }
            }
            '[' | '(' | ')' | '.' | '*' | '+' | '?' | '|' | '{' | '}' | '$' | '^' => break,
            literal => prefix.push(literal),
        }
    }
    (!prefix.is_empty()).then(|| prefix.into_boxed_str())
}

fn regex_contains_unescaped_alternation(pattern: &str) -> bool {
    let mut chars = pattern.chars();
    while let Some(ch) = chars.next() {
        match ch {
            '\\' => {
                chars.next();
            }
            '|' => return true,
            _ => {}
        }
    }
    false
}

fn regex_escape_is_literal_char(ch: char) -> bool {
    matches!(
        ch,
        '\\' | '.'
            | '+'
            | '*'
            | '?'
            | '('
            | ')'
            | '|'
            | '['
            | ']'
            | '{'
            | '}'
            | '^'
            | '$'
            | '#'
            | '&'
            | '-'
            | '~'
            | '/'
    )
}

/// The normalized view of a same-name object/destructure property — a property
/// whose source value is exactly its key identifier (`{ k }` or `{ k: k }`). Such
/// a property pins one stable key name plus one (possibly alpha-renamable) value
/// identifier, and the two surface forms (shorthand vs explicit) are equivalent,
/// so [`homo`] compares them through this view rather than by node kind.
struct ShorthandProperty<'a> {
    /// The property key — a stable source name, compared **exactly** even in alpha
    /// mode (a destructure shorthand key is a real property name, not a binding).
    key: &'a str,
    /// The value identifier: a reference for an object-literal property, the
    /// introduced binding for a destructure pattern property.
    value_ident: &'a str,
    /// True for a destructure pattern property (the value identifier is a
    /// **binding** → `match_binding`); false for an object-literal property (the
    /// value is a **reference** → `match_ref`).
    is_binding: bool,
}

/// View `node` as a same-name property (`{ k }` / `{ k: k }`) if it is one, for the
/// shorthand⟷explicit equivalence in [`homo`]. Covers the four fact node kinds the
/// two surface forms project to — object literal `Shorthand(k)` and
/// `KeyValue(PropName(k), Ident(k))`, destructure pattern `PatAssign(k)` (no
/// default) and `PatKeyValue(PropName(k), BindingIdent(k))`.
///
/// A `KeyValue`/`PatKeyValue` whose value is not a bare identifier (e.g.
/// `{ k: f() }`, `{ k: renamed }`) is *not* a same-name property and returns
/// `None`, falling through to structural matching. Hole carriers (`ANYTHING`
/// shorthands) are excluded — they are consumed by list placement and must keep
/// their run-hole identity.
fn shorthand_property_view(index: &Index, node: NodeId) -> Option<ShorthandProperty<'_>> {
    let same_name_key_value = |is_binding: bool| {
        let [key, value] = index.children_of(node) else {
            return None;
        };
        let key = index.prop_name_of(*key)?;
        // A hole-keyword value (`{ k: ANYTHING }`) is not a same-name property: it
        // must keep its single-node-hole identity and fall through to structural
        // matching (where `homo` treats it as match-any), not be alpha-bound as a
        // real identifier. Mirrors the `Shorthand`/`PatAssign` hole exclusion.
        let value_ident = index
            .ident_of(*value)
            .filter(|name| !is_hole_keyword(name))?;
        Some(ShorthandProperty {
            key,
            value_ident,
            is_binding,
        })
    };
    match index.kind_of(node) {
        NodeKind::Shorthand => {
            let name = index.ident_of(node).filter(|name| !is_hole_keyword(name))?;
            Some(ShorthandProperty {
                key: name,
                value_ident: name,
                is_binding: false,
            })
        }
        // A shorthand destructure property (`PatAssign`) with no default child.
        NodeKind::PatAssign if index.children_of(node).is_empty() => {
            let name = index.ident_of(node).filter(|name| !is_hole_keyword(name))?;
            Some(ShorthandProperty {
                key: name,
                value_ident: name,
                is_binding: true,
            })
        }
        NodeKind::KeyValue => same_name_key_value(false),
        NodeKind::PatKeyValue => same_name_key_value(true),
        _ => None,
    }
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
    // subject a `StrLit`).
    if regex_predicate_pattern(needle, nid).is_some() {
        return Ok(subject.kind_of(sid) == NodeKind::StrLit
            && match (needle.predicate_regex.get(&nid), subject.str_lit_of(sid)) {
                (Some(re), Some(value)) => re.is_match(value),
                _ => false,
            });
    }

    // Shorthand ⟷ explicit same-name property equivalence (object literals and
    // destructuring patterns): `{ k }` is equivalent to `{ k: k }`, in either
    // direction. The two forms project to *different* fact node kinds
    // (`Shorthand`/`KeyValue`, `PatAssign`/`PatKeyValue`), so this is matched
    // before the structural kind comparison would reject the cross-pair. The key
    // name is invariant (exact); the value identifier alpha-binds — as a reference
    // for object literals, as a binding for patterns. (Mirrors the deleted
    // matcher's `match_{shorthand,key_value}_against_*` cross-forms.)
    if let (Some(n_prop), Some(s_prop)) = (
        shorthand_property_view(needle, nid),
        shorthand_property_view(subject, sid),
    ) {
        if n_prop.key != s_prop.key {
            return Ok(false);
        }
        return Ok(if n_prop.is_binding {
            bindings.match_binding(n_prop.value_ident, s_prop.value_ident, mode)
        } else {
            bindings.match_ref(n_prop.value_ident, s_prop.value_ident, mode)
        });
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
            // A binding identifier (declaration) shadows within its frame; a
            // reference resolves against the visible scope stack. Both honor a
            // prebound `target_binding` mapping first (in either mode), then fall
            // back to alpha-binding or exact spelling per `mode`.
            let consistent = if nkind == NodeKind::BindingIdent {
                bindings.match_binding(n, s, mode)
            } else {
                bindings.match_ref(n, s, mode)
            };
            if !consistent {
                return Ok(false);
            }
        }
        (None, None) => {}
        _ => return Ok(false),
    }

    // A `Class` node's superclass (`extends`) is a separate relation, not a child
    // member, so compare it explicitly before the body — both present (and
    // matched) or both absent. `Class` introduces no alpha frame, so the
    // superclass reference resolves against the enclosing scope.
    if nkind == NodeKind::Class {
        match (needle.super_class_of(nid), subject.super_class_of(sid)) {
            (Some(n_super), Some(s_super)) => {
                if !homo(needle, n_super, subject, s_super, mode, bindings)? {
                    return Ok(false);
                }
            }
            (None, None) => {}
            _ => return Ok(false),
        }
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
    nkind: NodeKind,
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

/// Partition `0..len` into maximal runs of non-hole positions — the fixed
/// segments placed as an ordered subsequence with run-hole gaps — plus the
/// anchoring flags (`anchored_left`/`anchored_right` = the first/last position is
/// a fixed anchor). Position `i` is a hole iff `is_hole(i)`. An all-holes list
/// yields no segments. Shared by the list / declarator / top-level placement paths.
fn segment_partition(
    len: usize,
    is_hole: impl Fn(usize) -> bool,
) -> (Vec<(usize, usize)>, bool, bool) {
    let mut segments = Vec::new();
    let mut idx = 0;
    while idx < len {
        if is_hole(idx) {
            idx += 1;
            continue;
        }
        let start = idx;
        while idx < len && !is_hole(idx) {
            idx += 1;
        }
        segments.push((start, idx - start));
    }
    let anchored_left = len > 0 && !is_hole(0);
    let anchored_right = len > 0 && !is_hole(len - 1);
    (segments, anchored_left, anchored_right)
}

/// Ordered-subsequence-with-gaps match of a run-hole-bearing needle list against
/// a candidate list. The carriers partition the needle into maximal fixed
/// segments; an all-holes list pins nothing (matches any candidate run).
fn match_list_with_holes(
    needle: &Index,
    nlist: &[NodeId],
    subject: &Index,
    slist: &[NodeId],
    parent_kind: NodeKind,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let (segments, anchored_left, anchored_right) = segment_partition(nlist.len(), |i| {
        is_run_hole_carrier(needle, parent_kind, nlist[i])
    });
    if segments.is_empty() {
        return Ok(true);
    }
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

/// Run-hole-gap placement for plain node lists (object props / args / class
/// members / …): [`place_declarator_segments`] without alignment recording.
/// Delegates with a throwaway alignment buffer — the two are identical bar that
/// recording (both leftmost-first, both roll `Bindings` back on a failed attempt).
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
    let mut scratch = vec![None; nlist.len()];
    place_declarator_segments(
        needle,
        nlist,
        subject,
        slist,
        segments,
        anchored_left,
        anchored_right,
        seg_idx,
        cand_min,
        mode,
        bindings,
        &mut scratch,
    )
}

/// The var-decl node of a single var-decl-statement's facts, paired with the
/// root wrapper kind so the caller can enforce wrapper symmetry (a plain `const`
/// statement must not match an `export const`). `None` if the statement is not a
/// (possibly exported) variable declaration.
fn var_decl_node(index: &Index) -> Option<(NodeKind, NodeId)> {
    let &root = index.roots.first()?;
    match index.kind_of(root) {
        NodeKind::VarDecl => Some((NodeKind::VarDecl, root)),
        NodeKind::ExportDecl => {
            let &kid = index.children_of(root).first()?;
            (index.kind_of(kid) == NodeKind::VarDecl).then_some((NodeKind::ExportDecl, kid))
        }
        _ => None,
    }
}

/// Greedy-leftmost alignment of a needle var-decl's declarators (possibly
/// carrying `DECLARATORS` run holes) onto a subject var-decl's declarators,
/// recording for each needle declarator the subject declarator it placed onto. A
/// hole-free needle aligns 1:1 (lengths must match); otherwise the non-hole
/// declarators partition into maximal fixed segments placed as an ordered
/// subsequence with gaps. `None` when no placement matches.
fn align_var_declarators(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<Option<Vec<Option<usize>>>, Unsupported> {
    let mut alignment = vec![None; ndecls.len()];
    let is_hole = |d: NodeId| is_run_hole_carrier(needle, NodeKind::VarDecl, d);
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
    let (segments, anchored_left, anchored_right) =
        segment_partition(ndecls.len(), |i| is_hole(ndecls[i]));
    // An all-holes declarator list pins nothing (no positions to align).
    if segments.is_empty() {
        return Ok(Some(alignment));
    }
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
/// before matching (alpha-mode coupling), so the caller can force the target
/// binding's identity — a no-op under `Exact`. This composes the whole-statement
/// var-decl match (wrapper symmetry, `var`/`let`/`const` kind, declarator
/// alignment): a `Some` result *is* a faithful match. Fail-closed on an
/// unsupported needle construct.
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
    Ok(var_declarator_alignment_prepared(
        needle, subject, mode, prebind,
    ))
}

/// Like [`var_declarator_alignment_indexed`], but **without** re-running the
/// needle-only [`unsupported_needle_construct`] faithful-subset check — the caller
/// must have already probed this exact needle (once, before the candidate loop) so
/// the only `Unsupported` source is provably absent. This is the per-candidate hot
/// path: `unsupported_needle_construct` is three full passes over the needle index
/// plus `collect_subtree` recursion, all a function of the needle alone, so re-running
/// it for every subject is `O(selectors × candidates)` wasted work the up-front probe
/// already did. The return is therefore infallible (`Option`, not `Result`).
pub fn var_declarator_alignment_prepared(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    prebind: Option<(&str, &str)>,
) -> Option<Vec<Option<usize>>> {
    let (Some((nwrap, nvd)), Some((swrap, svd))) = (var_decl_node(needle), var_decl_node(subject))
    else {
        return None;
    };
    // Wrapper symmetry (plain vs exported) and `var`/`let`/`const` kind: the
    // node-level structure the declarator alignment does not itself compare.
    if nwrap != swrap || needle.operator_of(nvd) != subject.operator_of(svd) {
        return None;
    }
    let mut bindings = Bindings::default();
    if let Some((n, s)) = prebind
        && !bindings.prebind(n, s)
    {
        return None;
    }
    // The needle is probed-supported, so no sub-node descent can error (see
    // [`nodes_match`]); unwrap the infallible alignment.
    align_var_declarators(
        needle,
        needle.children_of(nvd),
        subject,
        subject.children_of(svd),
        mode,
        &mut bindings,
    )
    .expect("needle construct already probed as supported")
}

fn collect_subtree(index: &Index, node: NodeId, out: &mut HashSet<NodeId>) {
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
/// identifier discriminator ([`needle_required_tokens`]) so the two agree exactly
/// on which identifiers are real comparisons versus absorbed placeholders.
fn consumed_nodes(index: &Index) -> HashSet<NodeId> {
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
        if regex_predicate_pattern(index, node).is_some() {
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
fn unsupported_needle_construct(index: &Index) -> Option<&'static str> {
    let consumed = consumed_nodes(index);
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
    Ok(matches_prepared(needle, subject, mode))
}

/// Like [`matches_indexed`], but **without** re-running the needle-only
/// [`unsupported_needle_construct`] faithful-subset check — the caller must have
/// already probed this exact needle (once, before the candidate loop). On the
/// corpus-wide resolve hot path the same needle is matched against every candidate
/// statement, so re-validating it per candidate is `O(selectors × candidates)`
/// redundant work (the dominant self-cost in the fact-resolver profile). Skipping
/// it once the needle is known-supported is behavior-preserving — the check is a
/// pure function of the needle, identical across subjects — and makes the result
/// infallible.
pub fn matches_prepared(needle: &Index, subject: &Index, mode: Mode) -> bool {
    let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
        return false;
    };
    let mut bindings = Bindings::default();
    // Probed-supported needle: a sub-node descent of an already-probed needle never
    // newly errors (see [`nodes_match`]), so the match is infallible here.
    homo(needle, n_root, subject, s_root, mode, &mut bindings)
        .expect("needle construct already probed as supported")
}

/// A **sound** per-candidate prefilter for the single-statement `matches` scan:
/// the root node kind a subject must share for `matches` to possibly return true.
/// `Some(kind)` ⟹ every subject whose root kind differs is a guaranteed
/// `Ok(false)` (it is exactly the `nkind != subject kind` gate in [`homo`], which
/// runs after the hole/predicate special-cases), so the caller may skip it
/// without changing any verdict. `None` ⟹ no prefilter applies — the needle root
/// is a single-node hole or the regex predicate, which match subjects of *other*
/// kinds — so the caller must run the full match.
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
        .map(|(_, kind)| kind.as_tag())
}

/// The init-expression node kind of a single-declarator var-decl statement's
/// first declarator (the `VarDeclarator` child at ordinal 1), or `None` when
/// there is no init. A **sound secondary prefilter** for the var-decl member
/// scan: a subject declarator can only match the needle declarator if their init
/// kinds agree (the root-kind prefilter does not discriminate — every var-decl
/// shares kind `VarDecl`). `None` returns fall through to the full match.
pub fn var_declarator_init_kind(facts: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(facts);
    init_node_of(&index).map(|init| index.kind_of(init).as_tag())
}

/// The subject init kind a needle declarator requires, or `None` when it
/// constrains nothing (a single-node `EXPR`/`ANYTHING` init hole matches any
/// init). A `STR_LITERAL_MATCHING_RE(...)` predicate is a `Call` in the needle,
/// but [`homo`] matches it only against a `StrLit` subject — so the kind it
/// *requires* is `StrLit`, not its own `Call` kind. Returning that (rather than
/// `None`) keeps the prefilter both sound and selective: the CSS-module
/// class-name members (`const x = STR_LITERAL_MATCHING_RE(…)`, the bulk of the
/// per-chunk cost) scan only string-literal declarators, not every declarator.
pub fn needle_var_declarator_init_kind_prefilter(needle: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(needle);
    let init = init_node_of(&index)?;
    if is_single_node_hole(&index, init) {
        return None;
    }
    if regex_predicate_pattern(&index, init).is_some() {
        return Some("StrLit");
    }
    Some(index.kind_of(init).as_tag())
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

/// String-literal values carried by a subject index, deduplicated. Used by
/// resolver-side predicate postings: a `STR_LITERAL_MATCHING_RE(...)` needle can
/// only match a subject that carries at least one string literal satisfying that
/// predicate, and the full matcher still verifies the aligned position.
pub fn subject_string_literals(index: &Index) -> Vec<&str> {
    let mut values = Vec::new();
    let mut seen: HashSet<&str> = HashSet::new();
    for node in 0..index.kind.len() as NodeId {
        if let Some(value) = index.str_lit_of(node)
            && seen.insert(value)
        {
            values.push(value);
        }
    }
    values
}

/// The non-consumed `STR_LITERAL_MATCHING_RE(...)` predicates a needle requires.
/// Predicates inside run-hole carrier subtrees are excluded because the matcher
/// absorbs those subtrees rather than aligning them to subject string literals.
pub fn needle_required_string_literal_predicates(index: &Index) -> Vec<StringLiteralPredicate<'_>> {
    let consumed = consumed_nodes(index);
    let mut predicates = Vec::new();
    for node in 0..index.kind.len() as NodeId {
        if consumed.contains(&node) {
            continue;
        }
        if let Some(pattern) = regex_predicate_pattern(index, node) {
            predicates.push(StringLiteralPredicate {
                regex: index.predicate_regex.get(&node),
                literal_prefix: regex_required_literal_prefix(pattern),
            });
        }
    }
    predicates
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
    index.kind_of(root) == NodeKind::ExprStmt && {
        let kids = index.children_of(root);
        kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
    }
}

/// Match a multi-statement needle (each element one statement's facts, in source
/// order) against a chunk body (each element one top-level statement's facts, in
/// body order) as an ordered subsequence with module-level `STMT_LIST` holes,
/// **enumerating every alignment** (needle-index → body-index, `None` for a hole
/// position). Never anchored at module level, and an all-holes needle pins
/// nothing (no alignment). Fail-closed on an unsupported non-hole needle
/// statement.
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
    let (segments, _, _) = segment_partition(needle_idx.len(), |i| {
        is_module_stmt_list_hole(&needle_idx[i])
    });
    // A fixed (non-hole) needle statement must be faithfully supported.
    for &(start, len) in &segments {
        for item in &needle_idx[start..start + len] {
            if let Some(reason) = unsupported_needle_construct(item) {
                return Err(Unsupported { reason });
            }
        }
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

/// Align a multi-statement needle against the subject body as a **fixed
/// contiguous window** (the member binding-group path): slide
/// `subject.windows(needle.len())` and match each window positionally 1:1 with one
/// [`Bindings`] threaded through the window. Returns the body start index of every
/// full-window match.
///
/// Deviation from [`match_top_level_sequence_indexed`]: there are **no**
/// module-level `STMT_LIST` holes here. A run-hole keyword is not faithfully
/// encodable in a fixed-window position, so a needle bearing one fails closed via
/// the per-item unsupported scan (`STMT_LIST` is a statement-list hole, not a
/// single-statement wildcard, so a fixed window finds zero either way).
pub fn match_fixed_window_sequence_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    mode: Mode,
) -> Result<Vec<usize>, Unsupported> {
    for needle in needle_idx {
        if let Some(reason) = unsupported_needle_construct(needle) {
            return Err(Unsupported { reason });
        }
    }
    let n = needle_idx.len();
    let mut starts = Vec::new();
    if n == 0 || n > subject_idx.len() {
        return Ok(starts);
    }
    for start in 0..=(subject_idx.len() - n) {
        let mut bindings = Bindings::default();
        let mut matched = true;
        for offset in 0..n {
            let needle = &needle_idx[offset];
            let subject = &subject_idx[start + offset];
            let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first())
            else {
                matched = false;
                break;
            };
            if !homo(needle, n_root, subject, s_root, mode, &mut bindings)? {
                matched = false;
                break;
            }
        }
        if matched {
            starts.push(start);
        }
    }
    Ok(starts)
}

/// Resolve a **contiguous** multi-statement needle whose item at `target_idx` is
/// a single-declarator var-decl, over **prebuilt** subject indices (the caller
/// reuses cached chunk body indices instead of rebuilding them per selector). For
/// each body window of length `needle_idx.len()`, thread one `Bindings` through
/// the items in source order: every non-target item matches as a whole statement,
/// and at `target_idx` the match branches over the window item's declarators (each
/// matched via the single needle declarator, so a target inside a multi-declarator
/// owner is found). Returns `(target_body_idx, subject_declarator_idx)` for every
/// full-window match — the caller reads the binding from that declarator and
/// applies owner-level categoricity. This path is fixed-window (no module-level
/// `STMT_LIST` holes); a hole-bearing needle item fails closed via the per-item
/// unsupported scan.
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
/// [`match_single_declarator_target_windows_indexed`], threading `Bindings` in
/// source order and branching at `target_idx` over the window item's declarators. The
/// chosen declarator index is carried to the window end (when it is recorded),
/// so a binding is reported only for a full-window match.
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
/// snapshot/restore.
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
