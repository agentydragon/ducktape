use super::*;
use crate::facts::abstract_domain::builtins::member_call_row;
use crate::facts::abstract_domain::heap::sees_only_element;

/// Plain-array roots an at-init statement iterates only through
/// `%Array.prototype%` `forEach`/`map`/`filter` with call-free callbacks
/// that run no user code on the elements, so nothing reachable through the
/// array runs and the array adds nothing to the statement's fallback reach.
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
            if let Some(stage) = plain_array_chain(init, Consumer::Values, graph) {
                out.insert(stage.root);
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
    let Expr::Call(call) = expr.unwrap_parens() else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() || shadowed.contains("Object") {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = callee.unwrap_parens() else {
        return None;
    };
    if !matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Object" && prop.sym.as_ref() == "fromEntries"
    ) {
        return None;
    }
    plain_array_chain(&call.args[0].expr, Consumer::Entries, graph).map(|stage| stage.root)
}

fn collect_array_literal_plain_array_spread_roots(
    expr: &Expr,
    graph: &ChunkCodeGraph,
    out: &mut BTreeSet<Id>,
) {
    let Expr::Array(array) = expr.unwrap_parens() else {
        return;
    };
    for elem in array.elems.iter().flatten() {
        if elem.spread.is_some()
            && let Some(stage) = plain_array_chain(&elem.expr, Consumer::Values, graph)
        {
            out.insert(stage.root);
        }
    }
}

/// How the elements a chain produces are consumed.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Consumer {
    /// Stored or iterated by identity (an array spread, a binding).
    Values,
    /// `Object.fromEntries`: `[[Get]]` of `"0"` and `"1"` on each element,
    /// then `ToPropertyKey` of the first.
    Entries,
}

/// A plain-array chain: its root binding and whether the elements reaching
/// this point are the root's primitive elements.
struct Stage {
    root: Id,
    primitive: bool,
}

/// The stage `call` reads its elements from: a plain-array binding, or the
/// result of an earlier admitted stage.
fn receiver_stage(call: &CallExpr, method: &[&str], graph: &ChunkCodeGraph) -> Option<Stage> {
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&member.prop, MemberProp::Ident(prop) if method.contains(&prop.sym.as_ref()))
        || !callback_has_no_sync_invocation(&call.args[0].expr)
        || !sees_only_element(&call.args[0].expr)
    {
        return None;
    }
    match member.obj.as_ref().unwrap_parens() {
        Expr::Ident(recv) if graph.is_plain_array(recv.sym.as_ref()) => Some(Stage {
            root: recv.to_id(),
            primitive: graph.is_primitive_element_array(recv.sym.as_ref()),
        }),
        other => plain_array_chain(other, Consumer::Values, graph),
    }
}

fn plain_array_for_each_root(expr: &Expr, graph: &ChunkCodeGraph) -> Option<Id> {
    let Expr::Call(call) = expr.unwrap_parens() else {
        return None;
    };
    let input = receiver_stage(call, &["forEach"], graph)?;
    callback_is_silent_on_elements(&call.args[0].expr, input.primitive, Use::Value)
        .then_some(input.root)
}

/// A `map`/`filter` chain over a plain array whose callbacks, and the
/// chain's `consumer`, run no user code on the elements.
fn plain_array_chain(expr: &Expr, consumer: Consumer, graph: &ChunkCodeGraph) -> Option<Stage> {
    let Expr::Call(call) = expr.unwrap_parens() else {
        return None;
    };
    let input = receiver_stage(call, &["filter", "map"], graph)?;
    let callback = &call.args[0].expr;
    let filter = matches!(
        &call.callee,
        Callee::Expr(callee) if matches!(callee.unwrap_parens(), Expr::Member(member)
            if matches!(&member.prop, MemberProp::Ident(prop) if prop.sym.as_ref() == "filter"))
    );
    let silent = if filter {
        // `filter` passes elements through: an entry read needs them primitive.
        callback_is_silent_on_elements(callback, input.primitive, Use::Value)
            && (consumer == Consumer::Values || input.primitive)
    } else {
        let returned = match consumer {
            Consumer::Values => Use::Value,
            Consumer::Entries => Use::Entry,
        };
        callback_is_silent_on_elements(callback, input.primitive, returned)
    };
    silent.then_some(Stage {
        root: input.root,
        primitive: filter && input.primitive,
    })
}

/// How an expression's value is consumed.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Use {
    /// Stored, returned, compared with `===`/`!==`, or tested by
    /// `ToBoolean`: no user code runs.
    Value,
    /// Converted (`ToPrimitive`, `ToPropertyKey`, `ToNumber`, iteration) or
    /// used as a member receiver: an object's hooks or accessors may run.
    Coerce,
    /// An `Object.fromEntries` entry: `[[Get]]` of `"0"` and `"1"`, then
    /// `ToPropertyKey` of the first.
    Entry,
}

/// Whether a call-free element callback runs no user code on the elements
/// it receives. Primitive elements make every step silent under A11;
/// otherwise each element-derived value may only be stored, returned,
/// compared strictly or tested for truthiness.
fn callback_is_silent_on_elements(callback: &Expr, primitive: bool, returned: Use) -> bool {
    if primitive {
        return true;
    }
    let (params, body): (Vec<&Pat>, ElementCallbackBody<'_>) = match callback.unwrap_parens() {
        Expr::Arrow(arrow) => (
            arrow.params.iter().collect(),
            match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(expr) => ElementCallbackBody::Expr(expr),
                ArrowFunctionBody::FunctionBody(body) => ElementCallbackBody::Stmts(&body.stmts),
            },
        ),
        Expr::Fn(function) => match &function.function.body {
            Some(body) => (
                function
                    .function
                    .params
                    .iter()
                    .map(|param| &param.pat)
                    .collect(),
                ElementCallbackBody::Stmts(&body.stmts),
            ),
            None => return false,
        },
        _ => return false,
    };
    // `sees_only_element` limited the parameters to `(element, index)`;
    // the index is a Number. A destructured element reads its members.
    let element = match params.first() {
        Some(Pat::Ident(element)) => Some(element.id.sym.to_string()),
        Some(_) => return false,
        None => None,
    };
    let check = ElementUses { element, returned };
    match body {
        ElementCallbackBody::Expr(expr) => check.expr(expr, returned),
        ElementCallbackBody::Stmts(stmts) => check.stmts(stmts),
    }
}

enum ElementCallbackBody<'a> {
    Expr(&'a Expr),
    Stmts(&'a [Stmt]),
}

/// Checks every use of one element-callback parameter. A same-named inner
/// binding counts as the element, which only rejects more.
struct ElementUses {
    element: Option<String>,
    returned: Use,
}

impl ElementUses {
    fn is_element(&self, ident: &Ident) -> bool {
        self.element.as_deref() == Some(ident.sym.as_ref())
    }

    fn mentions_element<N: VisitWith<ElementMention>>(&self, node: &N) -> bool {
        let mut mention = ElementMention {
            element: self.element.clone(),
            found: false,
        };
        node.visit_with(&mut mention);
        mention.found
    }

    fn stmts(&self, stmts: &[Stmt]) -> bool {
        stmts.iter().all(|stmt| self.stmt(stmt))
    }

    fn stmt(&self, stmt: &Stmt) -> bool {
        match stmt {
            Stmt::Expr(statement) => self.expr(&statement.expr, Use::Value),
            Stmt::Return(statement) => statement
                .arg
                .as_deref()
                .is_none_or(|arg| self.expr(arg, self.returned)),
            Stmt::If(statement) => {
                self.expr(&statement.test, Use::Value)
                    && self.stmt(&statement.cons)
                    && statement.alt.as_deref().is_none_or(|alt| self.stmt(alt))
            }
            Stmt::Block(block) => self.stmts(&block.stmts),
            Stmt::Empty(_) => true,
            Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(|decl| {
                matches!(decl.name, Pat::Ident(_))
                    && decl
                        .init
                        .as_deref()
                        .is_none_or(|init| self.expr(init, Use::Coerce))
            }),
            other => !self.mentions_element(other),
        }
    }

    fn expr(&self, expr: &Expr, used: Use) -> bool {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => !self.is_element(ident) || used == Use::Value,
            Expr::Array(array) if used == Use::Entry => match array.elems.as_slice() {
                [Some(key), Some(value)] if key.spread.is_none() && value.spread.is_none() => {
                    self.expr(&key.expr, Use::Coerce) && self.expr(&value.expr, Use::Value)
                }
                _ => !self.mentions_element(array),
            },
            other if used == Use::Entry => !self.mentions_element(other),
            Expr::Lit(_) | Expr::This(_) => true,
            // Not invoked: the callback is call-free.
            Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_) => true,
            Expr::Tpl(template) => template
                .exprs
                .iter()
                .all(|expr| self.expr(expr, Use::Coerce)),
            Expr::Array(array) => array.elems.iter().flatten().all(|element| {
                self.expr(
                    &element.expr,
                    if element.spread.is_some() {
                        Use::Coerce
                    } else {
                        Use::Value
                    },
                )
            }),
            Expr::Object(object) => object.props.iter().all(|prop| match prop {
                PropOrSpread::Spread(spread) => self.expr(&spread.expr, Use::Coerce),
                PropOrSpread::Prop(prop) => match prop.as_ref() {
                    Prop::Shorthand(ident) => !self.is_element(ident) || used == Use::Value,
                    Prop::KeyValue(kv) => {
                        self.prop_name(&kv.key) && self.expr(&kv.value, Use::Value)
                    }
                    Prop::Getter(getter) => self.prop_name(&getter.key),
                    Prop::Setter(setter) => self.prop_name(&setter.key),
                    Prop::Method(method) => self.prop_name(&method.key),
                    Prop::Assign(_) => false,
                },
            }),
            Expr::Member(member) => self.member(member),
            Expr::OptChain(chain) => match chain.base.as_ref() {
                OptChainBase::Member(member) => self.member(member),
                OptChainBase::Call(_) => false,
            },
            Expr::Bin(binary) => match binary.op {
                BinaryOp::EqEqEq | BinaryOp::NotEqEq => {
                    self.expr(&binary.left, Use::Value) && self.expr(&binary.right, Use::Value)
                }
                // `ToBoolean` runs no user code; the result is an operand.
                BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing => {
                    self.expr(&binary.left, used) && self.expr(&binary.right, used)
                }
                _ => self.expr(&binary.left, Use::Coerce) && self.expr(&binary.right, Use::Coerce),
            },
            Expr::Unary(unary) => match unary.op {
                UnaryOp::Bang | UnaryOp::TypeOf | UnaryOp::Void => {
                    self.expr(&unary.arg, Use::Value)
                }
                UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Tilde | UnaryOp::Delete => {
                    self.expr(&unary.arg, Use::Coerce)
                }
            },
            Expr::Update(update) => self.expr(&update.arg, Use::Coerce),
            Expr::Cond(cond) => {
                self.expr(&cond.test, Use::Value)
                    && self.expr(&cond.cons, used)
                    && self.expr(&cond.alt, used)
            }
            Expr::Seq(sequence) => sequence.exprs.split_last().is_some_and(|(last, rest)| {
                rest.iter().all(|expr| self.expr(expr, Use::Value)) && self.expr(last, used)
            }),
            Expr::Assign(assign) => {
                // Binding an element-derived value to a name loses track of
                // it, and a compound operator converts both operands.
                let right = if assign.op == AssignOp::Assign
                    && matches!(
                        assign.left,
                        AssignTarget::Simple(SimpleAssignTarget::Member(_))
                    ) {
                    Use::Value
                } else {
                    Use::Coerce
                };
                let left = match &assign.left {
                    AssignTarget::Simple(SimpleAssignTarget::Member(member)) => self.member(member),
                    AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                        !self.is_element(&binding.id)
                    }
                    other => !self.mentions_element(other),
                };
                left && self.expr(&assign.right, right)
            }
            other => !self.mentions_element(other),
        }
    }

    /// A member read or write target: its receiver may run accessors, and a
    /// computed key runs `ToPropertyKey`.
    fn member(&self, member: &MemberExpr) -> bool {
        self.expr(&member.obj, Use::Coerce)
            && match &member.prop {
                MemberProp::Computed(key) => self.expr(&key.expr, Use::Coerce),
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => true,
            }
    }

    fn prop_name(&self, name: &PropName) -> bool {
        match name {
            PropName::Computed(key) => self.expr(&key.expr, Use::Coerce),
            _ => true,
        }
    }
}

/// Whether a node reads the element parameter outside nested functions.
struct ElementMention {
    element: Option<String>,
    found: bool,
}

impl Visit for ElementMention {
    fn visit_ident(&mut self, ident: &Ident) {
        self.found |= self.element.as_deref() == Some(ident.sym.as_ref());
    }

    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_class(&mut self, _: &Class) {}
}

fn callback_has_no_sync_invocation(expr: &Expr) -> bool {
    let mut finder = SyncInvocationFinder::default();
    match expr.unwrap_parens() {
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
    hints: &'a AnalysisInputs,
    native_facts: &'a ValidatedNativeOperationFacts,
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
                Expr::Call(call) if self.native_facts.symbol_for_literal_proof(call).is_some() => {
                    false
                }
                Expr::Call(_) | Expr::New(_) => !classify_expr_purity(
                    expr,
                    self.shadowed,
                    &BTreeSet::new(),
                    &self.hints.assumptions.declared_pure,
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
    hints: &AnalysisInputs,
    native_facts: &ValidatedNativeOperationFacts,
    graph: &ChunkCodeGraph,
) -> bool {
    let mut finder = OpaqueAtInitCallFinder {
        shadowed,
        hints,
        native_facts,
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
    hints: &AnalysisInputs,
) -> bool {
    let mut finder = UntrustedAtInitInlineFnFallbackFinder {
        no_sync_callback_members: &hints.assumptions.no_sync_callback_members,
        found: false,
    };
    item.visit_with(&mut finder);
    finder.found
}

pub(crate) fn no_sync_member_argument_fallback_sources(
    item: &ModuleItem,
    hints: &AnalysisInputs,
) -> PositionBucketed<BTreeSet<Id>> {
    let mut collector = NoSyncMemberArgumentSourceCollector {
        no_sync_callback_members: &hints.assumptions.no_sync_callback_members,
        sources: PositionBucketed::default(),
        lazy_depth: 0,
        past_await: false,
        may_skip_await_depth: 0,
    };
    item.visit_with(&mut collector);
    collector.sources
}

/// Whether a builtin member row covers the call and invokes none of its
/// arguments synchronously.
pub(crate) fn member_row_invokes_no_argument(callee: &Expr, args: &[ExprOrSpread]) -> bool {
    member_call_row(callee, args).is_some_and(|row| row.invokes.is_empty())
}

pub(crate) fn is_no_sync_callback_member_call(
    callee: &Expr,
    no_sync_callback_members: &BTreeMap<String, BTreeSet<String>>,
) -> bool {
    match callee.unwrap_parens() {
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
    let Expr::Ident(receiver) = member.obj.as_ref().unwrap_parens() else {
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
    may_skip_await_depth: u32,
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
    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
    }
}

impl Visit for NoSyncMemberArgumentSourceCollector<'_> {
    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            lazy_visit_may_skip(self, node);
        } else {
            node.visit_children_with(self);
        }
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
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        lazy_visit_may_skip(self, node);
    }

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
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
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

struct UntrustedAtInitInlineFnFallbackFinder<'a> {
    no_sync_callback_members: &'a BTreeMap<String, BTreeSet<String>>,
    found: bool,
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
        match &node.callee {
            Callee::Expr(callee) => match callee.unwrap_parens() {
                Expr::Ident(_) => {}
                _ if self.node_has_inline_fn(node)
                    && !member_row_invokes_no_argument(callee, &node.args)
                    && !self.is_no_sync_callback_member_call(node) =>
                {
                    self.found = true;
                    return;
                }
                _ => {}
            },
            Callee::Import(_) | Callee::Super(_) => {}
        }
        node.visit_children_with(self);
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        if self.node_has_inline_fn(node)
            && !member_row_invokes_no_argument(&node.callee, &node.args)
        {
            self.found = true;
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        if self.node_has_inline_fn(node) {
            self.found = true;
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, node: &MethodProp) {
        node.key.visit_with(self);
    }
    fn visit_getter_prop(&mut self, node: &GetterProp) {
        node.key.visit_with(self);
    }
    fn visit_setter_prop(&mut self, node: &SetterProp) {
        node.key.visit_with(self);
    }
    // Class decorators and extends are eager; the shared member traversal
    // includes computed keys, static initializers and blocks, not lazy bodies.
    fn visit_class_member(&mut self, member: &ClassMember) {
        visit_eager_member_parts(self, member);
    }
}
