use super::*;

/// One value per syntactic position bucket. Collapses the repeated
/// eager / lazy / first-order-lazy triples (reads, rebinds, calls)
/// into a single shape so the "first-order ⊆ lazy" subset invariant
/// lives in one place ([`Self::record`]) instead of at every
/// construction site.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PositionBucketed<T> {
    /// At-init position: outside any function/arrow/method/getter/
    /// setter body, constructor body, or instance class-field
    /// initializer.
    pub eager: T,
    /// Inside a lazy syntactic position, at any nesting depth. May
    /// overlap with `eager` if the same name appears in both eager
    /// and lazy positions of the statement.
    pub lazy: T,
    /// Subset of `lazy` whose sites sit in a function's
    /// **first-order, pre-await** body (depth 1 from this statement).
    /// Used by at-init call promotion: a synchronous call to the
    /// function only runs its immediate pre-await body, so sites
    /// inside nested function/arrow definitions or past an `await`
    /// don't promote to the caller.
    pub first_order_lazy: T,
}

impl PositionBucketed<BTreeSet<Id>> {
    /// Record `id` in the bucket the cursor position selects: `eager`
    /// at depth 0; `lazy` at depth ≥ 1, additionally `first_order_lazy`
    /// at depth 1 before the body's first `await`. Maintains the
    /// `first_order_lazy ⊆ lazy` invariant structurally.
    pub(crate) fn record(&mut self, id: &Id, lazy_depth: u32, past_await: bool) {
        if lazy_depth == 0 {
            self.eager.insert(id.clone());
            return;
        }
        self.lazy.insert(id.clone());
        if lazy_depth == 1 && !past_await {
            self.first_order_lazy.insert(id.clone());
        }
    }

    pub(crate) fn extend(&mut self, other: Self) {
        self.eager.extend(other.eager);
        self.lazy.extend(other.lazy);
        self.first_order_lazy.extend(other.first_order_lazy);
    }
}

#[derive(Debug, Clone)]
pub struct StatementFacts {
    pub ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    /// Identifier reads, bucketed by syntactic position.
    pub reads: PositionBucketed<BTreeSet<Id>>,
    /// Rebinding writes, bucketed by syntactic position. Member
    /// writes (`obj.x = ...`) are intentionally excluded: mutating an
    /// imported object is legal, but rebinding the imported binding
    /// cell is not.
    pub rebinds: PositionBucketed<BTreeSet<Id>>,
    /// Target-local mutations produced by recognized trusted helper
    /// calls. Each binding is the class/prototype owner that must
    /// co-locate with the mutating statement.
    pub local_effects: BTreeSet<Id>,
    /// Bare-identifier callees of `CallExpr` nodes, bucketed by
    /// syntactic position. Indirect calls (`const g = f; g()`),
    /// method calls (`obj.method()`), and computed callees are
    /// skipped — the callee must be a direct `Ident`. The owner-graph
    /// build uses `eager` to drive at-init call promotion (a call
    /// from statement S to chunk-declared function `f` transitively
    /// reads everything `f`'s body lazily reads — see docs/design.md
    /// "At-init call promotion") and `lazy` to reconstruct the chunk
    /// call graph so promotion follows call chains (e.g.
    /// `function f() { g(); } f();` promotes through `g`'s body too).
    /// `first_order_lazy` keeps calls nested inside a closure of the
    /// body from appearing as direct callees of the outer function.
    pub calls: PositionBucketed<BTreeSet<Id>>,
    /// Bindings referenced in the callee or arguments of at-init
    /// calls promotion can't follow (member calls `api.read()`,
    /// optional-chain calls, tagged templates). A function value
    /// invoked by such a call must have flowed through one of these
    /// bindings — or through an inline function expression (see
    /// `at_init_unresolved_inline_fn`) — so `promote_at_init_calls`
    /// makes the statement eagerly depend on the transitive lazy
    /// closures of the chunk-declared subset.
    pub at_init_unresolved_sources: BTreeSet<Id>,
    /// `true` when an unresolved at-init call carries an inline
    /// function/arrow/class expression: the statement's own lazy
    /// closures may fire synchronously (IIFE,
    /// `arr.forEach(x => ...)`).
    pub at_init_unresolved_inline_fn: bool,
    /// First-order pre-await body counterpart of
    /// `at_init_unresolved_sources`; propagated to at-init callers
    /// through the promotion call graph.
    pub first_order_unresolved_sources: BTreeSet<Id>,
    /// First-order pre-await body counterpart of
    /// `at_init_unresolved_inline_fn`.
    pub first_order_unresolved_inline_fn: bool,
    /// `true` when the statement's declared binding is directly a
    /// function value: a `function` declaration (incl. exported and
    /// `export default` forms) or a single-declarator
    /// `var/let/const` whose initializer is a function/arrow
    /// expression. Promotion treats only at-init calls to such
    /// bindings (when never rebound) as precisely resolvable;
    /// everything else takes the conservative fallback.
    pub declares_direct_function: bool,
    /// Static-key `globalThis.<prop>` cells the statement writes
    /// at-init (`globalThis.tag = ...` records `"tag"`). Dynamic-key
    /// accesses bail `cell_writes_summarizable` instead.
    pub global_writes: BTreeSet<String>,
    /// Static-key `globalThis.<prop>` cells the statement reads
    /// at-init.
    pub global_reads: BTreeSet<String>,
    /// `false` when the statement contains a shape that defeats any
    /// static reasoning about which cells it WRITES (`with`, direct
    /// or indirect `eval`, `Function(...)`, dynamic-key
    /// `globalThis[expr]`, `defineProperty`/`Proxy` on the global).
    /// Consumed by the vendor strip's swap-privacy gate, whose call
    /// side effects are covered by its own island-reachability
    /// analysis.
    pub cell_writes_summarizable: bool,
    /// `false` whenever `cell_writes_summarizable` is `false`, and
    /// additionally for shapes that defeat the dataflow-aware
    /// S-chain's stronger "which cells does this statement TOUCH"
    /// question: opaque (not classifier-Pure) at-init calls/news
    /// (I/O is not a cell; callee bodies may touch globals), member
    /// writes through bindings (aliasing), and statements tainted by
    /// a global-object alias escape. Downstream passes must treat
    /// the statement as touching every cell. See `README.md` →
    /// "Conditionally-correct optimizations" for the soundness
    /// precondition.
    pub dataflow_summarizable: bool,
    pub purity: Purity,
    pub kind: StatementKind,
}

impl StatementFacts {
    /// Per-statement (writes, reads) cell summary used by the
    /// dataflow-aware S-chain emission in `graph.rs` and the vendor
    /// strip's swap-privacy gate. Derived on demand: the
    /// `Binding`-cell half restates `declared` / `reads.eager` /
    /// `rebinds.eager`; only the `GlobalProp` half (`global_writes` /
    /// `global_reads`) is stored state.
    pub fn effects(&self) -> StatementEffectSummary {
        let mut writes = BTreeSet::<EffectCell>::new();
        for name in self.declared.iter().chain(self.rebinds.eager.iter()) {
            writes.insert(EffectCell::Binding(name.clone()));
        }
        for key in &self.global_writes {
            writes.insert(EffectCell::GlobalProp(key.clone()));
        }
        let mut reads = BTreeSet::<EffectCell>::new();
        for name in &self.reads.eager {
            reads.insert(EffectCell::Binding(name.clone()));
        }
        for key in &self.global_reads {
            reads.insert(EffectCell::GlobalProp(key.clone()));
        }
        StatementEffectSummary { writes, reads }
    }
}

/// One outer-observable storage location a statement can read or
/// write. `EffectCell::Binding(name)` covers identifier reads
/// (`Foo` or `Foo.bar` triggers a read of `Foo`) and rebind writes;
/// `EffectCell::GlobalProp(key)` covers static-key writes/reads on
/// `globalThis` (`globalThis.tag = ...` is `GlobalProp("tag")`).
/// Dynamic-keyed accesses (`globalThis[expr]`) are deliberately not
/// representable here — statements containing them are marked
/// non-summarizable.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum EffectCell {
    Binding(Id),
    GlobalProp(String),
}

/// The (writes, reads) cell view of one statement, derived by
/// [`StatementFacts::effects`]. The summarizability bits live on
/// [`StatementFacts`] directly (`cell_writes_summarizable`,
/// `dataflow_summarizable`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StatementEffectSummary {
    pub writes: BTreeSet<EffectCell>,
    pub reads: BTreeSet<EffectCell>,
}

#[derive(Debug, Clone)]
pub struct ChunkFactAnalysis {
    pub facts: Vec<StatementFacts>,
    pub top_level_await: Option<StatementOrdinal>,
    /// Author-declared `purity: pure` spec hints the analyzer
    /// determines would be inferred automatically by recursive
    /// purity — e.g. the callee's body classifies `Pure` even
    /// without the override, or the binding admits as `PlainData`
    /// such that the callsite hint is a no-op. Surfaced so the spec
    /// author can prune the hint and shrink the trust surface.
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
    /// Author-declared `pure_members: [<prop>, …]` entries the
    /// analyzer would classify pure without the hint — currently
    /// limited to `(WHITELIST_RECEIVERS, PURE_STATIC_CALLS-prop)`
    /// pairs (e.g. `pure_members: [isArray]` on a binding named
    /// `Array`). Surfaced for the same trust-surface-shrinking
    /// reason as `redundant_purity_hints`.
    pub redundant_pure_member_hints: Vec<RedundantPureMemberHint>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize, strum::IntoStaticStr)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum StatementKind {
    /// `var X = ...`, `let X = ...`, `const X = ...`. RHS reads at-init.
    VarDecl,
    /// `function X() { ... }`. Hoisted; no at-init reads from body.
    FnDecl,
    /// `class X { ... }`. Extends, decorators, computed keys, and
    /// static blocks read at-init.
    ClassDecl,
    /// `export { ... }`, `export X`, etc. Lazy reads (re-exports).
    Export,
    /// `import { ... } from ...`. Linked, no at-init body code.
    Import,
    /// Bare expression / control-flow / etc. that doesn't declare a
    /// top-level binding.
    SideEffect,
}

/// Policy-independent per-statement facts: everything the analyzer
/// can compute about a top-level statement from the module text alone,
/// without consulting `AnalysisHints`. Produced by
/// [`analyze_chunk_structural`] and consumed by
/// [`analyze_chunk_with_policy`] to assemble the full
/// [`StatementFacts`]. See the doc comment on
/// [`analyze_chunk_structural`] for why this layer exists.
#[derive(Debug, Clone)]
pub(crate) struct StructuralStatementFacts {
    pub(crate) ordinal: StatementOrdinal,
    pub(crate) source_location: Option<SourceLocation>,
    pub(crate) kind: StatementKind,
    pub(crate) declared: BTreeSet<Id>,
    pub(crate) reads: PositionBucketed<BTreeSet<Id>>,
    pub(crate) rebinds: PositionBucketed<BTreeSet<Id>>,
    pub(crate) calls: PositionBucketed<BTreeSet<Id>>,
    pub(crate) at_init_unresolved_sources: BTreeSet<Id>,
    pub(crate) at_init_unresolved_inline_fn: bool,
    pub(crate) first_order_unresolved_sources: BTreeSet<Id>,
    pub(crate) first_order_unresolved_inline_fn: bool,
    pub(crate) declares_direct_function: bool,
    pub(crate) global_writes: BTreeSet<String>,
    pub(crate) global_reads: BTreeSet<String>,
    pub(crate) cell_writes_summarizable: bool,
    pub(crate) dataflow_summarizable: bool,
}

/// The policy-independent half of [`analyze_chunk`]'s output: the
/// top-level item view, the shadowed-globals set, the top-level-await
/// scan, and the per-statement static facts that depend only on the
/// module text (not on `AnalysisHints`).
///
/// Owns its top-level item view by lifetime-tying to the source
/// module, so the policy-dependent pass can re-traverse the same views
/// without re-running the multi-declarator split.
pub(crate) struct StructuralChunkAnalysis<'a> {
    pub(crate) body: Vec<TopLevelItemView<'a>>,
    pub(crate) shadowed: BTreeSet<&'static str>,
    pub(crate) per_statement: Vec<StructuralStatementFacts>,
    pub(crate) top_level_await: Option<StatementOrdinal>,
}
