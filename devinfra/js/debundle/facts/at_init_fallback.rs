use super::*;

pub(crate) fn safe_plain_array_at_init_fallback_sources(
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    graph: &ChunkCodeGraph,
) -> BTreeSet<Id> {
    let mut out = BTreeSet::new();
    if let Some(var) = var_decl_of_item(item) {
        for decl in &var.decls {
            let Some(init) = decl.init.as_deref() else {
                continue;
            };
            if let Some(root) = object_from_entries_plain_array_chain_root(init, shadowed, graph) {
                out.insert(root);
            }
            if let Some(root) = plain_array_map_filter_chain_root(init, graph) {
                out.insert(root);
            }
            collect_array_literal_plain_array_spread_roots(init, graph, &mut out);
        }
    }
    if let ModuleItem::Stmt(Stmt::Expr(expr)) = item
        && let Some(root) = plain_array_for_each_root(&expr.expr, graph)
    {
        out.insert(root);
    }
    out
}

fn object_from_entries_plain_array_chain_root(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    graph: &ChunkCodeGraph,
) -> Option<Id> {
    let Expr::Call(call) = strip_parens(expr) else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() || shadowed.contains("Object") {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = strip_parens(callee) else {
        return None;
    };
    if !matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Object" && prop.sym.as_ref() == "fromEntries"
    ) {
        return None;
    }
    plain_array_map_filter_chain_root(&call.args[0].expr, graph)
}

fn collect_array_literal_plain_array_spread_roots(
    expr: &Expr,
    graph: &ChunkCodeGraph,
    out: &mut BTreeSet<Id>,
) {
    let Expr::Array(array) = strip_parens(expr) else {
        return;
    };
    for elem in array.elems.iter().flatten() {
        if elem.spread.is_some()
            && let Some(root) = plain_array_map_filter_chain_root(&elem.expr, graph)
        {
            out.insert(root);
        }
    }
}

fn plain_array_for_each_root(expr: &Expr, graph: &ChunkCodeGraph) -> Option<Id> {
    let Expr::Call(call) = strip_parens(expr) else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = strip_parens(callee) else {
        return None;
    };
    if !matches!(&member.prop, MemberProp::Ident(prop) if prop.sym.as_ref() == "forEach")
        || !callback_has_no_sync_invocation(&call.args[0].expr)
    {
        return None;
    }
    match strip_parens(member.obj.as_ref()) {
        Expr::Ident(recv) if graph.is_plain_array(recv.sym.as_ref()) => Some(recv.to_id()),
        _ => None,
    }
}

fn plain_array_map_filter_chain_root(expr: &Expr, graph: &ChunkCodeGraph) -> Option<Id> {
    let Expr::Call(call) = strip_parens(expr) else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = strip_parens(callee) else {
        return None;
    };
    if !matches!(
        &member.prop,
        MemberProp::Ident(prop) if matches!(prop.sym.as_ref(), "filter" | "map")
    ) || !callback_has_no_sync_invocation(&call.args[0].expr)
    {
        return None;
    }
    match strip_parens(member.obj.as_ref()) {
        Expr::Ident(recv) if graph.is_plain_array(recv.sym.as_ref()) => Some(recv.to_id()),
        other => plain_array_map_filter_chain_root(other, graph),
    }
}

fn callback_has_no_sync_invocation(expr: &Expr) -> bool {
    let mut finder = SyncInvocationFinder::default();
    match strip_parens(expr) {
        Expr::Arrow(arrow) => arrow.body.visit_with(&mut finder),
        Expr::Fn(function) => {
            if let Some(body) = &function.function.body {
                body.visit_with(&mut finder);
            }
        }
        _ => return false,
    }
    !finder.found
}

#[derive(Default)]
struct SyncInvocationFinder {
    found: bool,
}

impl Visit for SyncInvocationFinder {
    fn visit_call_expr(&mut self, _node: &CallExpr) {
        self.found = true;
    }

    fn visit_opt_call(&mut self, _node: &OptCall) {
        self.found = true;
    }

    fn visit_new_expr(&mut self, _node: &NewExpr) {
        self.found = true;
    }

    fn visit_tagged_tpl(&mut self, _node: &TaggedTpl) {
        self.found = true;
    }

    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_class(&mut self, _node: &Class) {}
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

pub(crate) fn has_opaque_at_init_call(
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

pub(crate) fn has_untrusted_at_init_unresolved_inline_fn_call(
    item: &ModuleItem,
    hints: &AnalysisHints,
) -> bool {
    let mut finder = UntrustedAtInitInlineFnFallbackFinder {
        no_sync_callback_members: &hints.no_sync_callback_members,
        found: false,
        lazy_depth: 0,
    };
    item.visit_with(&mut finder);
    finder.found
}

pub(crate) fn no_sync_member_argument_fallback_sources(
    item: &ModuleItem,
    hints: &AnalysisHints,
) -> PositionBucketed<BTreeSet<Id>> {
    let mut collector = NoSyncMemberArgumentSourceCollector {
        no_sync_callback_members: &hints.no_sync_callback_members,
        sources: PositionBucketed::default(),
        lazy_depth: 0,
        past_await: false,
    };
    item.visit_with(&mut collector);
    collector.sources
}

pub(crate) fn is_static_event_listener_registration(callee: &Expr, args: &[ExprOrSpread]) -> bool {
    args.len() >= 2
        && args
            .first()
            .is_some_and(|arg| is_static_event_name(&arg.expr))
        && expr_is_add_event_listener_member(callee)
}

fn is_static_event_name(expr: &Expr) -> bool {
    match strip_parens(expr) {
        Expr::Lit(Lit::Str(_)) => true,
        Expr::Tpl(Tpl { exprs, .. }) => exprs.is_empty(),
        _ => false,
    }
}

fn expr_is_add_event_listener_member(expr: &Expr) -> bool {
    match strip_parens(expr) {
        Expr::Member(member) => member_is_add_event_listener(member),
        Expr::OptChain(opt) => match opt.base.as_ref() {
            OptChainBase::Member(member) => member_is_add_event_listener(member),
            OptChainBase::Call(_) => false,
        },
        _ => false,
    }
}

fn member_is_add_event_listener(member: &MemberExpr) -> bool {
    matches!(
        &member.prop,
        MemberProp::Ident(prop) if prop.sym.as_ref() == "addEventListener"
    )
}

fn is_no_sync_callback_member_call(
    callee: &Expr,
    no_sync_callback_members: &BTreeMap<String, BTreeSet<String>>,
) -> bool {
    match strip_parens(callee) {
        Expr::Member(member) => member_matches_no_sync_callback(member, no_sync_callback_members),
        Expr::OptChain(opt) => match opt.base.as_ref() {
            OptChainBase::Member(member) => {
                member_matches_no_sync_callback(member, no_sync_callback_members)
            }
            OptChainBase::Call(_) => false,
        },
        _ => false,
    }
}

fn member_matches_no_sync_callback(
    member: &MemberExpr,
    no_sync_callback_members: &BTreeMap<String, BTreeSet<String>>,
) -> bool {
    let Expr::Ident(receiver) = strip_parens(member.obj.as_ref()) else {
        return false;
    };
    let MemberProp::Ident(prop) = &member.prop else {
        return false;
    };
    no_sync_callback_members
        .get(receiver.sym.as_ref())
        .is_some_and(|props| props.contains(prop.sym.as_ref()))
}

struct NoSyncMemberArgumentSourceCollector<'a> {
    no_sync_callback_members: &'a BTreeMap<String, BTreeSet<String>>,
    sources: PositionBucketed<BTreeSet<Id>>,
    lazy_depth: u32,
    past_await: bool,
}

impl NoSyncMemberArgumentSourceCollector<'_> {
    fn collect_no_sync_args(&mut self, args: &[ExprOrSpread]) {
        if self.lazy_depth > 1 || (self.lazy_depth == 1 && self.past_await) {
            return;
        }
        for arg in args {
            let mut sources = UnresolvedCallSourceCollector::default();
            arg.expr.visit_with(&mut sources);
            for id in sources.idents {
                self.sources.record(&id, self.lazy_depth, self.past_await);
            }
        }
    }
}

impl LazyBoundary for NoSyncMemberArgumentSourceCollector<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
}

impl Visit for NoSyncMemberArgumentSourceCollector<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && is_no_sync_callback_member_call(callee, self.no_sync_callback_members)
        {
            self.collect_no_sync_args(&node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        if is_no_sync_callback_member_call(&node.callee, self.no_sync_callback_members) {
            self.collect_no_sync_args(&node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.visit_children_with(self);
        self.past_await = true;
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
}

struct UntrustedAtInitInlineFnFallbackFinder<'a> {
    no_sync_callback_members: &'a BTreeMap<String, BTreeSet<String>>,
    found: bool,
    lazy_depth: u32,
}

impl UntrustedAtInitInlineFnFallbackFinder<'_> {
    fn is_no_sync_callback_member_call(&self, node: &CallExpr) -> bool {
        let Callee::Expr(callee) = &node.callee else {
            return false;
        };
        is_no_sync_callback_member_call(callee, self.no_sync_callback_members)
    }

    fn node_has_inline_fn<N: VisitWith<UnresolvedCallSourceCollector>>(&self, node: &N) -> bool {
        let mut sources = UnresolvedCallSourceCollector::default();
        node.visit_with(&mut sources);
        sources.inline_fn
    }
}

impl Visit for UntrustedAtInitInlineFnFallbackFinder<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if self.found {
            return;
        }
        if self.lazy_depth == 0 {
            match &node.callee {
                Callee::Expr(callee) => match strip_parens(callee) {
                    Expr::Ident(_) => {}
                    _ if self.node_has_inline_fn(node)
                        && !is_static_event_listener_registration(callee, &node.args)
                        && !self.is_no_sync_callback_member_call(node) =>
                    {
                        self.found = true;
                        return;
                    }
                    _ => {}
                },
                Callee::Import(_) | Callee::Super(_) => {}
            }
        }
        node.visit_children_with(self);
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        if self.lazy_depth == 0
            && self.node_has_inline_fn(node)
            && !is_static_event_listener_registration(&node.callee, &node.args)
        {
            self.found = true;
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        if self.lazy_depth == 0 && self.node_has_inline_fn(node) {
            self.found = true;
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}
    fn visit_class(&mut self, _node: &Class) {}
}
