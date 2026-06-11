use std::collections::{BTreeMap, BTreeSet};

mod local_effects;
pub mod wire;

pub use local_effects::local_namespace_iife_target;
pub use wire::{ChunkFactsReport, IdReport, StatementFactsReport};

use binding_targets::{
    TargetAccessRecorder, callee_base_expr, declaration_ids, hoisted_var_ids, record_assign_target,
    record_pat_write, record_update_target, strip_parens,
};
use serde::{Deserialize, Serialize};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::analysis_hints::{AnalysisHints, KnownEffect, LocalEffectPolicy};
use crate::purity::{
    ChunkCodeGraph, Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPurityHint,
    SHADOW_TRACKED_GLOBALS, class_has_static_observable, classify_expr_purity,
    classify_var_decl_purity, detect_redundant_pure_member_hints, detect_redundant_purity_hints,
};
use crate::{SourceLocation, StatementOrdinal};

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
    fn record(&mut self, id: &Id, lazy_depth: u32, past_await: bool) {
        if lazy_depth == 0 {
            self.eager.insert(id.clone());
            return;
        }
        self.lazy.insert(id.clone());
        if lazy_depth == 1 && !past_await {
            self.first_order_lazy.insert(id.clone());
        }
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

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
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

/// Walk the chunk's module body and produce one `StatementFacts`
/// entry per top-level statement, in source order.
///
/// Multi-declarator `var/let/const` statements are split into
/// per-declarator entries before analysis, so each row declares
/// a single name and owner-graph destination attribution
/// returns an unambiguous owner. Without the split, a chunk like
/// `const A = 1, B = readsX;` with `{A → mod_a, B → mod_b}`
/// would attribute `B`'s read of `X` to `mod_a` (the first
/// declared name's owner), inventing or hiding cycles. The
/// emitter splits the same comma-lists separately at lower-time
/// (`split_var_decl` in `lowering/util.rs`); this pre-split
/// just teaches the analyzer the same view.
/// Locate the first top-level `await` expression in `module`'s
/// body, if any. Returns the source-order ordinal of the offending
/// statement (in the post-comma-list-split view that
/// `analyze_chunk` uses, so reports align with statement indices
/// in `reports/tree/<chunk_id>/owner_graph.json`).
///
/// "Top-level" excludes function/method/arrow/getter/setter
/// bodies and class instance-field initializers — those are lazy
/// scopes that may legitimately contain `await` without making
/// the module a top-level-await module.
pub fn find_top_level_await(module: &Module) -> Option<StatementOrdinal> {
    let body = top_level_item_views(&module.body);
    for (ordinal, item) in body.iter().enumerate() {
        let mut finder = TopLevelAwaitFinder::default();
        item.as_module_item().visit_with(&mut finder);
        if finder.found {
            return Some(StatementOrdinal(ordinal));
        }
    }
    None
}

#[derive(Default)]
struct TopLevelAwaitFinder {
    found: bool,
}

impl Visit for TopLevelAwaitFinder {
    fn visit_await_expr(&mut self, _node: &AwaitExpr) {
        self.found = true;
    }

    // Lazy boundaries — `await` inside any of these is the body's
    // own concern (and only legal if the body is itself `async`).
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}

    fn visit_class_member(&mut self, member: &ClassMember) {
        visit_eager_member_parts(self, member);
    }
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
    ordinal: StatementOrdinal,
    source_location: Option<SourceLocation>,
    kind: StatementKind,
    declared: BTreeSet<Id>,
    reads: PositionBucketed<BTreeSet<Id>>,
    rebinds: PositionBucketed<BTreeSet<Id>>,
    calls: PositionBucketed<BTreeSet<Id>>,
    at_init_unresolved_sources: BTreeSet<Id>,
    at_init_unresolved_inline_fn: bool,
    first_order_unresolved_sources: BTreeSet<Id>,
    first_order_unresolved_inline_fn: bool,
    declares_direct_function: bool,
    global_writes: BTreeSet<String>,
    global_reads: BTreeSet<String>,
    cell_writes_summarizable: bool,
    dataflow_summarizable: bool,
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
    body: Vec<TopLevelItemView<'a>>,
    shadowed: BTreeSet<&'static str>,
    per_statement: Vec<StructuralStatementFacts>,
    top_level_await: Option<StatementOrdinal>,
}

/// Compute the policy-independent layer of the chunk analysis: item
/// views, shadowed globals, per-statement static facts, and the
/// top-level-await scan. None of these depend on `AnalysisHints`
/// (declared-pure hints, known effects, or local-effect policy).
///
/// The policy-dependent half — [`ChunkCodeGraph::build_full`],
/// local-effect detection, per-statement purity classification, and
/// the redundant-hint diagnostics — runs in
/// [`analyze_chunk_with_policy`].
///
/// **Cross-pass sharing**: this layer is NOT shareable across the two
/// `analyze_chunk` call sites (Stage A composer vs `vendor::strip`)
/// because they analyze different `Module` values. Vendor strip
/// reparses the emitted chunk file from disk and then mutates it
/// (`split_top_level_var_decls`, `strip_export_specifiers`) before
/// analyzing; Stage A analyzes the in-memory lowered runtime AST.
/// Even ignoring the reparse, `strip_export_specifiers` rewrites
/// `ExportNamed` items and folds `ExportDecl` into `Stmt::Decl`, which
/// changes the body view that `top_level_item_views` produces. So this
/// split exists for code-shape clarity, not as a perf optimization.
pub(crate) fn analyze_chunk_structural<'a, F>(
    module: &'a Module,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> StructuralChunkAnalysis<'a>
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let global_object_names = unshadowed_global_object_aliases(&body);
    let mut top_level_await = None;
    let mut per_statement: Vec<StructuralStatementFacts> = body
        .iter()
        .enumerate()
        .map(|(ordinal, item)| {
            let item = item.as_module_item();
            if top_level_await.is_none() {
                let mut finder = TopLevelAwaitFinder::default();
                item.visit_with(&mut finder);
                if finder.found {
                    top_level_await = Some(StatementOrdinal(ordinal));
                }
            }
            let kind = classify_item(item);
            let declared = collect_declared_names(item);
            let mut collector = StatementFactsCollector::new(global_object_names.clone());
            item.visit_with(&mut collector);
            let source_location = source_path.and_then(|source_path| {
                line_range_for_span(item.span()).map(|(start_line, end_line)| SourceLocation {
                    source_path: source_path.to_string(),
                    start_line,
                    end_line,
                })
            });
            StructuralStatementFacts {
                ordinal: StatementOrdinal(ordinal),
                source_location,
                kind,
                declared,
                reads: collector.reads,
                rebinds: collector.rebinds,
                calls: collector.calls,
                at_init_unresolved_sources: collector.at_init_unresolved_sources,
                at_init_unresolved_inline_fn: collector.at_init_unresolved_inline_fn,
                first_order_unresolved_sources: collector.first_order_unresolved_sources,
                first_order_unresolved_inline_fn: collector.first_order_unresolved_inline_fn,
                declares_direct_function: declares_direct_function(item),
                global_writes: collector.global_writes,
                global_reads: collector.global_reads,
                cell_writes_summarizable: collector.cell_writes_summarizable,
                dataflow_summarizable: collector.dataflow_summarizable,
            }
        })
        .collect();
    apply_global_escape_taint(&body, &global_object_names, &mut per_statement);
    StructuralChunkAnalysis {
        body,
        shadowed,
        per_statement,
        top_level_await,
    }
}

/// Identifier names that, when not shadowed by a chunk-top-level
/// declaration or import, evaluate to the global object in the
/// runtimes the debundler targets (browsers: `window` / `self` /
/// `frames` / `top`; Node and workers: `globalThis` / `self`).
const GLOBAL_OBJECT_ALIASES: [&str; 5] = ["globalThis", "window", "self", "frames", "top"];

/// The subset of [`GLOBAL_OBJECT_ALIASES`] no chunk-top-level
/// declaration (incl. block-hoisted `var`s) or import shadows.
/// Block-scoped `let`/`const` redeclarations of an alias *inside* a
/// top-level statement are not detected; treating such an access as
/// global only over-approximates the cell sets (extra `Sequenced`
/// edges), which is sound.
fn unshadowed_global_object_aliases(body: &[TopLevelItemView<'_>]) -> BTreeSet<&'static str> {
    let mut declared: BTreeSet<String> = BTreeSet::new();
    for item in body {
        let item = item.as_module_item();
        for id in collect_declared_names(item) {
            declared.insert(id.0.to_string());
        }
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for spec in &import.specifiers {
                let local = match spec {
                    ImportSpecifier::Named(named) => named.local.sym.as_ref(),
                    ImportSpecifier::Default(default) => default.local.sym.as_ref(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_ref(),
                };
                declared.insert(local.to_string());
            }
        }
    }
    GLOBAL_OBJECT_ALIASES
        .iter()
        .copied()
        .filter(|alias| !declared.contains(*alias))
        .collect()
}

/// Global-object aliasing taint. A statement that lets the global
/// object escape as a *value* (`const g = globalThis;`,
/// `register(window)`, a function body returning `self`) defeats
/// per-cell tracking for every binding the value flows into:
/// `g.tag` reads/writes the same cells as `globalThis.tag` but the
/// per-statement summary only sees `Binding(g)`. Conservative rule:
///
/// 1. A statement containing a bare global-object alias outside
///    member-base position is tainted.
/// 2. Bindings written (declared/rebound) by tainted statements are
///    suspects.
/// 3. Any statement reading a suspect is tainted (fixpoint).
///
/// Tainted statements get `dataflow_summarizable = false` — the
/// dataflow-aware S-chain falls back to the strict adjacent-impure
/// edge for them.
fn apply_global_escape_taint(
    body: &[TopLevelItemView<'_>],
    global_object_names: &BTreeSet<&'static str>,
    per_statement: &mut [StructuralStatementFacts],
) {
    let mut tainted: Vec<bool> = body
        .iter()
        .map(|item| {
            let mut finder = GlobalObjectEscapeFinder {
                names: global_object_names,
                escaped: false,
            };
            item.as_module_item().visit_with(&mut finder);
            finder.escaped
        })
        .collect();
    let mut suspects: BTreeSet<Id> = BTreeSet::new();
    loop {
        let mut changed = false;
        for (idx, facts) in per_statement.iter().enumerate() {
            if !tainted[idx]
                && facts
                    .reads
                    .eager
                    .iter()
                    .chain(facts.reads.lazy.iter())
                    .any(|id| suspects.contains(id))
            {
                tainted[idx] = true;
                changed = true;
            }
            if tainted[idx] {
                for id in facts
                    .declared
                    .iter()
                    .chain(facts.rebinds.eager.iter())
                    .chain(facts.rebinds.lazy.iter())
                {
                    changed |= suspects.insert(id.clone());
                }
            }
        }
        if !changed {
            break;
        }
    }
    for (idx, facts) in per_statement.iter_mut().enumerate() {
        if tainted[idx] {
            facts.dataflow_summarizable = false;
        }
    }
}

/// Detects a global-object alias used as a value. `globalThis.x` /
/// `window[k]` use the alias as a property base — not an escape —
/// so member-base positions are skipped; any other occurrence
/// (initializer, argument, return value, array/object element)
/// counts.
struct GlobalObjectEscapeFinder<'a> {
    names: &'a BTreeSet<&'static str>,
    escaped: bool,
}

impl Visit for GlobalObjectEscapeFinder<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if self.names.contains(node.sym.as_ref()) {
            self.escaped = true;
        }
    }
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        match strip_parens(&node.obj) {
            Expr::Ident(ident) if self.names.contains(ident.sym.as_ref()) => {}
            other => other.visit_with(self),
        }
        node.prop.visit_with(self);
    }
}

/// Compute the policy-dependent half of the chunk analysis from the
/// pre-computed structural layer and a set of `AnalysisHints`. Builds
/// the chunk code graph (hint-gated purity propagation), the
/// local-effect context (gated by `hints.local_effect_policy`), the
/// per-statement purity classification, and the redundant-hint
/// diagnostics. Folds the policy-independent facts in to produce the
/// full [`ChunkFactAnalysis`].
pub(crate) fn analyze_chunk_with_policy<F>(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let StructuralChunkAnalysis {
        body,
        shadowed,
        per_statement,
        top_level_await,
    } = structural;
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &hints.declared_pure,
        &hints.declared_pure_new,
        &hints.declared_pure_members,
        &hints.imported_purities,
        &hints.fluent_bindings,
    );
    let local_effect_context =
        local_effects::LocalEffectContext::for_body(&body, hints.local_effect_policy);
    let redundant_purity_hints =
        detect_redundant_purity_hints(&body, &shadowed, &hints.declared_pure);
    let redundant_pure_member_hints =
        detect_redundant_pure_member_hints(&hints.declared_pure_members);
    let facts = body
        .iter()
        .zip(per_statement)
        .map(|(item_view, structural_facts)| {
            let item = item_view.as_module_item();
            let mut fact = assemble_statement_facts(
                item,
                structural_facts,
                &shadowed,
                hints,
                &graph,
                &local_effect_context,
            );
            // Resolve any reason spans on `fact.purity` to
            // SourceLocations using the same per-chunk line index.
            // Done after fact construction because the classifier
            // doesn't have access to the line resolver.
            if let Some(source_path) = source_path
                && let Purity::NotPure { reasons } = &mut fact.purity
            {
                for reason in reasons.iter_mut() {
                    reason.source_location =
                        line_range_for_span(reason.span).map(|(start_line, end_line)| {
                            SourceLocation {
                                source_path: source_path.to_string(),
                                start_line,
                                end_line,
                            }
                        });
                }
            }
            fact
        })
        .collect();
    ChunkFactAnalysis {
        facts,
        top_level_await,
        redundant_purity_hints,
        redundant_pure_member_hints,
    }
}

pub fn analyze_chunk<F>(
    module: &Module,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let structural = analyze_chunk_structural(module, source_path, &mut line_range_for_span);
    analyze_chunk_with_policy(structural, hints, source_path, line_range_for_span)
}

pub enum TopLevelItemView<'a> {
    Borrowed(&'a ModuleItem),
    Owned(ModuleItem),
}

impl TopLevelItemView<'_> {
    pub fn as_module_item(&self) -> &ModuleItem {
        match self {
            Self::Borrowed(item) => item,
            Self::Owned(item) => item,
        }
    }
}

/// View the top-level body as analysis statements. Multi-declarator
/// top-level `var/let/const` statements are split into N
/// single-declarator statements preserving source order; unchanged
/// statements stay borrowed so the analyzer does not clone the whole
/// app chunk just to get per-declarator ownership.
pub fn top_level_item_views(body: &[ModuleItem]) -> Vec<TopLevelItemView<'_>> {
    let mut out = Vec::with_capacity(body.len());
    for item in body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() > 1 => {
                for decl in &var.decls {
                    let single = VarDecl {
                        span: decl.span,
                        ctxt: var.ctxt,
                        kind: var.kind,
                        declare: var.declare,
                        decls: vec![decl.clone()],
                    };
                    out.push(TopLevelItemView::Owned(ModuleItem::Stmt(Stmt::Decl(
                        Decl::Var(Box::new(single)),
                    ))));
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                match &export_decl.decl {
                    Decl::Var(var) if var.decls.len() > 1 => {
                        for decl in &var.decls {
                            let single = VarDecl {
                                span: decl.span,
                                ctxt: var.ctxt,
                                kind: var.kind,
                                declare: var.declare,
                                decls: vec![decl.clone()],
                            };
                            out.push(TopLevelItemView::Owned(ModuleItem::ModuleDecl(
                                ModuleDecl::ExportDecl(ExportDecl {
                                    span: decl.span,
                                    decl: Decl::Var(Box::new(single)),
                                }),
                            )));
                        }
                    }
                    _ => out.push(TopLevelItemView::Borrowed(item)),
                }
            }
            _ => out.push(TopLevelItemView::Borrowed(item)),
        }
    }
    out
}

/// Walk `body` and collect the subset of `SHADOW_TRACKED_GLOBALS`
/// (the union of every global name any purity-whitelist table keys
/// on — receivers like `Math`/`Object`, global callables like
/// `Boolean`/`Symbol`, pure-new builtins like `Map`/`Set`) that are
/// declared at the chunk's top-level scope (`var/let/const`,
/// `function`, `class`, exported decls) or bound by an import
/// specifier (default / namespace / named). The classifier consults
/// this set to skip the whitelist for any name the chunk shadows —
/// `const Math = …` and `import { Math } from "./userland"` both
/// make `Math.PI` an Unknown read, and `const Map = class { … }`
/// makes `new Map()` an Unknown construction, not the built-in.
/// See docs/design.md A8.
pub(crate) fn compute_shadowed_globals(body: &[TopLevelItemView<'_>]) -> BTreeSet<&'static str> {
    let mut shadowed = BTreeSet::new();
    let try_shadow = |name: &str, into: &mut BTreeSet<&'static str>| {
        if let Some(global) = SHADOW_TRACKED_GLOBALS.get(name) {
            into.insert(*global);
        }
    };
    for item in body {
        let item = item.as_module_item();
        for id in collect_declared_names(item) {
            try_shadow(id.0.as_ref(), &mut shadowed);
        }
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for spec in &import.specifiers {
                let local = match spec {
                    ImportSpecifier::Named(named) => named.local.sym.as_ref(),
                    ImportSpecifier::Default(default) => default.local.sym.as_ref(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_ref(),
                };
                try_shadow(local, &mut shadowed);
            }
        }
    }
    shadowed
}

/// Fold the policy-independent per-statement facts together with the
/// policy-dependent local-effect and purity classifications into a
/// complete [`StatementFacts`] record. Called once per statement by
/// [`analyze_chunk_with_policy`] after the policy-independent layer
/// has already walked the AST.
fn assemble_statement_facts(
    item: &ModuleItem,
    structural: StructuralStatementFacts,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
    local_effect_context: &local_effects::LocalEffectContext,
) -> StatementFacts {
    let StructuralStatementFacts {
        ordinal,
        source_location,
        kind,
        declared,
        reads,
        rebinds,
        calls,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        cell_writes_summarizable,
        dataflow_summarizable,
    } = structural;
    let local_effects = collect_local_effects(
        item,
        &hints.known_effects,
        local_effect_context,
        hints.local_effect_policy,
    );
    let purity = item_purity(
        item,
        kind,
        shadowed,
        hints,
        graph,
        !local_effects.is_empty(),
    );
    // Opaque at-init calls: a call/new the purity classifier can't
    // prove Pure may touch any cell (I/O like `console.log`, global
    // props written inside callee bodies, indirect eval) — the
    // statement must fall back to the strict S-chain. Classifier-Pure
    // calls are exempt: Pure guarantees no observable writes and no
    // global-prop reads, and binding-cell ordering is enforced by
    // binding edges + rebind co-location rather than the S-chain.
    let dataflow_summarizable =
        dataflow_summarizable && !has_opaque_at_init_call(item, shadowed, hints, graph);
    StatementFacts {
        ordinal,
        source_location,
        declared,
        reads,
        rebinds,
        local_effects,
        calls,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        cell_writes_summarizable,
        dataflow_summarizable,
        purity,
        kind,
    }
}

/// Walks a statement looking for an at-init (lazy-depth-0) call/new
/// expression the purity classifier does not prove `Pure`. See the
/// call site in [`assemble_statement_facts`] for the soundness
/// rationale.
struct OpaqueAtInitCallFinder<'a> {
    shadowed: &'a BTreeSet<&'static str>,
    hints: &'a AnalysisHints,
    graph: &'a ChunkCodeGraph,
    found: bool,
    lazy_depth: u32,
    past_await: bool,
}

impl LazyBoundary for OpaqueAtInitCallFinder<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
}

impl Visit for OpaqueAtInitCallFinder<'_> {
    fn visit_expr(&mut self, expr: &Expr) {
        if self.found {
            return;
        }
        if self.lazy_depth == 0 {
            let opaque = match expr {
                Expr::Call(_) | Expr::New(_) => !classify_expr_purity(
                    expr,
                    self.shadowed,
                    &BTreeSet::new(),
                    &self.hints.declared_pure,
                    self.graph,
                )
                .is_pure(),
                // Tagged templates invoke the tag function; the
                // classifier has no pure tag whitelist.
                Expr::TaggedTpl(_) => true,
                Expr::OptChain(opt) => matches!(&*opt.base, OptChainBase::Call(_)),
                _ => false,
            };
            if opaque {
                self.found = true;
                return;
            }
        }
        expr.visit_children_with(self);
    }
    fn visit_function(&mut self, node: &Function) {
        lazy_visit_function(self, node);
    }
    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        lazy_visit_arrow_expr(self, node);
    }
    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }
    fn visit_getter_prop(&mut self, node: &GetterProp) {
        lazy_visit_getter_prop(self, node);
    }
    fn visit_setter_prop(&mut self, node: &SetterProp) {
        lazy_visit_setter_prop(self, node);
    }
    fn visit_class(&mut self, node: &Class) {
        lazy_visit_class(self, node);
    }
    fn visit_class_member(&mut self, member: &ClassMember) {
        lazy_visit_class_member(self, member);
    }
}

fn has_opaque_at_init_call(
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
) -> bool {
    let mut finder = OpaqueAtInitCallFinder {
        shadowed,
        hints,
        graph,
        found: false,
        lazy_depth: 0,
        past_await: false,
    };
    item.visit_with(&mut finder);
    finder.found
}

fn item_purity(
    item: &ModuleItem,
    kind: StatementKind,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
    has_local_effect: bool,
) -> Purity {
    match kind {
        StatementKind::Import | StatementKind::FnDecl => Purity::Pure,
        // `export { ... }` / `export * from ...` / `export default
        // function` run no code at init — but `export default <expr>`
        // evaluates the expression and `export default class` runs
        // observable static parts (extends, static blocks, computed
        // keys), so those route through the regular classifiers.
        StatementKind::Export => match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                classify_expr_purity(
                    &default_expr.expr,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.declared_pure,
                    graph,
                )
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_)) => match class_of_item(item) {
                Some(c)
                    if class_has_static_observable(
                        c,
                        shadowed,
                        &BTreeSet::new(),
                        &hints.declared_pure,
                        graph,
                    ) =>
                {
                    Purity::NotPure {
                        reasons: vec![PurityReason {
                            rule: PurityRule::ClassStaticObservable,
                            span: c.span,
                            source_location: None,
                            detail: None,
                        }],
                    }
                }
                _ => Purity::Pure,
            },
            _ => Purity::Pure,
        },
        StatementKind::VarDecl if has_local_effect => Purity::Pure,
        // Top-level (non-function-body) scope: a chunk-top read of a
        // PlainData const is legitimately pure, so no PlainData name is
        // lexically shadowed here.
        StatementKind::VarDecl => var_decl_of_item(item)
            .map(|var| {
                classify_var_decl_purity(
                    var,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.declared_pure,
                    graph,
                )
            })
            .unwrap_or(Purity::Pure),
        StatementKind::ClassDecl => match class_of_item(item) {
            Some(c)
                if class_has_static_observable(
                    c,
                    shadowed,
                    &BTreeSet::new(),
                    &hints.declared_pure,
                    graph,
                ) =>
            {
                Purity::NotPure {
                    reasons: vec![PurityReason {
                        rule: PurityRule::ClassStaticObservable,
                        span: c.span,
                        source_location: None,
                        detail: None,
                    }],
                }
            }
            _ => Purity::Pure,
        },
        StatementKind::SideEffect if has_local_effect => Purity::Pure,
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => classify_expr_purity(
                &expr.expr,
                shadowed,
                &BTreeSet::new(),
                &hints.declared_pure,
                graph,
            ),
            // Bare blocks, control flow, loops, etc. — soundness-first.
            _ => Purity::NotPure {
                reasons: vec![PurityReason {
                    rule: PurityRule::BareControlFlow,
                    span: item.span(),
                    source_location: None,
                    detail: None,
                }],
            },
        },
    }
}

fn collect_local_effects(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
    local_effect_context: &local_effects::LocalEffectContext,
    local_effect_policy: LocalEffectPolicy,
) -> BTreeSet<Id> {
    let mut out = BTreeSet::new();
    if let Some(target) = recognized_local_effect_target(item, known_effects) {
        out.insert(target);
    }
    if local_effect_policy == LocalEffectPolicy::VendorPrune {
        out.extend(local_effect_context.local_effect_targets(item));
    }
    out
}

fn recognized_local_effect_target(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
) -> Option<Id> {
    let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item else {
        return None;
    };
    let Expr::Call(call) = strip_parens(&expr_stmt.expr) else {
        return None;
    };
    let callee = call_callee_ident(call)?;
    if known_effects.get(callee.sym.as_ref()) != Some(&KnownEffect::TypescriptDecorateHelper) {
        return None;
    }
    typescript_decorate_helper_target(call)
}

fn call_callee_ident(call: &CallExpr) -> Option<&Ident> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    match strip_parens(callee) {
        Expr::Ident(ident) => Some(ident),
        _ => None,
    }
}

fn typescript_decorate_helper_target(call: &CallExpr) -> Option<Id> {
    if call.args.iter().any(|arg| arg.spread.is_some()) {
        return None;
    }
    match call.args.len() {
        2 => {
            if !decorator_array_is_static_reference_list(&call.args[0].expr) {
                return None;
            }
            class_or_prototype_target_binding(&call.args[1].expr)
        }
        4 => {
            if !decorator_array_is_static_reference_list(&call.args[0].expr)
                || !decorate_property_key_is_static(&call.args[2].expr)
                || !decorate_flags_are_static(&call.args[3].expr)
            {
                return None;
            }
            class_or_prototype_target_binding(&call.args[1].expr)
        }
        _ => None,
    }
}
fn decorator_array_is_static_reference_list(expr: &Expr) -> bool {
    let Expr::Array(array) = strip_parens(expr) else {
        return false;
    };
    array.elems.iter().all(|elem| {
        let Some(elem) = elem else {
            return false;
        };
        elem.spread.is_none() && static_reference_expr(&elem.expr)
    })
}

fn static_reference_expr(expr: &Expr) -> bool {
    match strip_parens(expr) {
        Expr::Ident(_) => true,
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(_))
                && static_reference_expr(member.obj.as_ref())
        }
        _ => false,
    }
}

fn class_or_prototype_target_binding(expr: &Expr) -> Option<Id> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Member(member) => {
            let MemberProp::Ident(prop) = &member.prop else {
                return None;
            };
            if prop.sym.as_ref() != "prototype" {
                return None;
            }
            match strip_parens(member.obj.as_ref()) {
                Expr::Ident(ident) => Some(ident.to_id()),
                _ => None,
            }
        }
        _ => None,
    }
}

fn decorate_property_key_is_static(expr: &Expr) -> bool {
    matches!(
        strip_parens(expr),
        Expr::Lit(Lit::Str(_)) | Expr::Lit(Lit::Num(_))
    )
}

fn decorate_flags_are_static(expr: &Expr) -> bool {
    matches!(strip_parens(expr), Expr::Lit(Lit::Num(_)))
}

pub(crate) fn var_decl_of_item(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

fn class_of_item(item: &ModuleItem) -> Option<&Class> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(cls))) => Some(&cls.class),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        _ => None,
    }
}

fn classify_item(item: &ModuleItem) -> StatementKind {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => StatementKind::Import,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(_) => StatementKind::VarDecl,
            Decl::Fn(_) => StatementKind::FnDecl,
            Decl::Class(_) => StatementKind::ClassDecl,
            _ => StatementKind::Export,
        },
        ModuleItem::ModuleDecl(_) => StatementKind::Export,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))) => StatementKind::VarDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => StatementKind::FnDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(_))) => StatementKind::ClassDecl,
        _ => StatementKind::SideEffect,
    }
}

pub(crate) fn collect_declared_names(item: &ModuleItem) -> BTreeSet<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => declaration_names(&decl.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Fn(fn_expr) => fn_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.to_id()]))
                .unwrap_or_default(),
            DefaultDecl::Class(class_expr) => class_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.to_id()]))
                .unwrap_or_default(),
            _ => BTreeSet::new(),
        },
        // `var` declarations hoist to module scope out of blocks
        // (`try { var impl = ...; } catch { var impl = ...; }`,
        // `if`, loop bodies). The enclosing top-level statement is
        // the binding's owner.
        ModuleItem::Stmt(stmt) => hoisted_var_ids(stmt).into_iter().collect(),
        _ => BTreeSet::new(),
    }
}

fn declaration_names(decl: &Decl) -> BTreeSet<Id> {
    declaration_ids(decl).into_iter().collect()
}

/// `true` when the statement's declared binding is directly a
/// function value. See [`StatementFacts::declares_direct_function`].
fn declares_direct_function(item: &ModuleItem) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => true,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl))
            if matches!(decl.decl, Decl::Fn(_)) =>
        {
            true
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl))
            if matches!(decl.decl, DefaultDecl::Fn(_)) =>
        {
            true
        }
        _ => var_decl_of_item(item).is_some_and(|var| {
            var.decls.len() == 1
                && matches!(&var.decls[0].name, Pat::Ident(_))
                && var.decls[0]
                    .init
                    .as_deref()
                    .map(strip_parens)
                    .is_some_and(|init| matches!(init, Expr::Fn(_) | Expr::Arrow(_)))
        }),
    }
}

/// Shared trait for visitors that track lazy nesting depth and the
/// per-body "past first await" boundary.
///
/// Depth semantics:
/// - `0` — eager (outside any function body).
/// - `1` — first-order lazy (inside the immediate body of a function).
/// - `≥2` — nested lazy (inside a function nested in another function body).
///
/// `past_await` is a per-body flag: while visiting an async function /
/// arrow / method body, it flips `true` once an `AwaitExpr` has been
/// seen, and resets back to `false` when control exits that body.
/// Code past the first await runs in a microtask after the at-init
/// caller has finished, so it doesn't behave as "synchronously fires
/// when the function is invoked" — at-init call promotion treats it
/// like a nested closure.
///
/// At-init call promotion only inherits reads/rebinds/calls from the
/// **first-order, pre-await** part of a callee's body
/// (`lazy_depth == 1 && !past_await`), because a synchronous invocation
/// of the callee runs only that portion of the body. Statements
/// lexically inside nested function/arrow definitions or past an
/// `await` in an async body are not executed until something else
/// (the nested closure being called, or the microtask scheduler)
/// fires them. The general `lazy_*` sets stay coarse (any depth ≥1,
/// regardless of `past_await`) because, from the chunk's top-level
/// POV, any rebind inside any function body remains "lazy".
trait LazyBoundary: Visit {
    fn lazy_depth_mut(&mut self) -> &mut u32;
    fn past_await_mut(&mut self) -> &mut bool;

    fn descend_lazy<F: FnOnce(&mut Self)>(&mut self, f: F) {
        // Each body has its own `past_await` scope: a nested function
        // starts pre-await regardless of the enclosing body's state.
        let saved_past_await = std::mem::replace(self.past_await_mut(), false);
        *self.lazy_depth_mut() += 1;
        f(self);
        *self.lazy_depth_mut() -= 1;
        *self.past_await_mut() = saved_past_await;
    }
}

/// Single-pass collector producing every per-statement fact set the
/// analyzer needs. Walks the statement's AST exactly once and buckets
/// reads, rebind writes, calls, and effect-summary cells by the
/// syntactic context the cursor is in (`lazy_depth`, `past_await`).
/// Replaces the earlier five-pass design — one per fact set — which
/// each re-implemented the same `LazyBoundary` boilerplate and walked
/// the same AST.
///
/// Bucketing rules (see [`PositionBucketed::record`]):
///
/// - `lazy_depth == 0` (eager, top of the statement): reads, rebind
///   writes, and direct `f(...)` callees land in the respective
///   `eager` buckets; static-key `globalThis.<prop>` accesses
///   contribute to `global_writes`/`global_reads`; bail-out shapes
///   flip `dataflow_summarizable` to `false`.
/// - `lazy_depth >= 1` (inside a function/arrow/method/getter/setter
///   body, constructor body, or instance class-field initializer):
///   reads/writes/calls land in the `lazy` buckets. The subset whose
///   sites sit at `lazy_depth == 1 && !past_await` also lands in
///   `first_order_lazy` — used by at-init call promotion, which
///   only inherits effects from a callee's immediate pre-await body.
/// - Bail-out shapes nested inside lazy scopes are deliberately not
///   recorded: at-init call promotion handles transitive effects via
///   the call graph, not via per-statement syntactic checks.
#[derive(Default)]
struct StatementFactsCollector {
    reads: PositionBucketed<BTreeSet<Id>>,
    rebinds: PositionBucketed<BTreeSet<Id>>,
    calls: PositionBucketed<BTreeSet<Id>>,
    at_init_unresolved_sources: BTreeSet<Id>,
    at_init_unresolved_inline_fn: bool,
    first_order_unresolved_sources: BTreeSet<Id>,
    first_order_unresolved_inline_fn: bool,
    global_writes: BTreeSet<String>,
    global_reads: BTreeSet<String>,
    cell_writes_summarizable: bool,
    dataflow_summarizable: bool,
    /// Unshadowed global-object alias names for this chunk
    /// (`globalThis`, `window`, ...). See
    /// [`unshadowed_global_object_aliases`].
    global_object_names: BTreeSet<&'static str>,
    lazy_depth: u32,
    past_await: bool,
}

impl StatementFactsCollector {
    fn new(global_object_names: BTreeSet<&'static str>) -> Self {
        Self {
            cell_writes_summarizable: true,
            dataflow_summarizable: true,
            global_object_names,
            ..Self::default()
        }
    }

    fn record_read(&mut self, id: &Id) {
        self.reads.record(id, self.lazy_depth, self.past_await);
    }

    fn record_write(&mut self, id: &Id) {
        self.rebinds.record(id, self.lazy_depth, self.past_await);
    }

    fn record_call(&mut self, id: &Id) {
        self.calls.record(id, self.lazy_depth, self.past_await);
    }

    /// A call whose callee promotion can never resolve syntactically
    /// (member call, IIFE, optional-chain call, tagged template).
    /// Record the bindings the call mentions (callee root, argument
    /// idents, computed keys) — the only channels through which a
    /// chunk function value can reach the call — plus whether the
    /// call carries an inline function expression. At-init the
    /// statement takes the read-closure fallback over those sources;
    /// in a first-order body they propagate to at-init callers
    /// through the promotion call graph.
    fn record_unresolved_call<N: VisitWith<UnresolvedCallSourceCollector>>(&mut self, node: &N) {
        if self.lazy_depth > 1 || (self.lazy_depth == 1 && self.past_await) {
            return;
        }
        let mut sources = UnresolvedCallSourceCollector::default();
        node.visit_with(&mut sources);
        if self.lazy_depth == 0 {
            self.at_init_unresolved_sources.extend(sources.idents);
            self.at_init_unresolved_inline_fn |= sources.inline_fn;
        } else {
            self.first_order_unresolved_sources.extend(sources.idents);
            self.first_order_unresolved_inline_fn |= sources.inline_fn;
        }
    }

    /// Bail only the S-chain's "which cells does this touch"
    /// question (member writes, alias shapes). The vendor strip's
    /// write-cell view stays summarizable.
    fn bail_summarizable(&mut self) {
        self.dataflow_summarizable = false;
    }

    /// Bail every consumer: the statement may WRITE arbitrary cells
    /// (`with`, eval, `Function(...)`, dynamic global keys,
    /// `defineProperty`/`Proxy` on the global object).
    fn bail_cell_writes(&mut self) {
        self.cell_writes_summarizable = false;
        self.dataflow_summarizable = false;
    }

    fn is_global_object_expr(&self, expr: &Expr) -> bool {
        matches!(strip_parens(expr), Expr::Ident(i) if self.global_object_names.contains(i.sym.as_ref()))
    }

    fn record_global_prop(&mut self, member: &MemberExpr, is_write: bool) {
        if !self.is_global_object_expr(&member.obj) {
            return;
        }
        let key = match &member.prop {
            MemberProp::Ident(ident) => Some(ident.sym.to_string()),
            MemberProp::Computed(ComputedPropName { expr, .. }) => match strip_parens(expr) {
                Expr::Lit(Lit::Str(s)) => Some(s.value.to_string_lossy().into_owned()),
                _ => {
                    self.bail_cell_writes();
                    return;
                }
            },
            MemberProp::PrivateName(_) => None,
        };
        if let Some(key) = key {
            if is_write {
                self.global_writes.insert(key);
            } else {
                self.global_reads.insert(key);
            }
        }
    }
}

/// Collects the ident reads and inline-function presence inside an
/// unresolved call expression. Static member prop names are
/// `IdentName`s (not `Ident`s) and are not collected; function /
/// arrow / class / accessor interiors are skipped — their contents
/// are already covered by the owner's lazy sets, which the
/// `inline_fn` flag pulls into the fallback closure.
#[derive(Default)]
struct UnresolvedCallSourceCollector {
    idents: BTreeSet<Id>,
    inline_fn: bool,
}

impl Visit for UnresolvedCallSourceCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.idents.insert(node.to_id());
    }
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_function(&mut self, _node: &Function) {
        self.inline_fn = true;
    }
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {
        self.inline_fn = true;
    }
    fn visit_class(&mut self, _node: &Class) {
        self.inline_fn = true;
    }
    fn visit_getter_prop(&mut self, _node: &GetterProp) {
        self.inline_fn = true;
    }
    fn visit_setter_prop(&mut self, _node: &SetterProp) {
        self.inline_fn = true;
    }
}

impl LazyBoundary for StatementFactsCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
}

impl TargetAccessRecorder for StatementFactsCollector {
    fn record_binding_write(&mut self, id: &Id) {
        self.record_write(id);
    }

    fn record_member_write(&mut self, id: &Id) {
        // Property writes through a tracked binding (`obj.x = 1`,
        // `obj.x++`, `(a?.b).c = 1`) mutate heap state the cell
        // summary can't attribute: aliasing makes the write
        // invisible to readers going through a different binding.
        // Global-object roots are handled precisely (static key) or
        // bailed (dynamic key / deep chain) at the assign/update
        // visitors.
        if self.lazy_depth == 0 && !self.global_object_names.contains(id.0.as_ref()) {
            self.bail_summarizable();
        }
    }
}

impl Visit for StatementFactsCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.record_read(&node.to_id());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    // Export specifiers (`export { X }`, `export * from ...`) don't
    // fire at-init reads: ESM resolves the binding when a consumer's
    // `import` references it. But the materializer's routing layer
    // treats a re-exported binding's destination as a lazy
    // dependency: if the binding moves to a different module, the
    // emitter inserts `import { X } from <new home>` at the top of
    // this chunk's entry to keep the export surface intact. The
    // realizability primitive's I-graph cycle check needs to see
    // that materializer-induced lazy import edge, so record each
    // `orig` Ident as a LAZY read (placed directly into `reads.lazy`,
    // bypassing `record_read`'s lazy-depth bucketing — these reads
    // are semantically deferred regardless of where the export
    // specifier sits syntactically). Not added to
    // `reads.first_order_lazy`: re-exports aren't reachable through
    // at-init call promotion, so excluding them from that subset
    // prevents the promotion pass from inventing spurious eager
    // edges for re-exported bindings.
    //
    // `export { X } from "./foo"` (with `src.is_some()`) is a
    // different shape: the binding lives in `./foo`, not in this
    // chunk; the `import`-side dep is captured by the import-decl
    // path, not via specifier reads here.
    fn visit_named_export(&mut self, node: &NamedExport) {
        if node.src.is_some() {
            return;
        }
        for specifier in &node.specifiers {
            let ExportSpecifier::Named(named) = specifier else {
                continue;
            };
            if let ModuleExportName::Ident(ident) = &named.orig {
                self.reads.lazy.insert(ident.to_id());
            }
        }
    }
    fn visit_export_all(&mut self, _node: &ExportAll) {}

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        // The awaited operand runs to completion (synchronously)
        // before the engine suspends; visit its children first and
        // flip `past_await` only after, so the pre-await reads/writes
        // still count as first-order.
        node.visit_children_with(self);
        self.past_await = true;
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        record_assign_target(&node.left, self);
        if self.lazy_depth == 0 {
            match &node.left {
                AssignTarget::Simple(_) => {
                    if let Some(member) = simple_assign_member_target(&node.left) {
                        if self.is_global_object_expr(&member.obj) {
                            // `globalThis.tag = ...`: a precisely
                            // tracked cell (dynamic keys bail inside
                            // `record_global_prop`).
                            self.record_global_prop(member, /*is_write=*/ true);
                        } else {
                            // Member write through a binding or a
                            // deeper global chain (`globalThis.a.b`):
                            // not attributable to a static cell.
                            self.bail_summarizable();
                        }
                    }
                }
                AssignTarget::Pat(pat) => {
                    // Destructuring targets may smuggle member
                    // writes: `[obj.x] = arr`.
                    if assign_target_pat_has_member_target(pat) {
                        self.bail_summarizable();
                    }
                }
            }
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        record_update_target(&node.arg, self);
        if self.lazy_depth == 0 {
            match strip_parens(&node.arg) {
                // `count++`: binding read+write, handled by
                // `record_update_target` + the child visit.
                Expr::Ident(_) => {}
                Expr::Member(member) if self.is_global_object_expr(&member.obj) => {
                    // `globalThis.count++` reads and writes the cell.
                    self.record_global_prop(member, /*is_write=*/ true);
                    self.record_global_prop(member, /*is_write=*/ false);
                }
                _ => self.bail_summarizable(),
            }
        }
        node.arg.visit_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    // The S-chain's dataflow-summarizability of calls/news is
    // decided in the policy phase (`has_opaque_at_init_call`): any
    // at-init call or `new` the purity classifier can't prove Pure
    // bails `dataflow_summarizable`. The structural checks below
    // additionally flip `cell_writes_summarizable` for the shapes
    // that defeat WRITE-cell reasoning outright: direct and indirect
    // `eval`, `Function(...)`, `Object.defineProperty(globalThis,
    // ...)` / `Reflect.defineProperty(globalThis, ...)`, and
    // `new Proxy(globalThis, ...)`.
    fn visit_call_expr(&mut self, node: &CallExpr) {
        match &node.callee {
            Callee::Expr(callee) => match strip_parens(callee) {
                Expr::Ident(ident) => self.record_call(&ident.to_id()),
                _ => self.record_unresolved_call(node),
            },
            // `import(...)` evaluates another chunk asynchronously —
            // it never synchronously runs this chunk's functions.
            // `super(...)` can't appear at chunk top level.
            Callee::Import(_) | Callee::Super(_) => {}
        }
        if self.lazy_depth == 0 {
            if let Callee::Expr(expr) = &node.callee
                && let Expr::Ident(ident) = callee_base_expr(expr)
                && matches!(ident.sym.as_ref(), "eval" | "Function")
            {
                self.bail_cell_writes();
            }
            if let Callee::Expr(expr) = &node.callee
                && let Expr::Member(member) = strip_parens(expr)
                && let MemberProp::Ident(prop) = &member.prop
                && prop.sym.as_ref() == "defineProperty"
                && matches!(
                    strip_parens(&member.obj),
                    Expr::Ident(i) if matches!(i.sym.as_ref(), "Object" | "Reflect")
                )
                && node
                    .args
                    .first()
                    .is_some_and(|a| self.is_global_object_expr(&a.expr))
            {
                self.bail_cell_writes();
            }
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if self.lazy_depth == 0
            && let Expr::Ident(ident) = strip_parens(&node.callee)
        {
            match ident.sym.as_ref() {
                "Function" => self.bail_cell_writes(),
                "Proxy" => {
                    let proxies_global = node
                        .args
                        .as_ref()
                        .and_then(|args| args.first())
                        .is_some_and(|a| self.is_global_object_expr(&a.expr));
                    if proxies_global {
                        self.bail_cell_writes();
                    }
                }
                _ => {}
            }
        }
        node.visit_children_with(self);
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        // `f?.()` / `obj?.m()`: the callee is never a resolvable
        // bare-Ident shape for promotion.
        self.record_unresolved_call(node);
        node.visit_children_with(self);
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        // `` tag`...` `` invokes the tag function.
        self.record_unresolved_call(node);
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if self.lazy_depth == 0 {
            self.record_global_prop(node, /*is_write=*/ false);
        }
        node.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        if self.lazy_depth == 0 {
            self.bail_cell_writes();
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        lazy_visit_function(self, node);
    }
    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        lazy_visit_arrow_expr(self, node);
    }
    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }
    fn visit_getter_prop(&mut self, node: &GetterProp) {
        lazy_visit_getter_prop(self, node);
    }
    fn visit_setter_prop(&mut self, node: &SetterProp) {
        lazy_visit_setter_prop(self, node);
    }
    fn visit_class(&mut self, node: &Class) {
        lazy_visit_class(self, node);
    }
    fn visit_class_member(&mut self, member: &ClassMember) {
        lazy_visit_class_member(self, member);
    }
}

/// The member expression a simple assignment target writes through,
/// unwrapping parens (`(globalThis.x) = 1`). `None` for ident,
/// opt-chain, and pattern targets.
fn simple_assign_member_target(target: &AssignTarget) -> Option<&MemberExpr> {
    let AssignTarget::Simple(simple) = target else {
        return None;
    };
    match simple {
        SimpleAssignTarget::Member(member) => Some(member),
        SimpleAssignTarget::Paren(paren) => match strip_parens(&paren.expr) {
            Expr::Member(member) => Some(member),
            _ => None,
        },
        _ => None,
    }
}

/// `true` if a destructuring assignment target contains a member
/// expression (`[obj.x] = arr`, `({ k: obj.x } = o)`) — a property
/// write the binding-pattern walker doesn't record.
fn assign_target_pat_has_member_target(pat: &AssignTargetPat) -> bool {
    fn pat_has_expr(pat: &Pat) -> bool {
        match pat {
            Pat::Ident(_) | Pat::Invalid(_) => false,
            Pat::Expr(_) => true,
            Pat::Array(array) => array.elems.iter().flatten().any(pat_has_expr),
            Pat::Object(object) => object.props.iter().any(|prop| match prop {
                ObjectPatProp::KeyValue(kv) => pat_has_expr(&kv.value),
                ObjectPatProp::Assign(_) => false,
                ObjectPatProp::Rest(rest) => pat_has_expr(&rest.arg),
            }),
            Pat::Assign(assign) => pat_has_expr(&assign.left),
            Pat::Rest(rest) => pat_has_expr(&rest.arg),
        }
    }
    match pat {
        AssignTargetPat::Array(array) => array.elems.iter().flatten().any(pat_has_expr),
        AssignTargetPat::Object(object) => object.props.iter().any(|prop| match prop {
            ObjectPatProp::KeyValue(kv) => pat_has_expr(&kv.value),
            ObjectPatProp::Assign(_) => false,
            ObjectPatProp::Rest(rest) => pat_has_expr(&rest.arg),
        }),
        AssignTargetPat::Invalid(_) => false,
    }
}

fn lazy_visit_function<V: LazyBoundary>(v: &mut V, node: &Function) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

fn lazy_visit_arrow_expr<V: LazyBoundary>(v: &mut V, node: &ArrowExpr) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

fn lazy_visit_method_prop<V: LazyBoundary>(v: &mut V, node: &MethodProp) {
    node.key.visit_with(v);
    // `node.function.visit_with` dispatches to `visit_function`, which
    // already calls `descend_lazy` via `lazy_visit_function`. No outer
    // descend here — the method body must land at lazy_depth 1, the
    // same as a bare `function f() { ... }`.
    node.function.visit_with(v);
}

fn lazy_visit_getter_prop<V: LazyBoundary>(v: &mut V, node: &GetterProp) {
    node.key.visit_with(v);
    v.descend_lazy(|s| {
        if let Some(body) = &node.body {
            body.visit_with(s);
        }
    });
}

fn lazy_visit_setter_prop<V: LazyBoundary>(v: &mut V, node: &SetterProp) {
    node.key.visit_with(v);
    node.param.visit_with(v);
    v.descend_lazy(|s| {
        if let Some(body) = &node.body {
            body.visit_with(s);
        }
    });
}

fn lazy_visit_class<V: LazyBoundary>(v: &mut V, node: &Class) {
    visit_class_decl(v, node, |v, m| v.visit_class_member(m));
}

fn lazy_visit_class_member<V: LazyBoundary>(v: &mut V, member: &ClassMember) {
    visit_eager_member_parts(v, member);
    match member {
        // `method.function.visit_with` dispatches to `visit_function`,
        // which already calls `descend_lazy` via `lazy_visit_function`.
        // No outer descend here — a method body must land at
        // lazy_depth 1, the same as a bare `function f() { ... }`,
        // so rebinds in the immediate body show up in
        // `rebinds.first_order_lazy` and the owner-graph emits the
        // constraining `LazyRebind` edge that catches cross-module
        // writes to imported bindings (ESM rejects at runtime).
        ClassMember::Method(method) => {
            method.function.visit_with(v);
        }
        ClassMember::PrivateMethod(method) => {
            method.function.visit_with(v);
        }
        ClassMember::Constructor(ctor) => {
            v.descend_lazy(|s| ctor.visit_children_with(s));
        }
        ClassMember::ClassProp(prop) if !prop.is_static => {
            if let Some(value) = &prop.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        ClassMember::PrivateProp(prop) if !prop.is_static => {
            if let Some(value) = &prop.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
            if let Some(value) = &accessor.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        _ => {}
    }
}

fn visit_computed_prop_name<V: Visit>(v: &mut V, name: &PropName) {
    if let PropName::Computed(computed) = name {
        computed.expr.visit_with(v);
    }
}

fn visit_class_decl<V: Visit>(
    v: &mut V,
    node: &Class,
    mut visit_member: impl FnMut(&mut V, &ClassMember),
) {
    for decorator in &node.decorators {
        decorator.visit_with(v);
    }
    if let Some(super_class) = &node.super_class {
        super_class.visit_with(v);
    }
    for member in &node.body {
        visit_member(v, member);
    }
}

fn visit_eager_member_parts<V: Visit>(v: &mut V, member: &ClassMember) {
    match member {
        ClassMember::Method(method) => {
            visit_computed_prop_name(v, &method.key);
        }
        ClassMember::PrivateMethod(_) | ClassMember::Constructor(_) => {}
        ClassMember::ClassProp(prop) => {
            visit_computed_prop_name(v, &prop.key);
            if prop.is_static {
                if let Some(value) = &prop.value {
                    value.visit_with(v);
                }
            }
        }
        ClassMember::PrivateProp(prop) => {
            if prop.is_static {
                if let Some(value) = &prop.value {
                    value.visit_with(v);
                }
            }
        }
        ClassMember::StaticBlock(block) => {
            block.visit_with(v);
        }
        ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
        ClassMember::AutoAccessor(accessor) => {
            if let Key::Public(name) = &accessor.key {
                visit_computed_prop_name(v, name);
            }
            if accessor.is_static {
                if let Some(value) = &accessor.value {
                    value.visit_with(v);
                }
            }
        }
    }
}
