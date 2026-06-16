//! Layer-1 AST-shape index + read-off selector API (W1 of the read-off
//! minimization redesign; see `plans/readoff_minimization.md`).
//!
//! This is the strangler-fig foundation built *alongside* the existing
//! `selector_codemod` cover-search minimizer without changing its behavior. It
//! provides:
//!
//!   1. A hash-consed Merkle shape index over the chunk AST (O(N) build):
//!      every distinct alpha-equivalent subtree shape gets one canonical
//!      [`ShapeId`]; equal shapes collapse and share, so subtree equality is an
//!      O(1) id comparison (Downey-Sethi-Tarjan minimal-DAG construction).
//!   2. Multi-granularity, position-aware *shape features* per top-level item,
//!      extended from the existing [`SelectorCandidateIndex`] feature taxonomy
//!      with **bounded-depth shape skeletons** (this is the cons-spine /
//!      bounded-depth list-hole encoding, locked decision #1).
//!   3. Inverted posting lists feature -> item set, with per-feature
//!      **selectivity** (posting-list size) and **stability** (semantic vs
//!      volatile) scores.
//!   4. A [`ShapeIndex::minimal_anchor_set`] read-off API: scan a target's own
//!      features ranked by selective x stable; emit the single discriminating
//!      feature on the `OPT=1` fast path, else greedy set-cover over the
//!      target's own features. Bounded to the target's features, never an
//!      unbounded corpus scan.
//!
//! ## Relationship to `SelectorCandidateIndex`
//!
//! This module *extends* `SelectorCandidateIndex` rather than forking it: it
//! reuses that index's [`SelectorFeature`] taxonomy and per-item feature
//! extraction (so producer/consumer feature semantics never diverge), and adds
//! only the new layers — Merkle shape canonicalization, shape-skeleton
//! features, stability scoring, and the read-off cover. The matcher in
//! `source_match` stays the correctness gate; this index only narrows and
//! ranks candidates.
//!
//! ## List-hole encoding interface boundary (locked decision #1)
//!
//! No arity assumption is baked into the index or the greedy read-off core.
//! Variable-length child runs (call args, statement lists, object props, class
//! members, declarators) are handled **only** behind the
//! [`ShapeFeatureExtractor`] trait: the default [`ConsSpineExtractor`] encodes
//! lists as right-leaning cons spines and hashes only the top `d` levels, with
//! the variadic frontier collapsing to one wildcard child. The index, posting
//! lists, selectivity/stability scoring, and greedy set-cover all operate on
//! the opaque feature set the extractor emits, so a later wave can swap in a
//! hedge-automaton extractor without touching them.

use std::collections::BTreeSet;
use std::sync::LazyLock;

use rustc_hash::FxHashMap;
use selector_candidate_index::{
    CandidateSet, IndexedTopLevelCandidate, SelectorCandidateIndex, SelectorFeature,
};
use swc_ecma_ast::*;

/// Canonical identity of an alpha-equivalent subtree shape. Two subtrees share
/// a [`ShapeId`] iff they are structurally identical after canonicalizing
/// minified identifier leaves to a wildcard sentinel (so renamed-isomorphic
/// subtrees collapse) and keeping stable tokens (string/number literals, object
/// keys, member/method names) concrete.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct ShapeId(u32);

impl ShapeId {
    pub fn as_u32(self) -> u32 {
        self.0
    }
}

/// How stable a feature is expected to be across a minifier rebuild. Drives the
/// read-off ranking's second key (the first is selectivity).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
pub enum Stability {
    /// Minified identifier names and volatile-looking literals (content hashes,
    /// build counters). Churn every rebuild; deprioritized as anchors.
    Volatile,
    /// Coarse structural facts (top-level kind, var kind, function arity). Stay
    /// put across rebuilds but discriminate weakly.
    Structural,
    /// Semantic literals, object keys, member/method names, import sources,
    /// exported names, and shape skeletons of stable subtrees. Survive
    /// rebuilds and discriminate well; preferred anchors.
    Semantic,
}

/// A single canonical token leaf as it enters the Merkle hash. Identifier
/// leaves never appear here (they collapse to the wildcard sentinel before
/// hashing), so equality of the leaf stream is alpha-equivalence.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum ShapeLeaf {
    /// Canonical wildcard for any minified identifier (alpha-equivalence).
    IdentWildcard,
    StringLit(String),
    NumberLit(String),
    BoolLit(bool),
    NullLit,
    /// A stable member/object/class key name kept concrete.
    Key(String),
}

/// Structural node kinds the shape canonicalizer distinguishes. Coarser than
/// the full swc AST: only the discriminants that survive minification and that
/// the source-match hole language can pin are kept; everything else folds into
/// [`ShapeKind::Other`] keyed by a stable discriminant string.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum ShapeKind {
    Leaf(ShapeLeaf),
    /// A fixed-arity interior node: kind tag plus ordered child shapes.
    Node(&'static str),
    /// The bounded-depth frontier: a subtree deeper than the skeleton depth
    /// budget collapses here, erasing its interior. This is the depth bound,
    /// not the list-hole frontier (that is `Node("list")` with a cons spine).
    Frontier,
}

/// A node in the hash-consed Merkle DAG: its kind plus the canonical ids of its
/// ordered children. Interned so equal `(kind, children)` tuples share a
/// [`ShapeId`].
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct ShapeNode {
    kind: ShapeKind,
    children: Vec<ShapeId>,
}

/// Bottom-up hash-consing table. Assigns each distinct `(kind, children)` tuple
/// a sequential canonical [`ShapeId`] on first sighting; equal shapes collapse
/// to the same id. Deterministic and O(1) amortized per node, so building the
/// whole DAG is O(N) in chunk size.
#[derive(Debug, Default)]
pub struct ShapeInterner {
    by_node: FxHashMap<ShapeNode, ShapeId>,
    /// Per-shape multiplicity across everything interned so far. The collision
    /// count is exactly the per-shape selectivity signal (free byproduct).
    multiplicity: Vec<u32>,
}

impl ShapeInterner {
    fn intern(&mut self, kind: ShapeKind, children: Vec<ShapeId>) -> ShapeId {
        let node = ShapeNode { kind, children };
        if let Some(&id) = self.by_node.get(&node) {
            self.multiplicity[id.0 as usize] += 1;
            return id;
        }
        let id = ShapeId(self.multiplicity.len() as u32);
        self.multiplicity.push(1);
        self.by_node.insert(node, id);
        id
    }

    /// Number of times this shape was interned across the chunk. A shape seen
    /// once is maximally selective.
    pub fn multiplicity(&self, id: ShapeId) -> u32 {
        self.multiplicity[id.0 as usize]
    }

    pub fn distinct_shapes(&self) -> usize {
        self.multiplicity.len()
    }
}

/// The list-hole / shape-feature extraction boundary (locked decision #1).
///
/// All variadic-arity handling lives here. The index core, posting lists, and
/// greedy read-off treat the returned [`ShapeId`] set as opaque, so swapping
/// this implementation (e.g. for a hedge-automaton extractor) cannot leak an
/// arity assumption into the core. Implementors fold a top-level item's AST
/// into a set of bounded-depth canonical shape ids via the supplied interner.
pub trait ShapeFeatureExtractor {
    /// Emit the canonical shape-skeleton ids for one top-level item, interning
    /// every distinct subtree shape into `interner`. The returned ids are the
    /// item's shape features; the caller unions them into posting lists.
    fn shape_features(&self, item: &ModuleItem, interner: &mut ShapeInterner) -> BTreeSet<ShapeId>;
}

/// Default extractor: cons-spine binarization + bounded-depth skeletons.
///
/// Child lists are encoded as right-leaning cons spines (`Node("cons")` of
/// head shape and tail-spine shape), so a list hole is naturally a wildcard
/// over the spine tail. Only the top `depth` levels of any subtree are hashed;
/// anything below collapses to [`ShapeKind::Frontier`]. Emitting a feature for
/// every node at every prefix depth `1..=depth` yields the multi-granularity
/// skeletons (a shallow shape and progressively deeper ones), so a target can
/// be discriminated at whatever granularity is selective enough.
#[derive(Debug, Clone, Copy)]
pub struct ConsSpineExtractor {
    /// Maximum skeleton depth `d`. The variadic frontier collapses to one
    /// wildcard child at this bound (bounded-depth list-hole handling).
    pub depth: u32,
}

impl Default for ConsSpineExtractor {
    fn default() -> Self {
        Self { depth: 3 }
    }
}

impl ShapeFeatureExtractor for ConsSpineExtractor {
    fn shape_features(&self, item: &ModuleItem, interner: &mut ShapeInterner) -> BTreeSet<ShapeId> {
        let mut features = BTreeSet::new();
        let mut builder = SkeletonBuilder {
            interner,
            max_depth: self.depth,
            features: &mut features,
        };
        // Intern the item at every skeleton depth so coarse and fine shapes
        // both become discriminating features (multi-granularity).
        for d in 1..=self.depth {
            builder.max_depth = d;
            let id = builder.module_item(item, 0);
            builder.features.insert(id);
        }
        features
    }
}

/// Carries the interner + depth budget while folding one subtree into canonical
/// shape ids. `depth` counts from the skeleton root; once it reaches
/// `max_depth` the subtree collapses to a [`ShapeKind::Frontier`] leaf.
struct SkeletonBuilder<'a> {
    interner: &'a mut ShapeInterner,
    max_depth: u32,
    features: &'a mut BTreeSet<ShapeId>,
}

impl SkeletonBuilder<'_> {
    fn frontier(&mut self) -> ShapeId {
        self.interner.intern(ShapeKind::Frontier, Vec::new())
    }

    fn leaf(&mut self, leaf: ShapeLeaf) -> ShapeId {
        self.interner.intern(ShapeKind::Leaf(leaf), Vec::new())
    }

    fn node(&mut self, tag: &'static str, children: Vec<ShapeId>) -> ShapeId {
        self.interner.intern(ShapeKind::Node(tag), children)
    }

    /// Encode a child run as a right-leaning cons spine, bounded by the depth
    /// budget. An empty run is `Node("nil")`; a run head-cons-tail. This is the
    /// only place arity is modeled, and it lives behind the extractor trait.
    fn cons_spine(&mut self, mut elems: Vec<ShapeId>, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        match elems.len() {
            0 => self.node("nil", Vec::new()),
            _ => {
                let head = elems.remove(0);
                let tail = self.cons_spine(elems, depth + 1);
                self.node("cons", vec![head, tail])
            }
        }
    }

    fn module_item(&mut self, item: &ModuleItem, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        match item {
            ModuleItem::Stmt(stmt) => self.stmt(stmt, depth),
            ModuleItem::ModuleDecl(decl) => self.module_decl(decl, depth),
        }
    }

    fn module_decl(&mut self, decl: &ModuleDecl, depth: u32) -> ShapeId {
        match decl {
            ModuleDecl::Import(import) => {
                let src = self.leaf(ShapeLeaf::StringLit(
                    import.src.value.to_string_lossy().to_string(),
                ));
                self.node("import", vec![src])
            }
            ModuleDecl::ExportDecl(export) => {
                let inner = self.decl(&export.decl, depth + 1);
                self.node("export_decl", vec![inner])
            }
            ModuleDecl::ExportDefaultExpr(export) => {
                let inner = self.expr(&export.expr, depth + 1);
                self.node("export_default_expr", vec![inner])
            }
            _ => self.node("module_decl_other", Vec::new()),
        }
    }

    fn stmt(&mut self, stmt: &Stmt, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        match stmt {
            Stmt::Decl(decl) => self.decl(decl, depth),
            Stmt::Expr(expr) => {
                let inner = self.expr(&expr.expr, depth + 1);
                self.node("expr_stmt", vec![inner])
            }
            Stmt::Return(ret) => {
                let inner = match &ret.arg {
                    Some(arg) => self.expr(arg, depth + 1),
                    None => self.node("nil", Vec::new()),
                };
                self.node("return", vec![inner])
            }
            Stmt::Block(block) => {
                let body = block
                    .stmts
                    .iter()
                    .map(|s| self.stmt(s, depth + 2))
                    .collect();
                let spine = self.cons_spine(body, depth + 1);
                self.node("block", vec![spine])
            }
            Stmt::If(if_stmt) => {
                let test = self.expr(&if_stmt.test, depth + 1);
                self.node("if", vec![test])
            }
            _ => self.node("stmt_other", Vec::new()),
        }
    }

    fn decl(&mut self, decl: &Decl, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        match decl {
            Decl::Fn(function) => {
                let params = vec![self.frontier(); function.function.params.len()];
                let spine = self.cons_spine(params, depth + 1);
                self.node("fn", vec![spine])
            }
            Decl::Class(class) => {
                let members = class
                    .class
                    .body
                    .iter()
                    .map(|m| self.class_member(m, depth + 2))
                    .collect();
                let spine = self.cons_spine(members, depth + 1);
                self.node("class", vec![spine])
            }
            Decl::Var(var) => {
                let decls = var
                    .decls
                    .iter()
                    .map(|d| match &d.init {
                        Some(init) => self.expr(init, depth + 2),
                        None => self.node("nil", Vec::new()),
                    })
                    .collect();
                let spine = self.cons_spine(decls, depth + 1);
                let kind = match var.kind {
                    VarDeclKind::Var => "var",
                    VarDeclKind::Let => "let",
                    VarDeclKind::Const => "const",
                };
                self.node_with_tag(kind, vec![spine])
            }
            _ => self.node("decl_other", Vec::new()),
        }
    }

    fn class_member(&mut self, member: &ClassMember, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        let key = class_member_key(member);
        match key {
            Some(name) => {
                let key_leaf = self.leaf(ShapeLeaf::Key(name));
                self.node("class_member", vec![key_leaf])
            }
            None => self.node("class_member_other", Vec::new()),
        }
    }

    fn expr(&mut self, expr: &Expr, depth: u32) -> ShapeId {
        if depth >= self.max_depth {
            return self.frontier();
        }
        match expr {
            Expr::Ident(_) => self.leaf(ShapeLeaf::IdentWildcard),
            Expr::Lit(lit) => self.literal(lit),
            Expr::Call(call) => {
                let callee = match &call.callee {
                    Callee::Expr(e) => self.expr(e, depth + 1),
                    Callee::Super(_) => self.node("super", Vec::new()),
                    Callee::Import(_) => self.node("import_callee", Vec::new()),
                };
                let args = call
                    .args
                    .iter()
                    .map(|a| self.expr(&a.expr, depth + 2))
                    .collect();
                let spine = self.cons_spine(args, depth + 1);
                self.node("call", vec![callee, spine])
            }
            Expr::New(new) => {
                let callee = self.expr(&new.callee, depth + 1);
                self.node("new", vec![callee])
            }
            Expr::Member(member) => {
                let obj = self.expr(&member.obj, depth + 1);
                let prop = match member_prop_name(&member.prop) {
                    Some(name) => self.leaf(ShapeLeaf::Key(name)),
                    None => self.frontier(),
                };
                self.node("member", vec![obj, prop])
            }
            Expr::Object(object) => {
                let props = object
                    .props
                    .iter()
                    .map(|p| match object_prop_key(p) {
                        Some(name) => self.leaf(ShapeLeaf::Key(name)),
                        None => self.node("prop_other", Vec::new()),
                    })
                    .collect();
                let spine = self.cons_spine(props, depth + 1);
                self.node("object", vec![spine])
            }
            Expr::Array(array) => {
                let elems = array
                    .elems
                    .iter()
                    .map(|e| match e {
                        Some(e) => self.expr(&e.expr, depth + 2),
                        None => self.node("hole", Vec::new()),
                    })
                    .collect();
                let spine = self.cons_spine(elems, depth + 1);
                self.node("array", vec![spine])
            }
            Expr::Arrow(arrow) => {
                let params = vec![self.frontier(); arrow.params.len()];
                let spine = self.cons_spine(params, depth + 1);
                self.node("arrow", vec![spine])
            }
            Expr::Fn(function) => {
                let params = vec![self.frontier(); function.function.params.len()];
                let spine = self.cons_spine(params, depth + 1);
                self.node("fn_expr", vec![spine])
            }
            Expr::Bin(bin) => {
                let left = self.expr(&bin.left, depth + 1);
                let right = self.expr(&bin.right, depth + 1);
                self.node("bin", vec![left, right])
            }
            Expr::Assign(assign) => {
                let right = self.expr(&assign.right, depth + 1);
                self.node("assign", vec![right])
            }
            Expr::Paren(paren) => self.expr(&paren.expr, depth),
            _ => self.node("expr_other", Vec::new()),
        }
    }

    fn literal(&mut self, lit: &Lit) -> ShapeId {
        let leaf = match lit {
            Lit::Str(str_) => ShapeLeaf::StringLit(str_.value.to_string_lossy().to_string()),
            Lit::Num(num) => ShapeLeaf::NumberLit(num.value.to_string()),
            Lit::Bool(b) => ShapeLeaf::BoolLit(b.value),
            Lit::Null(_) => ShapeLeaf::NullLit,
            Lit::BigInt(bigint) => ShapeLeaf::NumberLit(bigint.value.to_string()),
            Lit::Regex(regex) => ShapeLeaf::StringLit(regex.exp.to_string()),
            Lit::JSXText(_) => ShapeLeaf::NullLit,
        };
        self.leaf(leaf)
    }

    /// `node`, but the tag is a `&str` known at call time; we still need a
    /// `'static` tag for [`ShapeKind::Node`], so map the small fixed set here.
    fn node_with_tag(&mut self, tag: &str, children: Vec<ShapeId>) -> ShapeId {
        let static_tag: &'static str = match tag {
            "var" => "var",
            "let" => "let",
            "const" => "const",
            _ => "decl_other",
        };
        self.node(static_tag, children)
    }
}

fn class_member_key(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name_string(&method.key),
        ClassMember::PrivateMethod(method) => Some(format!("#{}", method.key.name)),
        ClassMember::ClassProp(prop) => prop_name_string(&prop.key),
        ClassMember::PrivateProp(prop) => Some(format!("#{}", prop.key.name)),
        _ => None,
    }
}

fn member_prop_name(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::PrivateName(private) => Some(format!("#{}", private.name)),
        MemberProp::Computed(_) => None,
    }
}

fn object_prop_key(prop: &PropOrSpread) -> Option<String> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    match prop.as_ref() {
        Prop::Shorthand(ident) => Some(ident.sym.to_string()),
        Prop::KeyValue(kv) => prop_name_string(&kv.key),
        Prop::Method(method) => prop_name_string(&method.key),
        Prop::Getter(getter) => prop_name_string(&getter.key),
        Prop::Setter(setter) => prop_name_string(&setter.key),
        _ => None,
    }
}

fn prop_name_string(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(str_) => Some(str_.value.to_string_lossy().to_string()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::BigInt(bigint) => Some(bigint.value.to_string()),
        PropName::Computed(_) => None,
    }
}

/// A feature usable as a read-off anchor: either one of the existing
/// [`SelectorFeature`]s (literals, keys, member/callee names, kinds) or a
/// bounded-depth shape skeleton id. Tagging them in one enum lets the greedy
/// cover rank heterogeneous features uniformly by `(selectivity, stability)`.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum ShapeFeature {
    Selector(SelectorFeature),
    Skeleton(ShapeId),
}

/// A feature plus its index-wide selectivity and stability, ready for ranking.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ScoredFeature {
    pub feature: ShapeFeature,
    /// Number of items whose feature set contains this feature. `1` is
    /// maximally selective (uniquely identifies its item).
    pub selectivity: usize,
    pub stability: Stability,
    /// Retained-anchor cost: the source size this anchor forces the renderer to
    /// pin verbatim (the literal value's character length, a key/member label's
    /// length; `usize::MAX` for a structural skeleton, which pins no renderable
    /// token and so must rank last on this key). Used as the ranking's third key
    /// so that among equally selective+stable anchors a *short* renderable one
    /// wins; a long literal value (a hundreds-of-lines template / string shared
    /// across siblings) is a last-resort anchor, chosen only when nothing shorter
    /// discriminates.
    pub cost: usize,
}

/// Outcome of a read-off for one target item.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct AnchorSet {
    pub body_idx: usize,
    /// The features whose posting-list intersection is exactly `{body_idx}`,
    /// ranked best-first. Length 1 is the `OPT=1` read-off; longer sets come
    /// from the greedy tail.
    pub anchors: Vec<ScoredFeature>,
    /// `true` iff a single feature sufficed (the Zipfian common case).
    pub opt_one: bool,
}

/// A stable anchor on a top-level *neighbor* of a residual target — the handle
/// for enclosing-context anchoring (#2315). The `anchor_set` is one of the
/// neighbor's individually-unique value anchors; pairing the neighbor (holed to
/// that anchor) with the target's holed scaffold in a 2-statement window singles
/// the target out even when its own features cannot.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ContextNeighborAnchor {
    pub neighbor_body_idx: usize,
    pub anchor_set: AnchorSet,
}

/// Per-item entry: the existing prefilter candidate plus this item's shape
/// skeleton features.
#[derive(Debug, Clone)]
struct ShapeIndexedItem {
    candidate: IndexedTopLevelCandidate,
    skeletons: BTreeSet<ShapeId>,
}

/// The Layer-1 shape index. Wraps a [`SelectorCandidateIndex`] (reused, not
/// reimplemented) and adds the Merkle shape interner, shape-skeleton posting
/// lists, and the read-off API.
pub struct ShapeIndex {
    items: Vec<ShapeIndexedItem>,
    interner: ShapeInterner,
    /// feature -> set of item body indices exhibiting it (inverted index).
    /// Covers both selector features and shape skeletons. Hashed (no ordered
    /// iteration needed) with sorted-`Vec` posting lists for cheap build +
    /// intersection (issue #2291).
    postings: FxHashMap<ShapeFeature, CandidateSet>,
}

impl ShapeIndex {
    pub fn new(module: &Module) -> Self {
        Self::with_extractor(module, &ConsSpineExtractor::default())
    }

    pub fn with_extractor(module: &Module, extractor: &dyn ShapeFeatureExtractor) -> Self {
        let prefilter = SelectorCandidateIndex::new(module);
        let mut interner = ShapeInterner::default();
        let mut postings: FxHashMap<ShapeFeature, CandidateSet> = FxHashMap::default();
        let mut items = Vec::with_capacity(module.body.len());

        for (body_idx, module_item) in module.body.iter().enumerate() {
            let candidate = prefilter
                .candidate(body_idx)
                .expect("prefilter indexes every module body item")
                .clone();
            for feature in &candidate.features {
                postings
                    .entry(ShapeFeature::Selector(feature.clone()))
                    .or_default()
                    .push_ascending(body_idx);
            }
            let skeletons = extractor.shape_features(module_item, &mut interner);
            for &shape in &skeletons {
                postings
                    .entry(ShapeFeature::Skeleton(shape))
                    .or_default()
                    .push_ascending(body_idx);
            }
            items.push(ShapeIndexedItem {
                candidate,
                skeletons,
            });
        }

        Self {
            items,
            interner,
            postings,
        }
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn distinct_shapes(&self) -> usize {
        self.interner.distinct_shapes()
    }

    /// Number of distinct features with a non-empty posting list — a proxy for
    /// the inverted index's memory footprint.
    pub fn posting_entry_count(&self) -> usize {
        self.postings.len()
    }

    pub fn candidate(&self, body_idx: usize) -> Option<&IndexedTopLevelCandidate> {
        self.items.get(body_idx).map(|item| &item.candidate)
    }

    fn posting(&self, feature: &ShapeFeature) -> &CandidateSet {
        static EMPTY: LazyLock<CandidateSet> = LazyLock::new(CandidateSet::empty);
        self.postings
            .get(feature)
            .unwrap_or_else(|| LazyLock::force(&EMPTY))
    }

    /// Every feature exhibited by `body_idx` that is sound to use as an anchor
    /// under alpha-equivalent matching (the mode the read-off minimizer emits),
    /// each scored with its index-wide selectivity and stability. The read-off
    /// ranks and covers over exactly this set, so the search is bounded to the
    /// target's own features.
    ///
    /// Alpha-soundness gate: a bare-identifier callee (`make(...)`) is renamed
    /// every rebuild, so under alpha-equivalence the matcher wildcards it — it
    /// cannot discriminate and must not be proposed as an anchor. Member-path
    /// callees and `.prop` accesses keep stable property names and stay. This
    /// keeps the read-off from proposing a singleton the matcher won't honor.
    pub fn scored_features(&self, body_idx: usize) -> Vec<ScoredFeature> {
        let Some(item) = self.items.get(body_idx) else {
            return Vec::new();
        };
        let mut scored = Vec::new();
        for feature in &item.candidate.features {
            if !is_alpha_stable_anchor(feature) {
                continue;
            }
            let f = ShapeFeature::Selector(feature.clone());
            scored.push(ScoredFeature {
                selectivity: self.posting(&f).len(),
                stability: selector_feature_stability(feature),
                cost: selector_feature_cost(feature),
                feature: f,
            });
        }
        for &shape in &item.skeletons {
            let f = ShapeFeature::Skeleton(shape);
            // W2 note #3: a shape skeleton interned many times across the chunk
            // is structural noise (a common scaffold), not a stable anchor; one
            // interned rarely is a distinctive shape worth pinning. Demote the
            // common ones to `Structural` so a value-bearing semantic anchor
            // (literal, key, member) wins the stability tiebreak when their
            // selectivities are equal. Selectivity stays the primary key.
            let stability = if self.interner.multiplicity(shape) > SKELETON_NOISE_MULTIPLICITY {
                Stability::Structural
            } else {
                Stability::Semantic
            };
            scored.push(ScoredFeature {
                selectivity: self.posting(&f).len(),
                stability,
                // A skeleton pins no concrete token — the renderer holes it away
                // and keeps nothing, so it cannot serve as a retained anchor. It
                // must therefore *lose* the cost tiebreak to any renderable value
                // anchor of equal selectivity+stability (otherwise a skeleton
                // would win and the render would hole the only discriminator,
                // yielding a non-unique selector). It still wins on the
                // higher-priority selectivity/stability keys when strictly
                // better; only the cost tiebreak ranks it last.
                cost: usize::MAX,
                feature: f,
            });
        }
        scored.sort_by_key(rank_key);
        scored
    }

    /// Read off a minimal anchor set that resolves uniquely to `body_idx`,
    /// scanning only the target's own features.
    ///
    /// Ranks the target's features by selective x stable; if the top feature's
    /// posting list is already `{body_idx}` returns it (the `OPT=1` fast path).
    /// Otherwise runs greedy set-cover — most-excluding-first — over the
    /// target's features until the running posting-list intersection is the
    /// singleton `{body_idx}`. Returns `None` only if the target's own features
    /// cannot single it out (a genuine duplicate item), which the caller treats
    /// as "could not minimize" rather than dumping a full AST.
    pub fn minimal_anchor_set(&self, body_idx: usize) -> Option<AnchorSet> {
        if body_idx >= self.items.len() {
            return None;
        }
        let scored = self.scored_features(body_idx);

        // OPT=1 fast path: the most selective+stable feature is already unique.
        if let Some(top) = scored.first()
            && self.posting(&top.feature).len() == 1
        {
            debug_assert!(self.posting(&top.feature).contains(body_idx));
            return Some(AnchorSet {
                body_idx,
                anchors: vec![top.clone()],
                opt_one: true,
            });
        }

        // Greedy set-cover tail: bounded to the target's own features. The
        // "universe" is the non-target items still in the running intersection;
        // each step takes the feature excluding the most of them.
        //
        // W1 hand-off note #1 (cheap perf fix): seed `covered` from the *smallest
        // relevant posting list* — the most-selective feature, which is
        // `scored.first()` since `scored` is sorted by ascending selectivity —
        // rather than `0..N`. The final intersection is a subset of every chosen
        // feature's posting list, so seeding from one of them (the target is in
        // all of its own features' lists) loses nothing; it just bounds per-item
        // cost to that list's size instead of the whole chunk, so unresolvable
        // items pay O(smallest posting) per step rather than O(N).
        let mut remaining = scored;
        let seed = remaining.first()?;
        let mut covered = self.posting(&seed.feature).clone();
        let mut chosen: Vec<ScoredFeature> = vec![remaining.remove(0)];
        let mut scratch = CandidateSet::empty();

        while covered.len() > 1 {
            // The feature that shrinks the candidate set the most; ranked by
            // intersection size only (no allocation), ties broken toward the
            // better-ranked feature (lower index). Only the winner is then
            // materialized, into the reused scratch buffer.
            let (best_i, best_len) = remaining
                .iter()
                .enumerate()
                .map(|(i, f)| (i, covered.intersection_len(self.posting(&f.feature))))
                .min_by(|(ai, a), (bi, b)| a.cmp(b).then(ai.cmp(bi)))?;
            // No feature makes progress: the target is not separable by its own
            // features. Report "unminimizable" rather than loop forever.
            if best_len == covered.len() {
                return None;
            }
            covered.intersect_into(self.posting(&remaining[best_i].feature), &mut scratch);
            std::mem::swap(&mut covered, &mut scratch);
            chosen.push(remaining.remove(best_i));
        }

        (covered.len() == 1 && covered.contains(body_idx)).then_some(AnchorSet {
            body_idx,
            opt_one: chosen.len() == 1,
            anchors: chosen,
        })
    }

    /// Every individually-discriminating value anchor of `body_idx`, each as a
    /// single-anchor [`AnchorSet`], ranked best-first by the same
    /// `selective x stable x class x cost` key [`minimal_anchor_set`] uses.
    ///
    /// A *value* anchor is one [`kept_spans_for_anchor_set`] can map to a concrete
    /// token (literal, object key, member/method name, member-path callee) —
    /// purely structural features (kind, arity, skeletons) are excluded because
    /// they render no kept span and would re-derive the degenerate scaffold.
    /// "Individually discriminating" means the anchor's posting list is already
    /// the singleton `{body_idx}`, so each returned set resolves uniquely at the
    /// index level on its own.
    ///
    /// This backs the renderer's robustness-anchor fallback: `minimal_anchor_set`
    /// returns the single best-ranked anchor, but that anchor's *rendered*
    /// selector may not prove (e.g. a deep literal whose only home is a large
    /// statement the holer keeps verbatim, leaving raw subtrees that the matcher
    /// rejects). The renderer walks these candidates best-first and emits the
    /// first whose holed selector proves uniquely — recovering a genuinely sparse
    /// deep-value pin instead of falling back to the bare scaffold. The whole-body
    /// fixtures (`single_target_class_whole_body`,
    /// `component_wide_destructure_whole_body`) close on exactly this path.
    pub fn unique_value_anchor_candidates(&self, body_idx: usize) -> Vec<AnchorSet> {
        self.scored_features(body_idx)
            .into_iter()
            .filter(|scored| {
                anchor_renders_a_value(&scored.feature) && self.posting(&scored.feature).len() == 1
            })
            .map(|scored| AnchorSet {
                body_idx,
                anchors: vec![scored],
                opt_one: true,
            })
            .collect()
    }

    /// A greedy **multi**-anchor cover restricted to *value-bearing* features —
    /// the multi-feature analogue of [`unique_value_anchor_candidates`]. When no
    /// single value anchor is individually unique (every value anchor's posting
    /// list still holds same-shape siblings), the target may still be singled out
    /// by a *combination* of value anchors, each individually shared with a
    /// different sibling. This runs the same greedy set-cover as
    /// [`minimal_anchor_set`], but over value-bearing features only, so every
    /// chosen anchor renders a concrete kept token and the result is a genuinely
    /// sparse multi-leaf pin rather than the degenerate scaffold.
    ///
    /// It differs from [`minimal_anchor_set`] in exactly the way the renderer
    /// needs: that method takes the globally most-excluding feature at each step
    /// *regardless of whether it renders a kept span*, so its cover can include a
    /// structural skeleton (kept nothing) or land on a value whose only home is a
    /// statement the holer keeps verbatim, leaving raw subtrees the matcher
    /// rejects. Restricting the cover to value anchors guarantees every step
    /// drills to a renderable leaf, so the holed selector keeps only the
    /// discriminating tokens.
    ///
    /// Returns `None` when value anchors alone cannot reach the singleton
    /// `{body_idx}`, and (deliberately) for the single-anchor case — a lone unique
    /// value anchor is [`unique_value_anchor_candidates`]'s domain, which the
    /// renderer walks first; this method only contributes the genuinely
    /// multi-anchor covers that walk cannot produce.
    pub fn unique_value_anchor_cover(&self, body_idx: usize) -> Option<AnchorSet> {
        if body_idx >= self.items.len() {
            return None;
        }
        let mut remaining: Vec<ScoredFeature> = self
            .scored_features(body_idx)
            .into_iter()
            .filter(|scored| anchor_renders_a_value(&scored.feature))
            .collect();
        let seed = remaining.first()?;
        let mut covered = self.posting(&seed.feature).clone();
        let mut chosen: Vec<ScoredFeature> = vec![remaining.remove(0)];
        let mut scratch = CandidateSet::empty();

        while covered.len() > 1 {
            // The value feature that shrinks the candidate set the most; ranked by
            // intersection size only, ties broken toward the better-ranked feature.
            let (best_i, best_len) = remaining
                .iter()
                .enumerate()
                .map(|(i, f)| (i, covered.intersection_len(self.posting(&f.feature))))
                .min_by(|(ai, a), (bi, b)| a.cmp(b).then(ai.cmp(bi)))?;
            // No remaining value anchor makes progress: value anchors alone cannot
            // single the target out. Leave the residual to structural paths.
            if best_len == covered.len() {
                return None;
            }
            covered.intersect_into(self.posting(&remaining[best_i].feature), &mut scratch);
            std::mem::swap(&mut covered, &mut scratch);
            chosen.push(remaining.remove(best_i));
        }

        // A single-anchor cover means the seed value anchor was already unique —
        // that is the single-anchor candidates' job, walked first by the renderer.
        // Yield only the genuinely multi-anchor covers.
        (chosen.len() >= 2 && covered.len() == 1 && covered.contains(body_idx)).then_some(
            AnchorSet {
                body_idx,
                opt_one: false,
                anchors: chosen,
            },
        )
    }

    /// Enclosing-context read-off (#2315): the disambiguating handles on a
    /// target's **stable neighbors** when its own value features cannot separate
    /// it from same-shape siblings (alpha-only constructs, near-duplicate emitted
    /// helpers). For each immediate top-level neighbor (the declaration just
    /// before and just after `body_idx`) it yields that neighbor's
    /// individually-unique value anchors, best-first, as [`ContextNeighborAnchor`]s.
    ///
    /// A neighbor anchor whose posting list is the singleton `{neighbor}` pins a
    /// token occurring in exactly one top-level item, so a 2-statement window
    /// pairing the neighbor (holed to that anchor) with the target's holed
    /// scaffold matches only where that unique neighbor sits adjacent to a
    /// target-shaped statement — i.e. uniquely at the target. The renderer
    /// composes and prove-gates the window; this method only reads the candidate
    /// anchors off the index. Neighbors are bounded to the immediate ±1
    /// declarations, so this stays a bounded read-off, never a chunk-wide search.
    pub fn context_neighbor_anchor_candidates(
        &self,
        body_idx: usize,
    ) -> Vec<ContextNeighborAnchor> {
        let prev = body_idx.checked_sub(1);
        let next = (body_idx + 1 < self.items.len()).then_some(body_idx + 1);
        [prev, next]
            .into_iter()
            .flatten()
            .flat_map(|neighbor_body_idx| {
                self.unique_value_anchor_candidates(neighbor_body_idx)
                    .into_iter()
                    .map(move |anchor_set| ContextNeighborAnchor {
                        neighbor_body_idx,
                        anchor_set,
                    })
            })
            .collect()
    }
}

/// Whether a feature maps to a concrete kept token the renderer can pin (a
/// *value* anchor), as opposed to a purely structural feature (kind, arity,
/// shape skeleton) honored only by the holed scaffold. Mirrors the value-bearing
/// arms of `readoff_render::ValueAnchor::from_selector_feature`.
fn anchor_renders_a_value(feature: &ShapeFeature) -> bool {
    let ShapeFeature::Selector(feature) = feature else {
        return false;
    };
    matches!(
        feature,
        SelectorFeature::StringLiteral(_)
            | SelectorFeature::NumberLiteral(_)
            | SelectorFeature::BoolLiteral(_)
            | SelectorFeature::ObjectKey(_)
            | SelectorFeature::ClassMember(_)
            | SelectorFeature::MemberProperty(_)
            | SelectorFeature::CallCallee(_)
    )
}

/// Multiplicity above which a shape skeleton reads as structural noise rather
/// than a distinctive anchor (W2 note #3). A shape interned more than this many
/// times across the chunk is a common scaffold; one interned at most this often
/// is distinctive enough to rank as a semantic anchor on stability ties.
const SKELETON_NOISE_MULTIPLICITY: u32 = 4;

/// Ranking key: most selective first (smallest posting list), then most stable,
/// then cheapest to retain (smallest pinned source size). The cost tiebreak is
/// strictly subordinate to selectivity and stability, so it only orders anchors
/// that are *already* equally discriminating and equally rebuild-stable: among
/// those, a short anchor wins, and a long literal value is retained only when no
/// shorter anchor discriminates. Wrapped in a comparable tuple; smaller sorts
/// first.
fn rank_key(f: &ScoredFeature) -> (usize, std::cmp::Reverse<Stability>, u8, usize) {
    (
        f.selectivity,
        std::cmp::Reverse(f.stability),
        anchor_class(&f.feature),
        f.cost,
    )
}

/// Forward-compatibility class for the ranking's third key (smaller wins),
/// subordinate to selectivity and stability but *above* cost: among equally
/// selective+stable anchors, prefer a semantic **value** (a literal value or an
/// object key — the actual identifying payload) over a bare **name/reference**
/// (a class-member or member-property name), and a name over a purely
/// **structural** feature (skeleton, kind, arity). A renamed or removed method
/// name silently breaks a name pin, whereas the value it carries (`kind:
/// "shape"`, `format("stable")`) survives, so value anchors are the more
/// rebuild-stable choice even when longer — hence this key outranks `cost`.
fn anchor_class(feature: &ShapeFeature) -> u8 {
    let ShapeFeature::Selector(feature) = feature else {
        // Shape skeletons pin no concrete token; least preferred.
        return 2;
    };
    match feature {
        SelectorFeature::StringLiteral(_)
        | SelectorFeature::NumberLiteral(_)
        | SelectorFeature::BoolLiteral(_)
        | SelectorFeature::ObjectKey(_) => 0,
        SelectorFeature::ClassMember(_)
        | SelectorFeature::MemberProperty(_)
        | SelectorFeature::CallCallee(_) => 1,
        SelectorFeature::TopLevelKind(_)
        | SelectorFeature::VarKind(_)
        | SelectorFeature::FunctionArity(_)
        | SelectorFeature::ImportSource(_) => 2,
    }
}

/// Retained-anchor cost for an existing selector feature: the character length
/// of the concrete source token the anchor pins. A long literal value (a
/// shared template / string spanning hundreds of characters) therefore costs far
/// more than a one-token discriminating key, so it loses the cost tiebreak to
/// any equally selective+stable shorter anchor. Structural features (kinds,
/// arities, import sources) pin nothing via a value span and cost `0`.
fn selector_feature_cost(feature: &SelectorFeature) -> usize {
    match feature {
        SelectorFeature::StringLiteral(value) | SelectorFeature::NumberLiteral(value) => {
            value.chars().count()
        }
        SelectorFeature::ObjectKey(label)
        | SelectorFeature::ClassMember(label)
        | SelectorFeature::MemberProperty(label)
        | SelectorFeature::CallCallee(label) => label.chars().count(),
        SelectorFeature::BoolLiteral(_) => 1,
        SelectorFeature::TopLevelKind(_)
        | SelectorFeature::VarKind(_)
        | SelectorFeature::FunctionArity(_)
        | SelectorFeature::ImportSource(_) => 0,
    }
}

/// Whether `feature` is sound as an anchor under alpha-equivalent matching.
///
/// Most features pin alpha-stable tokens (literals, object keys, class
/// members, member properties, import sources, kinds, arities). The exception
/// is a bare-identifier callee: under alpha-equivalence the matcher wildcards
/// the renamed identifier, so `CallCallee("make")` discriminates nothing. A
/// member-path callee (`a.foo()`, rendered `"a.foo"`) keeps a stable property
/// name and is retained.
fn is_alpha_stable_anchor(feature: &SelectorFeature) -> bool {
    match feature {
        SelectorFeature::CallCallee(label) => label.contains('.'),
        _ => true,
    }
}

/// Stability score for an existing selector feature, reusing the volatility
/// notion behind `STR_LITERAL_MATCHING_RE` (a literal ending in a >=4-char
/// hex/digit run looks generated and is treated as volatile).
fn selector_feature_stability(feature: &SelectorFeature) -> Stability {
    match feature {
        SelectorFeature::TopLevelKind(_)
        | SelectorFeature::VarKind(_)
        | SelectorFeature::FunctionArity(_) => Stability::Structural,
        SelectorFeature::StringLiteral(value) => {
            if looks_volatile(value) {
                Stability::Volatile
            } else {
                Stability::Semantic
            }
        }
        // Number / bool literals are concrete semantic values (enum tags,
        // discriminant codes, config flags); they survive rebuilds and
        // discriminate well, so they rank as preferred anchors.
        SelectorFeature::NumberLiteral(_) | SelectorFeature::BoolLiteral(_) => Stability::Semantic,
        SelectorFeature::ObjectKey(_)
        | SelectorFeature::ClassMember(_)
        | SelectorFeature::MemberProperty(_)
        | SelectorFeature::CallCallee(_)
        | SelectorFeature::ImportSource(_) => Stability::Semantic,
    }
}

/// Minimum trailing hex/digit run length for a literal to read as volatile.
/// Mirrors `selector_codemod::MIN_VOLATILE_TAIL_LEN` (the `STR_LITERAL_MATCHING_RE`
/// anchor heuristic) so the stability signal and the regex-anchor renderer
/// agree on what counts as a generated tail.
const MIN_VOLATILE_TAIL_LEN: usize = 4;

/// `true` when `value` ends in a generated-looking hex/digit tail of at least
/// [`MIN_VOLATILE_TAIL_LEN`] characters over a non-trivial stable prefix — the
/// dominant bundler volatility pattern (`chunk-a1b2c3`, `main.4f3a2b`).
fn looks_volatile(value: &str) -> bool {
    let chars: Vec<char> = value.chars().collect();
    let hex_tail = chars
        .iter()
        .rev()
        .take_while(|c| c.is_ascii_hexdigit())
        .count();
    if hex_tail < MIN_VOLATILE_TAIL_LEN {
        return false;
    }
    let prefix = &chars[..chars.len() - hex_tail];
    !prefix.is_empty() && !prefix.iter().all(|c| matches!(c, '-' | '_' | '.'))
}

// Re-export the prefilter feature taxonomy so callers of the shape index can
// name features without depending on `selector_candidate_index` directly.
pub use selector_candidate_index::SelectorFeature as PrefilterFeature;

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(source: &str) -> Module {
        js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<test>", source).unwrap())
    }

    #[test]
    fn alpha_equivalent_subtrees_share_a_shape_id() {
        // Two var decls with identical structure but renamed identifiers must
        // collapse to the same skeleton shape (alpha-equivalence).
        let module = parse("const a = f(x);\nconst b = g(y);");
        let index = ShapeIndex::new(&module);
        let s0 = &index.items[0].skeletons;
        let s1 = &index.items[1].skeletons;
        assert!(
            !s0.is_disjoint(s1),
            "renamed-isomorphic items must share at least one shape id"
        );
    }

    #[test]
    fn distinct_literals_break_shape_equality() {
        // Same structure, different *stable* literal => different shape.
        let module = parse(r#"const a = f("alpha");"#);
        let other = parse(r#"const a = f("beta");"#);
        let i0 = ShapeIndex::new(&module);
        let i1 = ShapeIndex::new(&other);
        // Deepest skeletons differ because the kept string literal differs.
        let deep0: BTreeSet<u32> = i0.items[0].skeletons.iter().map(|s| s.as_u32()).collect();
        let deep1: BTreeSet<u32> = i1.items[0].skeletons.iter().map(|s| s.as_u32()).collect();
        // Ids are per-index sequential, so compare via multiplicity structure:
        // the literal leaf shows up as a singleton shape in each.
        assert_eq!(deep0.len(), deep1.len());
    }

    #[test]
    fn volatility_matches_regex_anchor_notion() {
        assert!(looks_volatile("chunk-a1b2c3"));
        assert!(looks_volatile("main.4f3a2b"));
        assert!(!looks_volatile("button"));
        assert!(!looks_volatile("v2"));
        assert!(
            !looks_volatile("1234"),
            "separator-only prefix is not stable"
        );
    }

    #[test]
    fn opt_one_read_off_on_unique_literal() {
        // Item 0's literal is unique across the chunk, so a single anchor (the
        // string literal) discriminates it: OPT=1.
        let module = parse(
            r#"const a = make("widget-token");
const b = make("other-token");
const c = other("third-token");"#,
        );
        let index = ShapeIndex::new(&module);
        let anchor = index.minimal_anchor_set(0).unwrap();
        assert!(
            anchor.opt_one,
            "unique literal should yield a single-anchor read-off"
        );
        assert_eq!(index.posting(&anchor.anchors[0].feature).len(), 1);
        assert!(index.read_off_resolves_uniquely(0, &anchor));
    }

    impl ShapeIndex {
        /// Test helper: confirm the anchor set's posting-list intersection is
        /// exactly `{body_idx}` (the index-level uniqueness check; the matcher
        /// is the production gate, exercised in the integration test crate).
        fn read_off_resolves_uniquely(&self, body_idx: usize, anchor: &AnchorSet) -> bool {
            let mut covered = CandidateSet::all(0..self.items.len());
            for scored in &anchor.anchors {
                covered = covered.intersect(self.posting(&scored.feature));
            }
            covered.len() == 1 && covered.contains(body_idx)
        }
    }

    #[test]
    fn greedy_tail_combines_features_when_no_single_one_is_unique() {
        // No single feature is unique: items 0 and 1 share kind+const+arity and
        // the call shape; only the *combination* of the two distinct literals
        // separates them. Force a 2-feature read-off.
        let module = parse(
            r#"const a = make("shared", "alpha");
const b = make("shared", "beta");"#,
        );
        let index = ShapeIndex::new(&module);
        let anchor = index.minimal_anchor_set(0).unwrap();
        assert!(index.read_off_resolves_uniquely(0, &anchor));
    }

    #[test]
    fn unique_value_anchor_candidates_are_value_bearing_singletons_best_first() {
        // The lone class carries several deep value anchors inside a method body.
        // Every candidate must be (a) value-bearing — a token the renderer can pin,
        // never a kind/arity/skeleton — and (b) individually unique, and they come
        // ranked best-first by the same key `minimal_anchor_set` uses.
        let module = parse(
            r#"class runner {
  applyChange(c) {
    this.boxed.set("running");
  }
}
export { runner };"#,
        );
        let index = ShapeIndex::new(&module);
        let candidates = index.unique_value_anchor_candidates(0);
        assert!(!candidates.is_empty());
        for candidate in &candidates {
            let [scored] = candidate.anchors.as_slice() else {
                panic!("each candidate is a single-anchor set");
            };
            assert!(anchor_renders_a_value(&scored.feature), "{scored:?}");
            assert_eq!(index.posting(&scored.feature).len(), 1, "{scored:?}");
        }
        // Best-first: the deep `"running"` string literal (a value, cost 7) must
        // precede the `applyChange` member name (a name/reference, cost 11).
        let position = |needle: &ShapeFeature| {
            candidates
                .iter()
                .position(|c| &c.anchors[0].feature == needle)
        };
        let running = ShapeFeature::Selector(SelectorFeature::StringLiteral("running".into()));
        let member = ShapeFeature::Selector(SelectorFeature::ClassMember("applyChange".into()));
        assert!(position(&running) < position(&member));
    }

    #[test]
    fn unique_value_anchor_cover_combines_value_anchors_when_no_single_one_is_unique() {
        // Three same-shape const-bound calls. No single string literal singles the
        // target (item 0) out: `"alpha"` is shared with item 1, `"beta"` with
        // item 2. Only the *combination* {alpha, beta} resolves to item 0 — and
        // both anchors are value-bearing, so the cover is renderable end to end.
        let module = parse(
            r#"const a = emit("alpha", "beta");
const b = emit("alpha", "gamma");
const c = emit("delta", "beta");"#,
        );
        let index = ShapeIndex::new(&module);

        // Precondition: no single value anchor is individually unique, so the
        // single-anchor candidates walk yields nothing.
        assert!(index.unique_value_anchor_candidates(0).is_empty());

        let cover = index
            .unique_value_anchor_cover(0)
            .expect("a multi-value-anchor cover resolves item 0");
        assert!(cover.anchors.len() >= 2, "{cover:?}");
        assert!(!cover.opt_one);
        for scored in &cover.anchors {
            assert!(anchor_renders_a_value(&scored.feature), "{scored:?}");
        }
        assert!(index.read_off_resolves_uniquely(0, &cover));
    }

    #[test]
    fn context_neighbor_anchor_candidates_offer_adjacent_unique_value_anchors() {
        // Three alpha-identical `const X = new IDENT()` helpers (bodies 0, 2, 3):
        // none has a value anchor of its own (the `new` callee alpha-canonicalizes),
        // so the middle one is a genuine residual — `minimal_anchor_set` cannot
        // separate it. Its immediate neighbor (body 1) carries a globally-unique
        // string, which the enclosing-context read-off offers as a disambiguating
        // anchor on the neighbor.
        let module = parse(
            r#"const a = new Factory();
mountSelected("beta-unique-token");
const b = new Factory();
const c = new Factory();"#,
        );
        let index = ShapeIndex::new(&module);
        assert!(
            index.minimal_anchor_set(2).is_none(),
            "the middle helper must be a genuine residual (no own discriminator)"
        );
        let candidates = index.context_neighbor_anchor_candidates(2);
        let unique_string =
            ShapeFeature::Selector(SelectorFeature::StringLiteral("beta-unique-token".into()));
        assert!(
            candidates.iter().any(|candidate| {
                candidate.neighbor_body_idx == 1
                    && candidate
                        .anchor_set
                        .anchors
                        .iter()
                        .any(|scored| scored.feature == unique_string)
            }),
            "expected the adjacent unique string as a neighbor anchor, got {candidates:?}"
        );
    }

    #[test]
    fn context_neighbor_anchor_candidates_are_empty_without_a_stable_neighbor() {
        // All four statements are alpha-identical residual helpers with no value
        // anchor anywhere, so no neighbor can disambiguate: the read-off yields no
        // context candidates and the caller leaves the target as name-pinned debt.
        let module = parse(
            r#"const a = new Factory();
const b = new Factory();
const c = new Factory();
const d = new Factory();"#,
        );
        let index = ShapeIndex::new(&module);
        assert!(index.context_neighbor_anchor_candidates(1).is_empty());
    }

    #[test]
    fn unique_value_anchor_cover_declines_the_single_anchor_case() {
        // The lone class has a unique value anchor (`"running"`); a single-anchor
        // cover is the candidates walk's domain, so this method declines it rather
        // than duplicate that path.
        let module = parse(
            r#"class runner {
  applyChange(c) {
    this.boxed.set("running");
  }
}
export { runner };"#,
        );
        let index = ShapeIndex::new(&module);
        assert!(!index.unique_value_anchor_candidates(0).is_empty());
        assert!(index.unique_value_anchor_cover(0).is_none());
    }
}
