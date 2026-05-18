use std::collections::{BTreeMap, BTreeSet};

use binding_targets::{
    TargetAccessRecorder, binding_names, record_assign_target, record_pat_write,
    record_update_target,
};
use serde::{Deserialize, Serialize};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::purity::{
    ChunkCodeGraph, Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPurityHint,
    WHITELIST_RECEIVERS, class_has_static_observable, classify_expr_purity,
    classify_var_decl_purity, detect_redundant_pure_member_hints, detect_redundant_purity_hints,
};
use crate::{BindingName, SourceLocation, StatementOrdinal};

#[derive(Debug, Clone)]
pub struct StatementFacts {
    pub ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<BindingName>,
    pub eager_reads: BTreeSet<BindingName>,
    pub eager_rebinds: BTreeSet<BindingName>,
    /// Reads happening only inside lazy syntactic positions (function
    /// bodies, instance class-field initializers, getters/setters,
    /// constructor bodies). May overlap with `eager_reads` if the
    /// same name appears in both eager and lazy positions of the
    /// statement.
    pub lazy_reads: BTreeSet<BindingName>,
    /// Rebinding writes happening only inside lazy syntactic
    /// positions. Member writes (`obj.x = ...`) are intentionally
    /// excluded: mutating an imported object is legal, but rebinding
    /// the imported binding cell is not.
    pub lazy_rebinds: BTreeSet<BindingName>,
    /// Subset of `lazy_reads` whose read sites sit in a function's
    /// **first-order** body (depth 1 from this statement). Used by
    /// at-init call promotion: a synchronous call to the function
    /// only runs its immediate body, so reads inside nested
    /// function/arrow definitions don't promote to the caller.
    pub first_order_lazy_reads: BTreeSet<BindingName>,
    /// Subset of `lazy_rebinds` whose write sites sit in a function's
    /// first-order body. See `first_order_lazy_reads`.
    pub first_order_lazy_rebinds: BTreeSet<BindingName>,
    /// Target-local mutations produced by recognized trusted helper
    /// calls. Each binding is the class/prototype owner that must
    /// co-locate with the mutating statement.
    pub local_effects: BTreeSet<BindingName>,
    /// Bare-identifier callees of `CallExpr` nodes seen at-init —
    /// i.e. outside any function/arrow/method body. Used by the
    /// owner-graph build to drive at-init call promotion: a call from
    /// statement S to chunk-declared function `f` is treated as
    /// transitively reading everything `f`'s body lazily reads. See
    /// DESIGN.md "At-init call promotion". Indirect calls
    /// (`const g = f; g()`), method calls (`obj.method()`), and
    /// computed callees are skipped — the callee must be a direct
    /// `Ident`.
    pub at_init_calls: BTreeSet<BindingName>,
    /// Same as `at_init_calls` but for calls inside lazy positions.
    /// Used by the owner-graph build to reconstruct the chunk call
    /// graph so that promotion can transitively follow call chains
    /// (e.g. `function f() { g(); } f();` at top level promotes
    /// through `g`'s body too).
    pub body_calls: BTreeSet<BindingName>,
    /// Subset of `body_calls` whose call sites sit in a function's
    /// **first-order** body. The promotion call graph uses this so
    /// that calls lexically nested inside a closure of the body
    /// don't appear as direct callees of the outer function — they
    /// don't fire when the outer function is invoked synchronously.
    pub first_order_body_calls: BTreeSet<BindingName>,
    pub purity: Purity,
    pub kind: StatementKind,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum KnownEffect {
    TypescriptDecorateHelper,
}

#[derive(Debug, Clone, Default)]
pub struct AnalysisHints {
    pub declared_pure: BTreeSet<String>,
    pub declared_pure_new: BTreeSet<String>,
    /// Author-declared pure member properties — keyed by binding name,
    /// value is the set of property names whose `<binding>.<prop>(args)`
    /// calls the spec author asserts are pure. The classifier consults
    /// this to admit `<recv>.<prop>(args)` as pure when `recv` is the
    /// keyed binding and `<prop>` is in the value set.
    /// See AGENTS.md "Declared purity".
    pub declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    pub known_effects: BTreeMap<String, KnownEffect>,
}

impl AnalysisHints {
    pub fn from_declared_pure(declared_pure: &BTreeSet<String>) -> Self {
        Self {
            declared_pure: declared_pure.clone(),
            declared_pure_new: BTreeSet::new(),
            declared_pure_members: BTreeMap::new(),
            known_effects: BTreeMap::new(),
        }
    }
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
/// (`split_var_decl` in `logical_modules.rs`); this pre-split
/// just teaches the analyzer the same view.
/// Locate the first top-level `await` expression in `module`'s
/// body, if any. Returns the source-order ordinal of the offending
/// statement (in the post-comma-list-split view that
/// `analyze_chunk` uses, so reports align with statement indices
/// in `<chunk_id>/schedule.json`).
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

pub fn analyze_chunk<F>(
    module: &Module,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &hints.declared_pure,
        &hints.declared_pure_new,
        &hints.declared_pure_members,
    );
    let redundant_purity_hints =
        detect_redundant_purity_hints(&body, &shadowed, &hints.declared_pure);
    let redundant_pure_member_hints =
        detect_redundant_pure_member_hints(&hints.declared_pure_members);
    let mut top_level_await = None;
    let facts = body
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
            let mut fact = analyze_item(StatementOrdinal(ordinal), item, &shadowed, hints, &graph);
            fact.source_location = source_path.and_then(|source_path| {
                line_range_for_span(item.span()).map(|(start_line, end_line)| SourceLocation {
                    source_path: source_path.to_string(),
                    start_line,
                    end_line,
                })
            });
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

pub(crate) enum TopLevelItemView<'a> {
    Borrowed(&'a ModuleItem),
    Owned(ModuleItem),
}

impl TopLevelItemView<'_> {
    pub(crate) fn as_module_item(&self) -> &ModuleItem {
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
pub(crate) fn top_level_item_views(body: &[ModuleItem]) -> Vec<TopLevelItemView<'_>> {
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

/// Walk `body` and collect the subset of `WHITELIST_RECEIVERS`
/// that are declared at the chunk's top-level scope (`var/let/const`,
/// `function`, `class`, exported decls) or bound by an import
/// specifier (default / namespace / named). The classifier consults
/// this set to skip the whitelist for any receiver the chunk
/// shadows — `const Math = …` and
/// `import { Math } from "./userland"` both make `Math.PI` an
/// Unknown read, not the global constant. See DESIGN.md A8.
pub(crate) fn compute_shadowed_globals(body: &[TopLevelItemView<'_>]) -> BTreeSet<&'static str> {
    let mut shadowed = BTreeSet::new();
    let try_shadow = |name: &str, into: &mut BTreeSet<&'static str>| {
        if let Some(global) = WHITELIST_RECEIVERS.iter().copied().find(|r| *r == name) {
            into.insert(global);
        }
    };
    for item in body {
        let item = item.as_module_item();
        for name in collect_declared_names(item) {
            try_shadow(name.as_str(), &mut shadowed);
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

fn analyze_item(
    ordinal: StatementOrdinal,
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
) -> StatementFacts {
    let kind = classify_item(item);
    let declared = collect_declared_names(item);
    let mut at_init = AtInitReadCollector::default();
    item.visit_with(&mut at_init);
    let mut lazy = LazyReadCollector::default();
    item.visit_with(&mut lazy);
    let mut writes = BindingWriteCollector::default();
    item.visit_with(&mut writes);
    let mut calls = CallCollector::default();
    item.visit_with(&mut calls);
    let local_effects = collect_local_effects(item, &hints.known_effects);
    let purity = item_purity(
        item,
        kind,
        shadowed,
        hints,
        graph,
        !local_effects.is_empty(),
    );
    StatementFacts {
        ordinal,
        source_location: None,
        declared,
        eager_reads: at_init.names,
        eager_rebinds: writes.at_init,
        lazy_reads: lazy.names,
        lazy_rebinds: writes.lazy,
        first_order_lazy_reads: lazy.first_order,
        first_order_lazy_rebinds: writes.first_order_lazy,
        local_effects,
        at_init_calls: calls.at_init,
        body_calls: calls.lazy,
        first_order_body_calls: calls.first_order_lazy,
        purity,
        kind,
    }
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
        StatementKind::Import | StatementKind::Export | StatementKind::FnDecl => Purity::Pure,
        StatementKind::VarDecl => var_decl_of_item(item)
            .map(|var| classify_var_decl_purity(var, shadowed, &hints.declared_pure, graph))
            .unwrap_or(Purity::Pure),
        StatementKind::ClassDecl => match class_of_item(item) {
            Some(c) if class_has_static_observable(c, shadowed, &hints.declared_pure, graph) => {
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
            ModuleItem::Stmt(Stmt::Expr(expr)) => {
                classify_expr_purity(&expr.expr, shadowed, &hints.declared_pure, graph)
            }
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
) -> BTreeSet<BindingName> {
    let mut out = BTreeSet::new();
    if let Some(target) = recognized_local_effect_target(item, known_effects) {
        out.insert(target);
    }
    out
}

fn recognized_local_effect_target(
    item: &ModuleItem,
    known_effects: &BTreeMap<String, KnownEffect>,
) -> Option<BindingName> {
    let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item else {
        return None;
    };
    let Expr::Call(call) = strip_parens(&expr_stmt.expr) else {
        return None;
    };
    let callee = call_callee_ident(call)?;
    if known_effects.get(callee) != Some(&KnownEffect::TypescriptDecorateHelper) {
        return None;
    }
    typescript_decorate_helper_target(call)
}

fn call_callee_ident(call: &CallExpr) -> Option<&str> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    match strip_parens(callee) {
        Expr::Ident(ident) => Some(ident.sym.as_ref()),
        _ => None,
    }
}

fn typescript_decorate_helper_target(call: &CallExpr) -> Option<BindingName> {
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

fn class_or_prototype_target_binding(expr: &Expr) -> Option<BindingName> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.sym.to_string()),
        Expr::Member(member) => {
            let MemberProp::Ident(prop) = &member.prop else {
                return None;
            };
            if prop.sym.as_ref() != "prototype" {
                return None;
            }
            match strip_parens(member.obj.as_ref()) {
                Expr::Ident(ident) => Some(ident.sym.to_string()),
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

fn strip_parens(expr: &Expr) -> &Expr {
    let mut cur = expr;
    while let Expr::Paren(paren) = cur {
        cur = &paren.expr;
    }
    cur
}

fn var_decl_of_item(item: &ModuleItem) -> Option<&VarDecl> {
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

fn collect_declared_names(item: &ModuleItem) -> BTreeSet<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => declaration_names(&decl.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Fn(fn_expr) => fn_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.sym.to_string()]))
                .unwrap_or_default(),
            DefaultDecl::Class(class_expr) => class_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.sym.to_string()]))
                .unwrap_or_default(),
            _ => BTreeSet::new(),
        },
        _ => BTreeSet::new(),
    }
}

fn declaration_names(decl: &Decl) -> BTreeSet<String> {
    match decl {
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|declarator| binding_names(&declarator.name))
            .collect(),
        Decl::Fn(fn_decl) => BTreeSet::from([fn_decl.ident.sym.to_string()]),
        Decl::Class(class_decl) => BTreeSet::from([class_decl.ident.sym.to_string()]),
        _ => BTreeSet::new(),
    }
}

/// Visitor that collects ident reads happening at-init only. Stops
/// at function bodies, method bodies, instance class-field
/// initializers, getter/setter bodies, and other lazy positions.
#[derive(Default)]
struct AtInitReadCollector {
    names: BTreeSet<String>,
}

impl Visit for AtInitReadCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.names.insert(node.sym.to_string());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    // Export specifiers don't fire reads at module-init: ESM treats
    // them as a static export entry, linked lazily when consumers
    // import. Counting them as at-init reads adds spurious `R`
    // edges (and, post-Phase-5 where R ⊆ I, spurious `I` edges).
    // `export var X = ...` / `export class X {}` etc. are still
    // visited via `ExportDecl`; only the bare-specifier forms are
    // suppressed here.
    fn visit_named_export(&mut self, _node: &NamedExport) {}
    fn visit_export_all(&mut self, _node: &ExportAll) {}

    // Function bodies are lazy — references inside don't read at-init.
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_fn_decl(&mut self, _node: &FnDecl) {}
    fn visit_fn_expr(&mut self, _node: &FnExpr) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}

    fn visit_class(&mut self, node: &Class) {
        visit_class_decl(self, node, |v, m| v.visit_class_member(m));
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        visit_eager_member_parts(self, member);
    }
}

/// Shared trait for visitors that track lazy nesting depth.
///
/// Depth semantics:
/// - `0` — eager (outside any function body).
/// - `1` — first-order lazy (inside the immediate body of a function).
/// - `≥2` — nested lazy (inside a function nested in another function body).
///
/// At-init call promotion only inherits reads/rebinds/calls from a
/// callee's first-order body, because a synchronous invocation of the
/// callee runs only its immediate body; statements lexically inside
/// nested function/arrow definitions are not executed until something
/// later invokes the nested closure. The general `lazy_*` sets stay
/// coarse (any depth ≥1) because, from the chunk's top-level POV, any
/// rebind inside any function body remains "lazy".
trait LazyBoundary: Visit {
    fn lazy_depth_mut(&mut self) -> &mut u32;

    fn descend_lazy<F: FnOnce(&mut Self)>(&mut self, f: F) {
        *self.lazy_depth_mut() += 1;
        f(self);
        *self.lazy_depth_mut() -= 1;
    }
}

/// Visitor that collects ident reads happening inside lazy syntactic
/// positions only — function bodies, method bodies, constructor
/// bodies, instance class-field initializers, getter/setter bodies.
/// Inverse boundary semantics from `AtInitReadCollector`.
///
/// `first_order` is the subset of `names` whose read sites sit
/// directly inside the function body the read collector is visiting
/// (depth 1); deeper closures don't contribute. Used by at-init call
/// promotion.
#[derive(Default)]
struct LazyReadCollector {
    names: BTreeSet<String>,
    first_order: BTreeSet<String>,
    lazy_depth: u32,
}

impl LazyBoundary for LazyReadCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
}

impl Visit for LazyReadCollector {
    fn visit_ident(&mut self, node: &Ident) {
        if self.lazy_depth == 0 {
            return;
        }
        self.names.insert(node.sym.to_string());
        if self.lazy_depth == 1 {
            self.first_order.insert(node.sym.to_string());
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

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

/// Visitor that collects rebinding writes to identifier bindings,
/// split by whether the write runs at module initialization or only
/// from a lazy syntactic position.
///
/// `first_order_lazy` is the subset of `lazy` whose write sites sit
/// directly inside the function body the collector is visiting
/// (depth 1); deeper closures don't contribute. Used by at-init call
/// promotion.
#[derive(Default)]
struct BindingWriteCollector {
    at_init: BTreeSet<String>,
    lazy: BTreeSet<String>,
    first_order_lazy: BTreeSet<String>,
    lazy_depth: u32,
}

impl LazyBoundary for BindingWriteCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
}

impl BindingWriteCollector {
    fn record_write(&mut self, name: &str) {
        if self.lazy_depth == 0 {
            self.at_init.insert(name.to_string());
            return;
        }
        self.lazy.insert(name.to_string());
        if self.lazy_depth == 1 {
            self.first_order_lazy.insert(name.to_string());
        }
    }
}

impl TargetAccessRecorder for BindingWriteCollector {
    fn record_binding_write(&mut self, name: &str) {
        self.record_write(name);
    }
}

impl Visit for BindingWriteCollector {
    fn visit_ident(&mut self, _node: &Ident) {}
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}
    fn visit_named_export(&mut self, _node: &NamedExport) {}
    fn visit_export_all(&mut self, _node: &ExportAll) {}

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        record_assign_target(&node.left, self);
        node.left.visit_with(self);
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        record_update_target(&node.arg, self);
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

/// Visitor that collects bare-identifier callees of `CallExpr` nodes,
/// split by whether the call appears at-init (top-level chunk
/// position, class static initializers) or only in a lazy position
/// (function/arrow/method/getter/setter bodies, instance class fields,
/// constructor bodies). Drives at-init call promotion in
/// `build_owner_graph`: see DESIGN.md "At-init call promotion".
///
/// Only direct `f(...)` calls where the callee is an `Ident` are
/// recorded. `obj.method()`, `(g)()`, `f()()`, computed callees, and
/// `(const g = f, g)()` are skipped — interprocedural promotion only
/// fires when the callee is statically a known chunk binding.
#[derive(Default)]
struct CallCollector {
    at_init: BTreeSet<String>,
    lazy: BTreeSet<String>,
    first_order_lazy: BTreeSet<String>,
    lazy_depth: u32,
}

impl LazyBoundary for CallCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
}

impl Visit for CallCollector {
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}
    fn visit_named_export(&mut self, _node: &NamedExport) {}
    fn visit_export_all(&mut self, _node: &ExportAll) {}

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Some(callee) = call_callee_ident(node) {
            let name = callee.to_string();
            if self.lazy_depth == 0 {
                self.at_init.insert(name);
            } else {
                self.lazy.insert(name.clone());
                if self.lazy_depth == 1 {
                    self.first_order_lazy.insert(name);
                }
            }
        }
        // Always recurse into args + callee so nested calls are seen.
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

fn lazy_visit_function<V: LazyBoundary>(v: &mut V, node: &Function) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

fn lazy_visit_arrow_expr<V: LazyBoundary>(v: &mut V, node: &ArrowExpr) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

fn lazy_visit_method_prop<V: LazyBoundary>(v: &mut V, node: &MethodProp) {
    node.key.visit_with(v);
    v.descend_lazy(|s| node.function.visit_with(s));
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
        ClassMember::Method(method) => {
            v.descend_lazy(|s| method.function.visit_with(s));
        }
        ClassMember::PrivateMethod(method) => {
            v.descend_lazy(|s| method.function.visit_with(s));
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
