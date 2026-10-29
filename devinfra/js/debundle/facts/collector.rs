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
    pub(crate) binding_idents: BTreeSet<Id>,
    pub(crate) var_declared: BTreeSet<Id>,
    pub(crate) var_initializers: BTreeSet<Id>,
    /// Multiplicity for `var` declarations and initializer writes, collected
    /// in the same visitor pass as reads and rebinds.
    pub(crate) var_declaration_counts: BTreeMap<Id, usize>,
    pub(crate) var_initializer_counts: BTreeMap<Id, usize>,
    pub(crate) reads: PositionBucketed<BTreeSet<Id>>,
    pub(crate) rebinds: PositionBucketed<BTreeSet<Id>>,
    /// Direct writes through a binding, including lazy bodies. A trusted
    /// imported decorator member cannot retain its identity if written here.
    pub(crate) member_write_roots: BTreeSet<Id>,
    /// Source-visible direct value escapes of simple parameters in direct
    /// top-level function declarations. Member mutations use the existing
    /// `member_write_roots` fact instead.
    pub(crate) argument_value_escapes: BTreeMap<Id, BTreeSet<usize>>,
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
    may_skip_await_depth: u32,
    active_argument_parameters: Option<(Id, BTreeMap<Id, usize>, u32)>,
}

impl StatementFactsCollector {
    fn mark_parameter_value_escape(&mut self, expr: &Expr) {
        let Some((binding, parameters, _)) = &self.active_argument_parameters else {
            return;
        };
        let escaped: Vec<_> = parameters
            .iter()
            .filter_map(|(parameter, index)| {
                expression_may_retain_parameter(expr, parameter).then_some(*index)
            })
            .collect();
        self.argument_value_escapes
            .entry(binding.clone())
            .or_default()
            .extend(escaped);
    }
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
        let Expr::Member(member) = callee.unwrap_parens() else {
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
        match expr.unwrap_parens() {
            Expr::Call(call) => {
                self.is_async_direct_function_call(call) || self.is_promise_static_call(call)
            }
            Expr::New(new_expr) => {
                self.promise_global_unshadowed
                    && matches!(new_expr.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "Promise")
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
        let Expr::Ident(ident) = callee.unwrap_parens() else {
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
        let Expr::Member(member) = callee.unwrap_parens() else {
            return false;
        };
        matches!(member.obj.as_ref().unwrap_parens(), Expr::Ident(obj) if obj.sym.as_ref() == "Promise")
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
        matches!(expr.unwrap_parens(), Expr::Ident(i) if self.global_object_names.contains(i.sym.as_ref()))
    }

    fn record_global_prop(&mut self, member: &MemberExpr, is_write: bool) {
        if !self.is_global_object_expr(&member.obj) {
            return;
        }
        let key = match &member.prop {
            MemberProp::Ident(ident) => Some(ident.sym.to_string()),
            MemberProp::Computed(ComputedPropName { expr, .. }) => match expr.unwrap_parens() {
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

fn expression_may_retain_parameter(expr: &Expr, parameter: &Id) -> bool {
    match expr.unwrap_parens() {
        Expr::Ident(id) => &id.to_id() == parameter,
        Expr::Member(member) => expression_may_retain_parameter(&member.obj, parameter),
        Expr::Cond(cond) => {
            expression_may_retain_parameter(&cond.cons, parameter)
                || expression_may_retain_parameter(&cond.alt, parameter)
        }
        Expr::Bin(bin) => {
            expression_may_retain_parameter(&bin.left, parameter)
                || expression_may_retain_parameter(&bin.right, parameter)
        }
        Expr::Seq(seq) => seq
            .exprs
            .iter()
            .any(|expr| expression_may_retain_parameter(expr, parameter)),
        Expr::Assign(assign) => expression_may_retain_parameter(&assign.right, parameter),
        // Calls are under the author's opaque-callee obligation; the direct
        // call result is not a syntactically proven alias of the parameter.
        _ => false,
    }
}

/// Only these lazy class positions execute when a class is constructed.
/// `visit_class_member` supplies the same depth/await rules as the ordinary
/// collector, while skipping methods until the constructor may invoke one.
pub(crate) fn collect_constructor_facts(
    class: &Class,
    full: &StatementFactsCollector,
    global_object_names: BTreeSet<&'static str>,
    async_direct_function_bindings: BTreeSet<Id>,
    promise_global_unshadowed: bool,
) -> ConstructorFacts {
    let mut collector = StatementFactsCollector::new(
        global_object_names,
        async_direct_function_bindings,
        promise_global_unshadowed,
    );
    let mut method_may_run = false;
    for member in &class.body {
        let constructed = match member {
            ClassMember::Constructor(ctor) => {
                let mut finder = ConstructorMethodUse::default();
                ctor.visit_with(&mut finder);
                method_may_run |= finder.found;
                true
            }
            ClassMember::ClassProp(prop) => !prop.is_static,
            ClassMember::PrivateProp(prop) => !prop.is_static,
            ClassMember::AutoAccessor(accessor) => !accessor.is_static,
            _ => false,
        };
        if constructed {
            member.visit_with(&mut collector);
        }
    }
    let mut result = ConstructorFacts {
        reads: collector.reads.first_order_lazy,
        rebinds: collector.rebinds.first_order_lazy,
        calls: collector.calls.first_order_lazy,
        unresolved_sources: collector.first_order_unresolved_sources,
        unresolved_inline_fn: collector.first_order_unresolved_inline_fn,
    };
    if let Some(super_class) = class.super_class.as_deref() {
        match super_class.unwrap_parens() {
            Expr::Ident(base) => {
                result.calls.insert(base.to_id());
            }
            _ => {
                let mut sources = UnresolvedCallSourceCollector::default();
                super_class.visit_with(&mut sources);
                result.unresolved_sources.extend(sources.idents);
                result.unresolved_inline_fn |= sources.inline_fn;
            }
        }
    }
    if method_may_run {
        result
            .reads
            .extend(full.reads.first_order_lazy.iter().cloned());
        result
            .rebinds
            .extend(full.rebinds.first_order_lazy.iter().cloned());
        result
            .calls
            .extend(full.calls.first_order_lazy.iter().cloned());
        result
            .unresolved_sources
            .extend(full.first_order_unresolved_sources.iter().cloned());
        result.unresolved_inline_fn |= full.first_order_unresolved_inline_fn;
    }
    result
}

#[derive(Default)]
struct ConstructorMethodUse {
    found: bool,
}

impl Visit for ConstructorMethodUse {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::This(_))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Some(init) = node.init.as_deref()
            && let Expr::Member(member) = init.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::This(_))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if let Expr::Member(member) = node.right.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::This(_))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }
}

/// Collects the ident reads and inline-function presence inside an
/// unresolved call expression. Static member prop names are
/// `IdentName`s (not `Ident`s) and are not collected; function /
/// arrow / class / accessor interiors are skipped — their contents
/// are already covered by the source_unit's lazy sets, which the
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
    may_skip_await_depth: u32,
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
            may_skip_await_depth: 0,
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

impl LazyBoundary for SyncInlineEffectCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.inline_depth
    }
    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
    }
}

impl Visit for SyncInlineEffectCollector {
    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_if_stmt(&mut self, node: &IfStmt) {
        lazy_visit_if_stmt(self, node);
    }
    fn visit_cond_expr(&mut self, node: &CondExpr) {
        lazy_visit_cond_expr(self, node);
    }
    fn visit_bin_expr(&mut self, node: &BinExpr) {
        lazy_visit_bin_expr(self, node);
    }
    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        lazy_visit_may_skip(self, node);
    }

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
        if self.inline_depth > 0 && self.may_skip_await_depth == 0 {
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
                Callee::Expr(callee) => match callee.unwrap_parens() {
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
        let before_assignment = self.past_await;
        if self.active() {
            record_assign_target(&node.left, self);
        }
        node.visit_children_with(self);
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        if self.active() {
            record_update_target(&node.arg, self);
        }
        node.arg.visit_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        let before_loop = self.past_await;
        if self.active()
            && let ForHead::Pat(pattern) = &node.left
        {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
        self.past_await = before_loop;
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        let before_loop = self.past_await;
        if self.active()
            && let ForHead::Pat(pattern) = &node.left
        {
            record_pat_write(pattern, self);
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
        self.past_await = before_loop;
    }
}

impl LazyBoundary for StatementFactsCollector {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }
    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
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
    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_if_stmt(&mut self, node: &IfStmt) {
        lazy_visit_if_stmt(self, node);
    }
    fn visit_cond_expr(&mut self, node: &CondExpr) {
        lazy_visit_cond_expr(self, node);
    }
    fn visit_bin_expr(&mut self, node: &BinExpr) {
        lazy_visit_bin_expr(self, node);
    }
    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        lazy_visit_may_skip(self, node);
    }

    fn visit_var_decl(&mut self, node: &VarDecl) {
        if node.kind == VarDeclKind::Var {
            for decl in &node.decls {
                for id in binding_targets::binding_names(&decl.name) {
                    *self.var_declaration_counts.entry(id.clone()).or_default() += 1;
                    if decl.init.is_some() {
                        *self.var_initializer_counts.entry(id.clone()).or_default() += 1;
                    }
                    if self.lazy_depth == 0 {
                        self.var_declared.insert(id.clone());
                        if decl.init.is_some() {
                            self.var_initializers.insert(id);
                        }
                    }
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_ident(&mut self, node: &Ident) {
        self.record_read(&node.to_id());
        if let Some((binding, parameters, depth)) = &self.active_argument_parameters
            && self.lazy_depth > *depth
            && let Some(index) = parameters.get(&node.to_id())
        {
            self.argument_value_escapes
                .entry(binding.clone())
                .or_default()
                .insert(*index);
        }
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(argument) = &node.arg {
            self.mark_parameter_value_escape(argument);
        }
        node.visit_children_with(self);
    }

    fn visit_throw_stmt(&mut self, node: &ThrowStmt) {
        self.mark_parameter_value_escape(&node.arg);
        node.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Some(init) = &node.init {
            self.mark_parameter_value_escape(init);
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        for element in node.elems.iter().flatten() {
            self.mark_parameter_value_escape(&element.expr);
        }
        node.visit_children_with(self);
    }

    fn visit_key_value_prop(&mut self, node: &KeyValueProp) {
        self.mark_parameter_value_escape(&node.value);
        node.visit_children_with(self);
    }

    fn visit_prop(&mut self, node: &Prop) {
        if let Prop::Shorthand(id) = node {
            self.mark_parameter_value_escape(&Expr::Ident(id.clone()));
        }
        node.visit_children_with(self);
    }

    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.binding_idents.insert(node.id.to_id());
    }
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
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        self.mark_parameter_value_escape(&node.right);
        let before_assignment = self.past_await;
        record_assign_target(&node.left, self);
        if let Some(member) = simple_assign_member_target(&node.left)
            && let Expr::Ident(root) = member.obj.unwrap_parens()
        {
            self.member_write_roots.insert(root.to_id());
        }
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
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        record_update_target(&node.arg, self);
        if let Expr::Member(member) = node.arg.unwrap_parens()
            && let Expr::Ident(root) = member.obj.unwrap_parens()
        {
            self.member_write_roots.insert(root.to_id());
        }
        if self.lazy_depth == 0 {
            match node.arg.unwrap_parens() {
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

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && let Expr::Member(member) = node.arg.unwrap_parens()
            && let Expr::Ident(root) = member.obj.unwrap_parens()
        {
            self.member_write_roots.insert(root.to_id());
        }
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        let before_loop = self.past_await;
        match &node.left {
            ForHead::Pat(pattern) => record_pat_write(pattern, self),
            ForHead::VarDecl(var) => {
                for decl in &var.decls {
                    record_pat_write(&decl.name, self);
                }
            }
            ForHead::UsingDecl(_) => {}
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
        self.past_await = before_loop;
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        let before_loop = self.past_await;
        match &node.left {
            ForHead::Pat(pattern) => record_pat_write(pattern, self),
            ForHead::VarDecl(var) => {
                for decl in &var.decls {
                    record_pat_write(&decl.name, self);
                }
            }
            ForHead::UsingDecl(_) => {}
        }
        node.left.visit_with(self);
        node.right.visit_with(self);
        node.body.visit_with(self);
        self.past_await = before_loop;
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
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let (Expr::Ident(intrinsic), MemberProp::Ident(method)) =
                (member.obj.unwrap_parens(), &member.prop)
            && matches!(
                (intrinsic.sym.as_ref(), method.sym.as_ref()),
                (
                    "Object",
                    "assign" | "defineProperty" | "defineProperties" | "setPrototypeOf"
                ) | ("Reflect", "set" | "defineProperty" | "setPrototypeOf")
            )
            && let Some(first) = node.args.first()
            && let Expr::Ident(root) = first.expr.unwrap_parens()
        {
            self.member_write_roots.insert(root.to_id());
        }
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
            Callee::Expr(callee) => match callee.unwrap_parens() {
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
                && let Expr::Member(member) = expr.unwrap_parens()
                && let MemberProp::Ident(prop) = &member.prop
                && prop.sym.as_ref() == "defineProperty"
                && matches!(
                    member.obj.unwrap_parens(),
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
        // `new Foo(...)` synchronously runs a chunk-local class constructor
        // (and its instance-field initializers). Treat its bare callee as an
        // invocation for at-init body promotion, just like `Foo(...)`.
        if let Expr::Ident(ident) = node.callee.unwrap_parens() {
            self.record_call(&ident.to_id());
        }
        if self.lazy_depth == 0
            && let Expr::Ident(ident) = node.callee.unwrap_parens()
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

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        // The name is a declaration, not an evaluation-time read. Visit the
        // function normally so recursive references in its body stay lazy.
        if self.lazy_depth == 0 {
            let parameters = node
                .function
                .params
                .iter()
                .enumerate()
                .filter_map(|(index, parameter)| match &parameter.pat {
                    Pat::Ident(id) => Some((id.id.to_id(), index)),
                    _ => None,
                })
                .collect();
            self.active_argument_parameters =
                Some((node.ident.to_id(), parameters, self.lazy_depth + 1));
            node.function.visit_with(self);
            self.active_argument_parameters = None;
        } else {
            node.function.visit_with(self);
        }
    }

    fn visit_fn_expr(&mut self, node: &FnExpr) {
        // A named function expression's local name is a binding too.
        node.function.visit_with(self);
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
    fn visit_class_decl(&mut self, node: &ClassDecl) {
        // The declaration name binds the class; evaluating its definition
        // does not read that binding. Heritage, computed keys, static fields,
        // and method bodies still visit through the class node below.
        node.class.visit_with(self);
    }
    fn visit_class_expr(&mut self, node: &ClassExpr) {
        // A named class expression has a local binding with the same rule.
        node.class.visit_with(self);
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
        SimpleAssignTarget::Paren(paren) => match paren.expr.unwrap_parens() {
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
