use super::*;

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
pub(crate) struct StatementFactsCollector {
    pub(crate) reads: PositionBucketed<BTreeSet<Id>>,
    pub(crate) rebinds: PositionBucketed<BTreeSet<Id>>,
    pub(crate) calls: PositionBucketed<BTreeSet<Id>>,
    pub(crate) at_init_unresolved_sources: BTreeSet<Id>,
    pub(crate) at_init_unresolved_inline_fn: bool,
    pub(crate) first_order_unresolved_sources: BTreeSet<Id>,
    pub(crate) first_order_unresolved_inline_fn: bool,
    pub(crate) global_writes: BTreeSet<String>,
    pub(crate) global_reads: BTreeSet<String>,
    pub(crate) cell_writes_summarizable: bool,
    pub(crate) dataflow_summarizable: bool,
    /// Unshadowed global-object alias names for this chunk
    /// (`globalThis`, `window`, ...). See
    /// [`unshadowed_global_object_aliases`].
    global_object_names: BTreeSet<&'static str>,
    async_direct_function_bindings: BTreeSet<Id>,
    promise_global_unshadowed: bool,
    lazy_depth: u32,
    past_await: bool,
}

impl StatementFactsCollector {
    pub(crate) fn new(
        global_object_names: BTreeSet<&'static str>,
        async_direct_function_bindings: BTreeSet<Id>,
        promise_global_unshadowed: bool,
    ) -> Self {
        Self {
            cell_writes_summarizable: true,
            dataflow_summarizable: true,
            global_object_names,
            async_direct_function_bindings,
            promise_global_unshadowed,
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
    fn record_unresolved_call<N>(&mut self, node: &N)
    where
        N: VisitWith<UnresolvedCallSourceCollector> + VisitWith<SyncInlineEffectCollector>,
    {
        if self.lazy_depth > 1 || (self.lazy_depth == 1 && self.past_await) {
            return;
        }
        let mut sources = UnresolvedCallSourceCollector::default();
        node.visit_with(&mut sources);
        let needs_owner_inline_fallback = if sources.inline_fn && self.lazy_depth > 0 {
            let mut inline_effects =
                SyncInlineEffectCollector::new(self.lazy_depth, self.past_await);
            node.visit_with(&mut inline_effects);
            let needs_owner_fallback = inline_effects.needs_owner_fallback;
            self.merge_sync_inline_effects(inline_effects.effects);
            needs_owner_fallback
        } else {
            sources.inline_fn
        };
        if self.lazy_depth == 0 {
            self.at_init_unresolved_sources.extend(sources.idents);
            self.at_init_unresolved_inline_fn |= needs_owner_inline_fallback;
        } else {
            self.first_order_unresolved_sources.extend(sources.idents);
            self.first_order_unresolved_inline_fn |= needs_owner_inline_fallback;
        }
    }

    fn merge_sync_inline_effects(&mut self, effects: SyncInlineEffects) {
        self.reads.extend(effects.reads);
        self.rebinds.extend(effects.rebinds);
        self.calls.extend(effects.calls);
        self.at_init_unresolved_sources
            .extend(effects.unresolved_sources.eager);
        self.first_order_unresolved_sources
            .extend(effects.unresolved_sources.first_order_lazy);
    }

    fn is_known_promise_reaction_call(&self, node: &CallExpr) -> bool {
        let Callee::Expr(callee) = &node.callee else {
            return false;
        };
        let Expr::Member(member) = strip_parens(callee) else {
            return false;
        };
        if !matches!(
            &member.prop,
            MemberProp::Ident(prop)
                if matches!(prop.sym.as_ref(), "then" | "catch" | "finally")
        ) {
            return false;
        }
        self.is_known_promise_expr(member.obj.as_ref())
    }

    fn is_known_promise_expr(&self, expr: &Expr) -> bool {
        match strip_parens(expr) {
            Expr::Call(call) => {
                self.is_async_direct_function_call(call) || self.is_promise_static_call(call)
            }
            Expr::New(new_expr) => {
                self.promise_global_unshadowed
                    && matches!(strip_parens(&new_expr.callee), Expr::Ident(ident) if ident.sym.as_ref() == "Promise")
            }
            _ => false,
        }
    }

    fn is_known_event_listener_registration_call(&self, node: &CallExpr) -> bool {
        let Callee::Expr(callee) = &node.callee else {
            return false;
        };
        is_static_event_listener_registration(callee, &node.args)
    }

    fn is_known_event_listener_registration_opt_call(&self, node: &OptCall) -> bool {
        is_static_event_listener_registration(&node.callee, &node.args)
    }

    fn is_async_direct_function_call(&self, call: &CallExpr) -> bool {
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let Expr::Ident(ident) = strip_parens(callee) else {
            return false;
        };
        self.async_direct_function_bindings.contains(&ident.to_id())
    }

    fn is_promise_static_call(&self, call: &CallExpr) -> bool {
        if !self.promise_global_unshadowed {
            return false;
        }
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let Expr::Member(member) = strip_parens(callee) else {
            return false;
        };
        matches!(strip_parens(member.obj.as_ref()), Expr::Ident(obj) if obj.sym.as_ref() == "Promise")
            && matches!(
                &member.prop,
                MemberProp::Ident(prop)
                    if matches!(
                        prop.sym.as_ref(),
                        "resolve" | "reject" | "all" | "allSettled" | "any" | "race"
                    )
            )
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
pub(crate) struct UnresolvedCallSourceCollector {
    pub(crate) idents: BTreeSet<Id>,
    pub(crate) inline_fn: bool,
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

#[derive(Default)]
struct SyncInlineEffects {
    reads: PositionBucketed<BTreeSet<Id>>,
    rebinds: PositionBucketed<BTreeSet<Id>>,
    calls: PositionBucketed<BTreeSet<Id>>,
    unresolved_sources: PositionBucketed<BTreeSet<Id>>,
}

/// Effects from inline function arguments that an unresolved call may
/// invoke synchronously. The containing statement/function already has
/// its own lazy-depth position; if the callback fires immediately, the
/// callback body's pre-await effects happen at that same position.
pub(crate) struct SyncInlineEffectCollector {
    effects: SyncInlineEffects,
    outer_lazy_depth: u32,
    outer_past_await: bool,
    inline_depth: u32,
    past_await: bool,
    needs_owner_fallback: bool,
}

impl SyncInlineEffectCollector {
    fn new(outer_lazy_depth: u32, outer_past_await: bool) -> Self {
        Self {
            effects: SyncInlineEffects::default(),
            outer_lazy_depth,
            outer_past_await,
            inline_depth: 0,
            past_await: false,
            needs_owner_fallback: false,
        }
    }

    fn active(&self) -> bool {
        self.inline_depth > 0 && !self.outer_past_await && !self.past_await
    }

    fn record_read(&mut self, id: &Id) {
        if self.active() {
            self.effects
                .reads
                .record(id, self.outer_lazy_depth, self.outer_past_await);
        }
    }

    fn record_write(&mut self, id: &Id) {
        if self.active() {
            self.effects
                .rebinds
                .record(id, self.outer_lazy_depth, self.outer_past_await);
        }
    }

    fn record_call(&mut self, id: &Id) {
        if self.active() {
            self.effects
                .calls
                .record(id, self.outer_lazy_depth, self.outer_past_await);
        }
    }

    fn record_unresolved_call<N: VisitWith<UnresolvedCallSourceCollector>>(&mut self, node: &N) {
        if !self.active() {
            return;
        }
        let mut sources = UnresolvedCallSourceCollector::default();
        node.visit_with(&mut sources);
        for id in sources.idents {
            self.effects.unresolved_sources.record(
                &id,
                self.outer_lazy_depth,
                self.outer_past_await,
            );
        }
    }

    fn visit_inline_function_body(&mut self, visit_body: impl FnOnce(&mut Self)) {
        if self.outer_past_await || self.past_await {
            return;
        }
        let saved_past_await = self.past_await;
        self.past_await = false;
        self.inline_depth += 1;
        visit_body(self);
        self.inline_depth -= 1;
        self.past_await = saved_past_await;
    }
}

impl TargetAccessRecorder for SyncInlineEffectCollector {
    fn record_binding_write(&mut self, id: &Id) {
        self.record_write(id);
    }

    fn record_member_write(&mut self, _id: &Id) {}
}

impl Visit for SyncInlineEffectCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.record_read(&node.to_id());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_function(&mut self, node: &Function) {
        self.visit_inline_function_body(|s| {
            for param in &node.params {
                param.visit_with(s);
            }
            if let Some(body) = &node.body {
                body.visit_with(s);
            }
        });
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.visit_inline_function_body(|s| {
            for param in &node.params {
                param.visit_with(s);
            }
            node.body.visit_with(s);
        });
    }

    fn visit_class(&mut self, _node: &Class) {
        self.needs_owner_fallback = true;
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        if self.inline_depth > 0 {
            self.past_await = true;
        }
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            callee.visit_with(self);
        }
        for arg in &node.args {
            arg.visit_with(self);
        }
        if self.active() {
            match &node.callee {
                Callee::Expr(callee) => match strip_parens(callee) {
                    Expr::Ident(ident) => self.record_call(&ident.to_id()),
                    _ => self.record_unresolved_call(node),
                },
                Callee::Import(_) | Callee::Super(_) => {}
            }
        }
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        node.callee.visit_with(self);
        for arg in &node.args {
            arg.visit_with(self);
        }
        self.record_unresolved_call(node);
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        node.tag.visit_with(self);
        node.tpl.visit_with(self);
        self.record_unresolved_call(node);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if self.active() {
            record_assign_target(&node.left, self);
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        if self.active() {
            record_update_target(&node.arg, self);
        }
        node.arg.visit_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if self.active()
            && let ForHead::Pat(pattern) = &node.left
        {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if self.active()
            && let ForHead::Pat(pattern) = &node.left
        {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
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
        // Callee/argument expressions evaluate before the call. If one
        // of them contains an `await` in an async body, the call itself
        // happens after suspension and must not be first-order.
        if let Callee::Expr(callee) = &node.callee {
            callee.visit_with(self);
        }
        for arg in &node.args {
            arg.visit_with(self);
        }
        match &node.callee {
            Callee::Expr(callee) => match strip_parens(callee) {
                Expr::Ident(ident) => self.record_call(&ident.to_id()),
                _ if !self.is_known_promise_reaction_call(node)
                    && !self.is_known_event_listener_registration_call(node) =>
                {
                    self.record_unresolved_call(node);
                }
                _ => {}
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
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        node.callee.visit_with(self);
        if let Some(args) = &node.args {
            for arg in args {
                arg.visit_with(self);
            }
        }
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
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        // `f?.()` / `obj?.m()`: the callee is never a resolvable
        // bare-Ident shape for promotion.
        node.callee.visit_with(self);
        for arg in &node.args {
            arg.visit_with(self);
        }
        if !self.is_known_event_listener_registration_opt_call(node) {
            self.record_unresolved_call(node);
        }
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        // `` tag`...` `` invokes the tag function.
        node.tag.visit_with(self);
        node.tpl.visit_with(self);
        self.record_unresolved_call(node);
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
